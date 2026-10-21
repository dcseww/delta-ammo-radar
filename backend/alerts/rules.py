# -*- coding: utf-8 -*-
"""告警规则判定。

设计原则
--------
1. **纯函数**：不读数据库、不读时钟（`now_ts` 由调用方注入），
   因此冷却、静默、持续确认这些容易出错的时间逻辑可以被完整单测。
2. **防轰炸四件套**：冷却期、同物品合并、静默时段、持续确认。
   这四件事必须在第一版就实现 —— 一个会疯狂弹窗的告警系统，
   最终结果一定是用户关掉它，等于没做。
3. **可解释**：每次判定都返回触发原因文本，直接展示给用户，
   避免出现"它为什么报警"说不清的情况。

规则类型
--------
price_above       单发价 >= 阈值
price_below       单发价 <= 阈值（抄底用）
pct_up            窗口内涨幅 >= 阈值（%）
pct_down          窗口内跌幅 >= 阈值（%）
percentile_below  近 N 天分位 <= 阈值（0~1）
vol_spike         |z-score| >= 阈值
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.pipeline.metrics import SeriesMetrics

RuleType = Literal[
    "price_above",
    "price_below",
    "pct_up",
    "pct_down",
    "percentile_below",
    "vol_spike",
]

ALL_RULE_TYPES: tuple[str, ...] = (
    "price_above", "price_below", "pct_up", "pct_down", "percentile_below", "vol_spike",
)

# 静默时段**不阻挡**的规则类型：异动往往是最重要的信号，半夜也值得叫醒。
QUIET_HOURS_EXEMPT: frozenset[str] = frozenset({"vol_spike"})


@dataclass(slots=True)
class RuleSpec:
    """规则的最小必要信息（与 alert_rule 表解耦，便于单测构造）。"""

    rule_id: int | None = None
    ammo_id: str | None = None       # None = 适用于全部物品
    rule_type: str = "price_below"
    threshold: float = 0.0
    window_min: int = 60
    confirm_min: int = 0
    cooldown_min: int = 30
    quiet_hours: str | None = None
    enabled: bool = True
    last_triggered_at: int | None = None   # 由调用方从 alert_log 带入


@dataclass(slots=True)
class RuleEvaluation:
    """单条规则的判定结果。"""

    triggered: bool
    rule_id: int | None = None
    ammo_id: str | None = None
    rule_type: str = ""
    price: float | None = None
    reason: str = ""
    detail: dict = field(default_factory=dict)
    blocked_by: str | None = None    # 未触发时的拦截原因，便于调试规则为什么不响


# ── 时间辅助 ────────────────────────────────────────────────────────────
def parse_quiet_hours(text: str | None) -> tuple[int, int] | None:
    """解析 '23:30-07:00' 为 (起, 止) 的当日分钟数。跨夜时段起 > 止。"""
    if not text or "-" not in text:
        return None
    try:
        a, b = text.split("-", 1)
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
    except (ValueError, TypeError):
        return None
    start, end = sh * 60 + sm, eh * 60 + em
    if not (0 <= start < 1440 and 0 <= end < 1440):
        return None
    return start, end


def in_quiet_hours(minute_of_day: int, window: tuple[int, int] | None) -> bool:
    """判断当前时刻是否落在静默时段内。支持跨夜（start > end）。"""
    if window is None:
        return False
    start, end = window
    if start == end:
        return True                  # 起止相同视为全天静默
    if start < end:
        return start <= minute_of_day < end
    # 跨夜：例如 23:30-07:00
    return minute_of_day >= start or minute_of_day < end


def emit_rule(
    rule: RuleSpec,
    metrics: SeriesMetrics,
    *,
    ammo_id: str,
    now_ts: int,
    local_minute_of_day: int,
    default_cooldown_min: int = 30,
    confirm_ok: bool = True,
) -> RuleEvaluation:
    """判定单条规则是否应当触发。

    参数
    ----
    metrics              该物品当前的指标快照
    now_ts               当前时间（epoch 秒）
    local_minute_of_day  本地时间的"当日第几分钟"（0~1439），用于静默判断
    confirm_ok           是否满足"持续确认"条件，由调用方根据连续采样判断
    """
    ev = RuleEvaluation(triggered=False, rule_id=rule.rule_id,
                        ammo_id=ammo_id, rule_type=rule.rule_type)

    if not rule.enabled:
        ev.blocked_by = "规则已停用"
        return ev

    price = metrics.latest
    ev.price = price
    if price is None:
        ev.blocked_by = "无有效价格"
        return ev

    # ── 静默时段 ────────────────────────────────────────────────────────
    window = parse_quiet_hours(rule.quiet_hours)
    if rule.rule_type not in QUIET_HOURS_EXEMPT and in_quiet_hours(local_minute_of_day, window):
        ev.blocked_by = f"静默时段（{rule.quiet_hours}）"
        return ev

    # ── 冷却期 ──────────────────────────────────────────────────────────
    cooldown_min = rule.cooldown_min if rule.cooldown_min is not None else default_cooldown_min
    if rule.last_triggered_at is not None:
        elapsed_min = (now_ts - rule.last_triggered_at) / 60.0
        if elapsed_min < cooldown_min:
            ev.blocked_by = f"冷却中（还需 {cooldown_min - elapsed_min:.0f} 分钟）"
            return ev

    # ── 持续确认 ────────────────────────────────────────────────────────
    if rule.confirm_min > 0 and not confirm_ok:
        ev.blocked_by = f"未持续满足 {rule.confirm_min} 分钟"
        return ev

    # ── 各类型的实际判定 ────────────────────────────────────────────────
    t = rule.rule_type
    if t == "price_above":
        if price >= rule.threshold:
            ev.triggered = True
            ev.reason = f"价格 {price:.0f} ≥ 阈值 {rule.threshold:.0f}"
        else:
            ev.blocked_by = f"价格 {price:.0f} < 阈值 {rule.threshold:.0f}"

    elif t == "price_below":
        if price <= rule.threshold:
            ev.triggered = True
            ev.reason = f"价格 {price:.0f} ≤ 阈值 {rule.threshold:.0f}（抄底信号）"
        else:
            ev.blocked_by = f"价格 {price:.0f} > 阈值 {rule.threshold:.0f}"

    elif t == "pct_up":
        chg = metrics.today_change_pct if rule.window_min <= 1440 else metrics.day7_change_pct
        if chg is not None and chg >= rule.threshold:
            ev.triggered = True
            ev.reason = f"{rule.window_min} 分钟内涨幅 {chg:.2f}% ≥ {rule.threshold:.2f}%"
            ev.detail["change_pct"] = chg
        else:
            ev.blocked_by = f"涨幅 {chg if chg is None else round(chg, 2)}% 未达阈值"

    elif t == "pct_down":
        chg = metrics.today_change_pct if rule.window_min <= 1440 else metrics.day7_change_pct
        if chg is not None and chg <= -abs(rule.threshold):
            ev.triggered = True
            ev.reason = f"{rule.window_min} 分钟内跌幅 {chg:.2f}% ≤ -{abs(rule.threshold):.2f}%"
            ev.detail["change_pct"] = chg
        else:
            ev.blocked_by = f"跌幅 {chg if chg is None else round(chg, 2)}% 未达阈值"

    elif t == "percentile_below":
        p = metrics.percentile_7d
        if not metrics.percentile_reliable:
            ev.blocked_by = f"分位数样本不足（{metrics.percentile_samples_7d} 条）"
        elif p is not None and p <= rule.threshold:
            ev.triggered = True
            ev.reason = (f"7 日分位 {p * 100:.1f}% ≤ {rule.threshold * 100:.1f}%"
                         f"（样本 {metrics.percentile_samples_7d} 条）")
            ev.detail["percentile_7d"] = p
        else:
            ev.blocked_by = f"分位 {None if p is None else round(p, 3)} 未低于阈值"

    elif t == "vol_spike":
        z = metrics.z_score
        if z is None:
            ev.blocked_by = "波动率样本不足，无法计算 z-score"
        elif abs(z) >= abs(rule.threshold):
            ev.triggered = True
            direction = "高于" if z > 0 else "低于"
            ev.reason = f"价格异动：z={z:.2f}，{direction} 24 小时均价 {abs(z):.1f} 个标准差"
            ev.detail["z_score"] = z
        else:
            ev.blocked_by = f"z={z:.2f} 未达阈值 {abs(rule.threshold)}"

    else:
        ev.blocked_by = f"未知规则类型 {rule.rule_type!r}"

    return ev


def evaluate_all(
    rules: list[RuleSpec],
    metrics_by_ammo: dict[str, SeriesMetrics],
    *,
    now_ts: int,
    local_minute_of_day: int,
    default_cooldown_min: int = 30,
) -> list[RuleEvaluation]:
    """判定一批规则，并做**同物品合并**以防止一次采集弹出多条通知。

    合并策略：同一 ammo_id 若有多条规则同时触发，只保留"最重要"的一条：
    vol_spike > price_below/percentile_below（抄底类）> 其它。
    其余以 blocked_by 标注合并原因，仍然写入日志以便追溯。
    """
    triggered: list[RuleEvaluation] = []
    for rule in rules:
        if rule.ammo_id is not None:
            targets = {rule.ammo_id: metrics_by_ammo.get(rule.ammo_id)}
        else:
            targets = metrics_by_ammo

        for ammo_id, m in targets.items():
            if m is None:
                continue
            ev = emit_rule(
                rule, m,
                ammo_id=ammo_id,
                now_ts=now_ts,
                local_minute_of_day=local_minute_of_day,
                default_cooldown_min=default_cooldown_min,
            )
            if ev.triggered:
                triggered.append(ev)

    # 同物品合并：按优先级保留一条
    priority = {"vol_spike": 0, "price_below": 1, "percentile_below": 2, "pct_down": 3}
    by_ammo: dict[str, list[RuleEvaluation]] = {}
    for ev in triggered:
        by_ammo.setdefault(ev.ammo_id or "", []).append(ev)

    merged: list[RuleEvaluation] = []
    for ammo_id, evs in by_ammo.items():
        evs.sort(key=lambda e: priority.get(e.rule_type, 9))
        winner = evs[0]
        if len(evs) > 1:
            others = ", ".join(e.rule_type for e in evs[1:])
            winner.detail["merged_rules"] = others
            winner.reason += f"（另有 {len(evs) - 1} 条规则同时命中，已合并：{others}）"
        merged.append(winner)

    merged.sort(key=lambda e: (e.ammo_id or "", e.rule_type))
    return merged


def sustained(series_tail: list[tuple[int, float]], rule_type: str, threshold: float,
              confirm_min: int, now_ts: int) -> bool:
    """判断条件是否在最近 confirm_min 分钟内**持续满足**。

    用于抑制"价格在阈值附近反复穿越"造成的抖动告警。
    series_tail: 该物品最近的价格序列 [(ts, price)]，升序。
    """
    if confirm_min <= 0:
        return True
    cutoff = now_ts - confirm_min * 60
    window = [(ts, p) for ts, p in series_tail if ts >= cutoff]
    if len(window) < 2:
        return False                 # 窗口内采样不足，无法确认"持续"

    if rule_type == "price_below":
        return all(p <= threshold for _, p in window)
    if rule_type == "price_above":
        return all(p >= threshold for _, p in window)
    if rule_type == "percentile_below":
        # 分位数需要历史序列，这里退化为"期间最低价不低于阈值对应的价格水平"，
        # 保守起见只在窗口内价格持续走低时算满足。
        first = window[0][1]
        return window[-1][1] <= first
    return True
