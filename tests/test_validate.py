# -*- coding: utf-8 -*-
"""价格校验的单元测试。

重点覆盖两处**容易写出静默错误**的逻辑：
1. 无价格/负价格必须被拒 —— 否则 0 会被当成历史最低价污染分位数；
2. 跳变判异**只在相邻采集间隔足够短时**才生效 ——
   实测有 7 日涨幅 +79% 的真实行情，拿长区间累计涨幅判异会误杀真实暴涨。
"""

from __future__ import annotations

from backend.pipeline.validate import (
    MAX_COMPARABLE_GAP_SECONDS,
    pct_change,
    validate_quote,
)

NOW = 1_800_000_000


def test_accepts_normal_quote():
    r = validate_quote(price=237, ts=NOW, now_ts=NOW)
    assert r.ok and not r.suspect
    assert r.reason == "ok"


def test_rejects_missing_and_non_positive_price():
    for bad in (None, 0, -1, -0.5):
        r = validate_quote(price=bad, ts=NOW, now_ts=NOW)
        assert not r.ok, f"price={bad} 应被拒绝"
        assert r.reason == "non_positive_price"


def test_rejects_future_timestamp_beyond_tolerance():
    # 超出 5 分钟容忍度 → 拒绝
    r = validate_quote(price=100, ts=NOW + 600, now_ts=NOW)
    assert not r.ok and r.reason == "future_timestamp"


def test_allows_small_clock_skew():
    # 5 分钟内的时间偏差应容忍（本机与数据源时钟不可能完全一致）
    r = validate_quote(price=100, ts=NOW + 120, now_ts=NOW)
    assert r.ok


def test_rejects_backwards_timestamp():
    r = validate_quote(price=100, ts=NOW - 10, now_ts=NOW, prev_price=100, prev_ts=NOW)
    assert not r.ok and r.reason == "backwards_timestamp"


def test_rejects_large_jump_between_close_samples():
    """相邻 10 分钟内价格翻倍 → 判异（典型的解析错位或数据源故障）。"""
    r = validate_quote(
        price=200, ts=NOW, now_ts=NOW,
        prev_price=100, prev_ts=NOW - 600, jump_threshold_pct=60,
    )
    assert not r.ok and r.reason == "jump_too_large"
    assert r.change_pct == 100.0


def test_allows_large_jump_between_distant_samples():
    """**关键回归测试**：相隔 7 天的 +79% 是真实行情，绝不能被判异。

    这正是开发文档 5.3 节原设计（对所有相邻差判 ±60%）的缺陷：
    它会把真实的季节性暴涨误杀，让你看不到最该关注的行情。
    """
    r = validate_quote(
        price=179, ts=NOW, now_ts=NOW,
        prev_price=100, prev_ts=NOW - 7 * 86400, jump_threshold_pct=60,
    )
    assert r.ok, "相隔 7 天的 +79% 是真实行情，不应判异"
    assert r.change_pct is None


def test_jump_boundary_is_inclusive_of_threshold():
    """恰好等于阈值不算超限（用 > 而不是 >=），避免边界抖动误伤。"""
    r = validate_quote(
        price=160, ts=NOW, now_ts=NOW,
        prev_price=100, prev_ts=NOW - 60, jump_threshold_pct=60,
    )
    assert r.ok


def test_comparable_gap_constant_is_15_minutes():
    assert MAX_COMPARABLE_GAP_SECONDS == 900


def test_stale_rejection():
    r = validate_quote(price=100, ts=NOW - 3600, now_ts=NOW, stale_after_seconds=1800)
    assert not r.ok and r.reason == "stale"


def test_pct_change_handles_zero_base():
    """基数为 0 时不能返回 inf —— 那会污染下游所有计算。"""
    assert pct_change(0, 100) is None
    assert pct_change(-5, 100) is None
    assert pct_change(100, 150) == 50.0
    assert pct_change(None, 150) is None


def test_describe_is_human_readable():
    r = validate_quote(price=200, ts=NOW, now_ts=NOW,
                       prev_price=100, prev_ts=NOW - 60, jump_threshold_pct=60)
    assert "跳变" in r.describe()
