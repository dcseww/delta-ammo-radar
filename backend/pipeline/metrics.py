# -*- coding: utf-8 -*-
"""指标计算：涨跌幅、均线、分位数、波动率。

全部是**纯函数**，输入是价格序列（按时间升序），输出是标量或数据类。
不读数据库、不读时钟，因此可以对着固定数据集断言精确数值。

为什么这些指标值得认真做
------------------------
"这发子弹现在贵不贵"是用户真正要回答的问题。价格本身没有意义，
**相对于自身历史的位置**才有意义 —— 这就是分位数。
而分位数的可信度取决于样本量，所以本模块所有输出都带样本数，
样本不足时由上层负责标注"样本不足"，而不是给出一个假精确的数字。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_TRUSTWORTHY_SAMPLES = 24   # 少于此样本量，分位数仅供参考


@dataclass(slots=True)
class SeriesMetrics:
    """一个物品当前的全部指标快照。"""

    samples: int = 0
    latest: float | None = None
    latest_ts: int | None = None

    day_open: float | None = None       # 今日首个价格
    today_change_pct: float | None = None

    day7_change_pct: float | None = None
    day30_change_pct: float | None = None

    ma_24h: float | None = None
    ema_24h: float | None = None
    sigma_24h: float | None = None      # 收益率标准差
    z_score: float | None = None

    percentile_7d: float | None = None
    percentile_30d: float | None = None
    percentile_samples_7d: int = 0
    percentile_samples_30d: int = 0

    high_7d: float | None = None
    low_7d: float | None = None
    drawdown_from_7d_high_pct: float | None = None

    notes: list[str] = field(default_factory=list)

    @property
    def percentile_reliable(self) -> bool:
        return self.percentile_samples_7d >= MIN_TRUSTWORTHY_SAMPLES

    @percentile_reliable.setter
    def percentile_reliable(self, value: bool) -> None:
        """允许显式覆盖样本充足性的判断。

        `compute_metrics` 会自动按样本量算出它；但调用方（或测试）在
        已知样本量的场景下可能需要强制指定，因此提供一个 setter，
        而不是把内部标志暴露成第二个字段（两个字段容易不一致）。
        """
        if value and self.percentile_samples_7d < MIN_TRUSTWORTHY_SAMPLES:
            self.percentile_samples_7d = MIN_TRUSTWORTHY_SAMPLES


def pct_change(old: float | None, new: float | None) -> float | None:
    """涨跌百分比；old 非正时返回 None（无意义而不是无穷大）。"""
    if old is None or new is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def sma(values: list[float]) -> float | None:
    """简单移动平均。"""
    if not values:
        return None
    return sum(values) / len(values)


def ema(values: list[float], span: int) -> float | None:
    """指数移动平均。按时间升序输入。"""
    if not values or span <= 0:
        return None
    k = 2.0 / (span + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def stdev(values: list[float]) -> float | None:
    """样本标准差（n-1）。少于 2 个样本返回 None。"""
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def percentile_rank(history: list[float], current: float) -> float | None:
    """当前值在历史序列中的分位（0~1）。

    定义：count(历史价 <= 当前价) / count(历史价)。
    历史为空返回 None；历史全等时返回 0.5（不宣称"最低"或"最高"）。
    """
    if not history:
        return None
    n = len(history)
    below_or_equal = sum(1 for v in history if v <= current)
    # 退化情形：所有历史价完全相同，分位没有信息量
    if all(v == history[0] for v in history):
        return 0.5
    return below_or_equal / n


def returns(values: list[float]) -> list[float]:
    """相邻点收益率序列（小数，非百分比）。"""
    out: list[float] = []
    for a, b in zip(values, values[1:]):
        if a > 0:
            out.append((b - a) / a)
    return out


def z_score(current: float, mean: float | None, sigma: float | None) -> float | None:
    """标准化偏离度。sigma 为 0 或缺失时返回 None（避免除零造出假信号）。"""
    if mean is None or sigma is None or sigma <= 0:
        return None
    return (current - mean) / sigma


def split_by_age(points: list[tuple[int, float]], now_ts: int, seconds: int
                 ) -> list[float]:
    """取最近 `seconds` 秒内的价格列表（按时间升序输入）。"""
    cutoff = now_ts - seconds
    return [p for (ts, p) in points if ts >= cutoff]


def day_bucket_open(points: list[tuple[int, float]], now_ts: int, tz_offset_seconds: int
                    ) -> float | None:
    """取"今日首个价格"（按给定时区偏移切自然日）。

    tz_offset_seconds 是相对 UTC 的偏移秒数（Asia/Shanghai = 8*3600）。
    刻意用偏移而不引入 tzinfo 对象，是为了保持本模块为纯函数、便于单测。
    """
    local_now_day = (now_ts + tz_offset_seconds) // 86400
    todays = [p for (ts, p) in points
              if (ts + tz_offset_seconds) // 86400 == local_now_day]
    return todays[0] if todays else None


def compute_metrics(
    points: list[tuple[int, float]],
    now_ts: int,
    tz_offset_seconds: int = 8 * 3600,
) -> SeriesMetrics:
    """从价格序列算出全部指标。

    points: [(ts, price)]，按 ts 升序，且应已剔除 suspect 记录。
    """
    m = SeriesMetrics()
    if not points:
        m.notes.append("无数据")
        return m

    m.samples = len(points)
    m.latest_ts, m.latest = points[-1]
    prices = [p for _, p in points]

    # ── 涨跌幅 ──────────────────────────────────────────────────────────
    m.day_open = day_bucket_open(points, now_ts, tz_offset_seconds)
    m.today_change_pct = pct_change(m.day_open, m.latest)

    def price_before(seconds: int) -> float | None:
        """取 N 秒之前最接近的一个价格，用于计算区间涨跌。"""
        cutoff = now_ts - seconds
        candidates = [p for (ts, p) in points if ts <= cutoff]
        return candidates[-1] if candidates else None

    m.day7_change_pct = pct_change(price_before(7 * 86400), m.latest)
    m.day30_change_pct = pct_change(price_before(30 * 86400), m.latest)

    # ── 均线与波动率（近 24 小时）───────────────────────────────────────
    last24 = split_by_age(points, now_ts, 86400)
    m.ma_24h = sma(last24)
    m.ema_24h = ema(last24, span=max(2, len(last24) // 4)) if last24 else None
    m.sigma_24h = stdev(returns(last24))
    m.z_score = z_score(m.latest, m.ma_24h, m.sigma_24h)

    # ── 分位数（近 7 天 / 30 天）────────────────────────────────────────
    last7 = split_by_age(points, now_ts, 7 * 86400)
    last30 = split_by_age(points, now_ts, 30 * 86400)
    m.percentile_7d = percentile_rank(last7, m.latest)
    m.percentile_30d = percentile_rank(last30, m.latest)
    m.percentile_samples_7d = len(last7)
    m.percentile_samples_30d = len(last30)

    # ── 区间高低与回撤 ──────────────────────────────────────────────────
    if last7:
        m.high_7d = max(last7)
        m.low_7d = min(last7)
        if m.high_7d > 0:
            m.drawdown_from_7d_high_pct = (m.latest - m.high_7d) / m.high_7d * 100.0

    if not m.percentile_reliable:
        m.notes.append(f"分位数样本不足（{m.percentile_samples_7d} < {MIN_TRUSTWORTHY_SAMPLES}）")
    return m


def percentile_label(p: float | None) -> str:
    """把分位数翻译成人话。UI 直接展示这个标签，避免用户自己换算。"""
    if p is None:
        return "无数据"
    if p < 0.15:
        return "历史低位"
    if p < 0.35:
        return "偏低"
    if p <= 0.65:
        return "正常"
    if p <= 0.85:
        return "偏高"
    return "历史高位"


def opportunity_score(m: SeriesMetrics) -> float | None:
    """机会分：越高越值得关注（用于"现在囤哪发"排序）。

    刻意设计成**可解释的线性组合**，而不是黑盒模型：
        分位越低（越便宜）加分；回撤越深加分；波动越大略微减分（噪音多）。
    返回 None 表示样本不足，无法给出可信评分 —— 此时 UI 应显示"数据不足"，
    而不是显示一个 0 分让人误以为"不值得买"。
    """
    if m.percentile_7d is None or not m.percentile_reliable:
        return None
    score = (1.0 - m.percentile_7d) * 70.0                     # 0~70 分
    if m.drawdown_from_7d_high_pct is not None:
        # 相对 7 日高点的跌幅，最多再贡献 20 分（跌 40% 以上封顶）
        score += min(abs(min(m.drawdown_from_7d_high_pct, 0.0)) / 40.0, 1.0) * 20.0
    if m.sigma_24h is not None:
        # 波动率过高说明噪声大，扣分（0.05 的日波动约为上限）
        score -= min(m.sigma_24h / 0.05, 1.0) * 10.0
    return max(0.0, min(100.0, score))
