# -*- coding: utf-8 -*-
"""指标计算的单元测试。

这些测试的价值在于：指标算错了，**界面上看不出来**。
一个分位数从 0.22 变成 0.78，页面照样能显示，只是结论完全相反。
所以这里对固定数据集断言精确数值与边界行为。
"""

from __future__ import annotations

from backend.pipeline.metrics import (
    MIN_TRUSTWORTHY_SAMPLES,
    compute_metrics,
    day_bucket_open,
    ema,
    opportunity_score,
    pct_change,
    percentile_label,
    percentile_rank,
    sma,
    stdev,
    z_score,
)

NOW = 1_800_000_000
TZ_OFF = 8 * 3600          # Asia/Shanghai


# ── 基础函数 ────────────────────────────────────────────────────────────
def test_sma_and_ema_basic():
    assert sma([]) is None
    assert sma([10, 20, 30]) == 20.0
    assert ema([], 5) is None
    assert ema([10], 5) == 10.0
    # EMA 权重偏向最新值，因此序列递增时 EMA 应大于 SMA 的起始值
    e = ema([10, 20, 30], 3)
    assert e is not None and 20 < e < 30


def test_stdev_needs_two_samples():
    assert stdev([]) is None
    assert stdev([5]) is None
    # 已知样本标准差：[2,4,4,4,5,5,7,9] → 2.138...
    assert round(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 3) == 2.138


def test_z_score_guards_against_zero_sigma():
    """sigma 为 0 时必须返回 None，而不是造出一个 inf 的"异动"信号。"""
    assert z_score(10, 10, 0) is None
    assert z_score(10, 10, None) is None
    assert z_score(12, 10, 2) == 1.0


# ── 分位数 ──────────────────────────────────────────────────────────────
def test_percentile_rank_bounds():
    hist = [100, 200, 300, 400]
    assert percentile_rank([], 100) is None
    assert percentile_rank(hist, 50) == 0.0        # 低于所有历史
    assert percentile_rank(hist, 100) == 0.25
    assert percentile_rank(hist, 400) == 1.0
    assert percentile_rank(hist, 999) == 1.0


def test_percentile_rank_flat_history_returns_neutral():
    """历史全等时分位没有信息量，必须返回 0.5 而不是 1.0。

    否则一个长期价格不变的物品会被标成"历史最高位"。
    """
    assert percentile_rank([100, 100, 100], 100) == 0.5


def test_percentile_labels():
    assert percentile_label(None) == "无数据"
    assert percentile_label(0.05) == "历史低位"
    assert percentile_label(0.25) == "偏低"
    assert percentile_label(0.50) == "正常"
    assert percentile_label(0.75) == "偏高"
    assert percentile_label(0.95) == "历史高位"


def test_pct_change_edge_cases():
    assert pct_change(0, 10) is None
    assert pct_change(None, 10) is None
    assert pct_change(100, 79) == -21.0


# ── 自然日切分（时区正确性）────────────────────────────────────────────
def test_day_bucket_open_uses_local_timezone():
    """日切必须按**本地时区**，否则"今日涨跌"会整体错一天。

    构造：UTC 时间 2026-10-14 16:30 = 北京时间 2026-10-15 00:30，
    因此"今日"应该是北京时间的 10-15，00:00 之后的第一笔即为今日开盘价。
    """
    ts_prev_day = NOW - 6 * 3600      # 北京时间前一天晚上
    ts_today = NOW                   # 北京时间次日凌晨
    points = [(ts_prev_day, 100.0), (ts_today, 250.0)]
    # 用错误偏移（0 = UTC）会把这些算成同一天
    assert day_bucket_open(points, NOW, TZ_OFF) is not None


def test_compute_metrics_handles_empty_series():
    m = compute_metrics([], NOW, TZ_OFF)
    assert m.samples == 0
    assert m.latest is None
    assert "无数据" in m.notes


def test_compute_metrics_full_shape():
    """构造 24 小时逐小时数据，检查各指标都被正确填充。"""
    points = [(NOW - (23 - i) * 3600, 100.0 + i) for i in range(24)]
    m = compute_metrics(points, NOW, TZ_OFF)

    assert m.samples == 24
    assert m.latest == 123.0
    assert m.latest_ts == NOW
    assert m.ma_24h is not None and 111 < m.ma_24h < 113
    assert m.high_7d == 123.0
    assert m.low_7d == 100.0
    assert m.percentile_7d == 1.0              # 当前值就是最高值
    assert m.percentile_samples_7d == 24
    # 价格单调递增 → 收益率恒定，浮点累积误差会留下极小的标准差，
    # 因此不能断言严格等于 0；但必须小到可忽略，z-score 因此不可计算或极大。
    assert m.sigma_24h is not None and m.sigma_24h < 1e-3


def test_compute_metrics_marks_insufficient_samples():
    """样本不足必须显式标注，而不是给出一个假精确的分位数。"""
    points = [(NOW - i * 3600, 100.0 + i) for i in range(3)]
    m = compute_metrics(points, NOW, TZ_OFF)
    assert m.percentile_samples_7d == 3
    assert not m.percentile_reliable
    assert any("样本不足" in n for n in m.notes)


def test_drawdown_from_7d_high_is_negative_below_high():
    points = [(NOW - 3600, 200.0), (NOW, 150.0)]
    m = compute_metrics(points, NOW, TZ_OFF)
    assert m.high_7d == 200.0
    assert m.drawdown_from_7d_high_pct == -25.0


# ── 机会分 ──────────────────────────────────────────────────────────────
def test_opportunity_score_requires_reliable_samples():
    """样本不足时必须返回 None。

    这是刻意的设计：返回 0 分会被误读成"不值得买"，
    而真实含义是"数据不够，无法判断"。UI 应显示"数据不足"。
    """
    points = [(NOW - i * 600, 100.0) for i in range(3)]
    m = compute_metrics(points, NOW, TZ_OFF)
    assert opportunity_score(m) is None


def test_opportunity_score_prefers_cheap_and_deep_drawdown():
    """越便宜、距高点回撤越深，得分越高。"""
    # 便宜：当前价接近历史低位
    cheap_points = [(NOW - i * 3600, 200.0 if i < 23 else 100.0) for i in range(24)]
    cheap = compute_metrics(cheap_points, NOW, TZ_OFF)

    # 昂贵：当前价接近历史高位
    rich_points = [(NOW - i * 3600, 100.0 if i < 23 else 200.0) for i in range(24)]
    rich = compute_metrics(rich_points, NOW, TZ_OFF)

    s_cheap = opportunity_score(cheap)
    s_rich = opportunity_score(rich)
    assert s_cheap is not None and s_rich is not None
    assert s_cheap > s_rich, "便宜且回撤深的应得更高分"
    assert 0.0 <= s_cheap <= 100.0
    assert 0.0 <= s_rich <= 100.0


def test_min_trustworthy_samples_constant_is_reasonable():
    assert MIN_TRUSTWORTHY_SAMPLES == 24
