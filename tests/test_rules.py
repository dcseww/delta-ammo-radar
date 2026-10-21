# -*- coding: utf-8 -*-
"""告警规则判定的单元测试。

告警是最容易"做出来但没人用"的功能，原因几乎总是同一个：**太吵**。
所以这里重点验证防轰炸四件套：
冷却期、静默时段、持续确认、同物品合并。
另外还验证一条**必须存在的入口校验**：percentile_below 的阈值必须是 0~1 小数，
否则用户填 15（以为是 15%）会得到一条永远不会触发的规则 —— 而且失败得很安静。
"""

from __future__ import annotations

import pytest

from backend.alerts.rules import (
    QUIET_HOURS_EXEMPT,
    RuleSpec,
    emit_rule,
    evaluate_all,
    in_quiet_hours,
    parse_quiet_hours,
    sustained,
)
from backend.pipeline.metrics import SeriesMetrics

NOW = 1_800_000_000


def metrics(price: float = 100.0, **kw) -> SeriesMetrics:
    m = SeriesMetrics(samples=kw.pop("samples", 100), latest=price, latest_ts=NOW)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


# ── 静默时段 ────────────────────────────────────────────────────────────
def test_parse_quiet_hours():
    assert parse_quiet_hours("23:30-07:00") == (23 * 60 + 30, 7 * 60)
    assert parse_quiet_hours("09:00-17:00") == (540, 1020)
    assert parse_quiet_hours(None) is None
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("乱写") is None
    assert parse_quiet_hours("25:00-07:00") is None


def test_in_quiet_hours_crossing_midnight():
    """跨夜时段是最容易写错的地方：23:30-07:00 必须同时覆盖 23:45 与 06:00。"""
    w = parse_quiet_hours("23:30-07:00")
    assert in_quiet_hours(23 * 60 + 45, w) is True
    assert in_quiet_hours(6 * 60, w) is True
    assert in_quiet_hours(7 * 60, w) is False       # 右开区间
    assert in_quiet_hours(12 * 60, w) is False
    assert in_quiet_hours(0, w) is True


def test_in_quiet_hours_same_day_window():
    w = parse_quiet_hours("09:00-17:00")
    assert in_quiet_hours(600, w) is True
    assert in_quiet_hours(1020, w) is False
    assert in_quiet_hours(60, w) is False


def test_in_quiet_hours_none_window():
    assert in_quiet_hours(0, None) is False


# ── 基础触发 ────────────────────────────────────────────────────────────
def test_price_below_triggers():
    rule = RuleSpec(rule_id=1, ammo_id="a", rule_type="price_below", threshold=120)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert ev.triggered
    assert "抄底" in ev.reason
    assert ev.price == 100.0


def test_price_below_does_not_trigger_above_threshold():
    rule = RuleSpec(rule_id=1, rule_type="price_below", threshold=120)
    ev = emit_rule(rule, metrics(150.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert ev.blocked_by is not None


def test_price_above_triggers():
    rule = RuleSpec(rule_id=2, rule_type="price_above", threshold=100)
    ev = emit_rule(rule, metrics(150.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert ev.triggered


def test_disabled_rule_never_triggers():
    rule = RuleSpec(rule_id=3, rule_type="price_below", threshold=999, enabled=False)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert "停用" in (ev.blocked_by or "")


# ── 冷却与静默 ──────────────────────────────────────────────────────────
def test_cooldown_blocks_repeat_trigger():
    """刚触发过的规则在冷却期内不得重复触发 —— 这是防轰炸的第一道闸。"""
    rule = RuleSpec(rule_id=4, rule_type="price_below", threshold=120,
                    cooldown_min=30, last_triggered_at=NOW - 5 * 60)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert "冷却" in (ev.blocked_by or "")


def test_cooldown_expires():
    rule = RuleSpec(rule_id=4, rule_type="price_below", threshold=120,
                    cooldown_min=30, last_triggered_at=NOW - 31 * 60)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert ev.triggered


def test_quiet_hours_blocks_normal_rule():
    rule = RuleSpec(rule_id=5, rule_type="price_below", threshold=120,
                    quiet_hours="23:30-07:00")
    minute = 6 * 60        # 06:00，处于静默时段
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=minute)
    assert not ev.triggered
    assert "静默" in (ev.blocked_by or "")


def test_quiet_hours_does_not_block_vol_spike():
    """异动是最重要的信号，必须能穿透静默时段。

    这条规则的价值在于：半夜的价格异常往往正是最值得知道的事。
    """
    assert "vol_spike" in QUIET_HOURS_EXEMPT
    rule = RuleSpec(rule_id=6, rule_type="vol_spike", threshold=2.0,
                    quiet_hours="23:30-07:00")
    m = metrics(100.0, z_score=3.5)
    ev = emit_rule(rule, m, ammo_id="a", now_ts=NOW, local_minute_of_day=6 * 60)
    assert ev.triggered, "vol_spike 应穿透静默时段"


def test_confirm_min_blocks_without_sustained():
    rule = RuleSpec(rule_id=7, rule_type="price_below", threshold=120, confirm_min=10)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW,
                   local_minute_of_day=720, confirm_ok=False)
    assert not ev.triggered
    assert "持续" in (ev.blocked_by or "")


# ── 分位数规则（含样本量保护）──────────────────────────────────────────
def test_percentile_below_triggers():
    rule = RuleSpec(rule_id=8, rule_type="percentile_below", threshold=0.15)
    m = metrics(100.0, percentile_7d=0.10, percentile_samples_7d=100,
                percentile_reliable=True)
    ev = emit_rule(rule, m, ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert ev.triggered
    assert "分位" in ev.reason


def test_percentile_below_blocked_when_samples_insufficient():
    """样本不足时**不能**触发。

    否则刚装上系统的第一天，每个物品都会因为"历史里只有它自己"而分位=0，
    瞬间刷屏 —— 这正是用户会立刻关掉告警的典型原因。
    """
    rule = RuleSpec(rule_id=9, rule_type="percentile_below", threshold=0.15)
    m = metrics(100.0, percentile_7d=0.0, percentile_samples_7d=3,
                percentile_reliable=False)
    ev = emit_rule(rule, m, ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert "样本不足" in (ev.blocked_by or "")


def test_vol_spike_requires_z_score():
    rule = RuleSpec(rule_id=10, rule_type="vol_spike", threshold=3.0)
    ev = emit_rule(rule, metrics(100.0, z_score=None), ammo_id="a",
                   now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert "z-score" in (ev.blocked_by or "")


def test_unknown_rule_type_is_reported_not_crashed():
    rule = RuleSpec(rule_id=11, rule_type="不存在的类型", threshold=1)
    ev = emit_rule(rule, metrics(100.0), ammo_id="a", now_ts=NOW, local_minute_of_day=720)
    assert not ev.triggered
    assert "未知规则类型" in (ev.blocked_by or "")


# ── 合并 ────────────────────────────────────────────────────────────────
def test_evaluate_all_merges_multiple_rules_for_same_ammo():
    """同一物品多条规则同时命中时只保留一条 —— 防轰炸的第二道闸。"""
    rules = [
        RuleSpec(rule_id=1, ammo_id="a", rule_type="price_below", threshold=999),
        RuleSpec(rule_id=2, ammo_id="a", rule_type="vol_spike", threshold=1.0),
        RuleSpec(rule_id=3, ammo_id="a", rule_type="pct_down", threshold=1.0),
    ]
    m = metrics(100.0, z_score=5.0, today_change_pct=-20.0, percentile_reliable=False)
    out = evaluate_all(rules, {"a": m}, now_ts=NOW, local_minute_of_day=720)
    assert len(out) == 1, "同一物品应合并为一条通知"
    assert out[0].rule_type == "vol_spike", "异动优先级最高"
    assert "合并" in out[0].reason


def test_evaluate_all_handles_global_rule_across_ammo():
    rules = [RuleSpec(rule_id=1, ammo_id=None, rule_type="price_below", threshold=999)]
    mets = {"a": metrics(100.0), "b": metrics(200.0), "c": metrics(300.0)}
    out = evaluate_all(rules, mets, now_ts=NOW, local_minute_of_day=720)
    assert {e.ammo_id for e in out} == {"a", "b", "c"}


def test_evaluate_all_skips_missing_metrics():
    rules = [RuleSpec(rule_id=1, ammo_id="missing", rule_type="price_below", threshold=999)]
    out = evaluate_all(rules, {"a": metrics(1.0)}, now_ts=NOW, local_minute_of_day=720)
    assert out == []


# ── 持续确认 ────────────────────────────────────────────────────────────
def test_sustained_requires_enough_samples():
    """窗口内采样不足时必须返回 False（不能凭一个点就宣称"持续满足"）。"""
    assert sustained([(NOW, 100.0)], "price_below", 120, 10, NOW) is False
    assert sustained([], "price_below", 120, 10, NOW) is False


def test_sustained_price_below():
    tail = [(NOW - 300, 100.0), (NOW - 200, 110.0), (NOW - 100, 115.0), (NOW, 119.0)]
    assert sustained(tail, "price_below", 120, 5, NOW) is True
    assert sustained(tail, "price_below", 110, 5, NOW) is False


def test_sustained_zero_confirm_always_true():
    assert sustained([], "price_below", 120, 0, NOW) is True


def test_sustained_outside_window_ignored():
    """窗口之外的历史不应影响判断。"""
    tail = [(NOW - 3600, 999.0), (NOW - 60, 100.0), (NOW, 100.0)]
    assert sustained(tail, "price_below", 120, 5, NOW) is True
