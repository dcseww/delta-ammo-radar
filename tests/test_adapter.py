# -*- coding: utf-8 -*-
"""适配器解析逻辑的单元测试（离线，不发任何网络请求）。

之所以能离线测：适配器把"取数"与"解析"分开了，`parse_quotes` / `parse_history`
都是纯函数，喂 fixture 即可断言。这也是适配器设计成两层的原因 ——
将来数据源改版时，只要字段变了就能被这些测试立刻抓住。
"""

from __future__ import annotations

import pytest

from backend.sources.dergame import DerGameAdapter, _parse_src_time, _to_float


@pytest.fixture()
def adapter() -> DerGameAdapter:
    return DerGameAdapter()


# ── 类型转换的宽容度 ────────────────────────────────────────────────────
def test_to_float_accepts_int_float_and_string():
    """实测数据源类型会摇摆：7zf 有时 int 有时 float，avg_price 是字符串。"""
    assert _to_float(6) == 6.0
    assert _to_float(6.5) == 6.5
    assert _to_float("284") == 284.0
    assert _to_float("") is None
    assert _to_float(None) is None


def test_to_float_rejects_bool_and_garbage():
    """bool 是 int 的子类，若不排除会把 True 当成 1 元的价格。"""
    assert _to_float(True) is None
    assert _to_float(False) is None
    assert _to_float("abc") is None
    assert _to_float(object()) is None


# ── 时间解析（时区正确性是关键）────────────────────────────────────────
def test_parse_src_time_uses_configured_timezone():
    """数据源给的是不带时区的本地时间，必须按 Asia/Shanghai 解释。

    否则整体偏移 8 小时，新鲜度判断与告警时间全部出错。
    """
    ts = _parse_src_time("2026-10-14 18:30:01")
    assert ts is not None
    # 北京时间 2026-10-14 18:30:01 = UTC 10:30:01
    from datetime import datetime, timezone

    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert (utc.hour, utc.minute) == (10, 30), f"实际解析为 UTC {utc}"


def test_parse_src_time_handles_variants():
    assert _parse_src_time("2026-10-14 18:30") is not None      # 无秒
    assert _parse_src_time("2026/10/14 18:30:01") is not None   # 斜杠
    assert _parse_src_time("2026-10-14") is not None            # 仅日期


def test_parse_src_time_returns_none_for_garbage():
    """脏时间必须返回 None 让上层跳过，而不是抛异常中断整批解析。"""
    assert _parse_src_time("not-a-time") is None
    assert _parse_src_time("") is None
    assert _parse_src_time(None) is None


# ── 报价解析 ────────────────────────────────────────────────────────────
def test_parse_quotes_filters_non_ammo_and_missing_price(adapter, sample_search_payload):
    """一份含 4 条记录的响应里，只有 2 条应被接受。

    被拒的两种情况都很重要：
    - value.new = null（不可交易）→ 若当成 0 会污染分位数；
    - primary = 装备（服务端类型过滤不可靠）→ 必须本地再过滤一次。
    """
    quotes, src_time = adapter.parse_quotes(sample_search_payload)
    assert len(quotes) == 2
    assert src_time == "2026-10-14 18:30:01"

    ids = {q.raw["id"] for q in quotes}
    assert ids == {"37220000001", "37220200001"}
    assert "99000000001" not in ids, "非弹药记录必须被过滤"
    assert "37240700001" not in ids, "无价格记录必须被跳过"


def test_parse_quotes_maps_fields_correctly(adapter, sample_search_payload):
    quotes, _ = adapter.parse_quotes(sample_search_payload)
    q = next(x for x in quotes if x.raw["id"] == "37220000001")
    assert q.price == 237.0
    assert q.unit_price == 237.0        # 本源全是散装 1x1
    assert q.price_kind == "reference"  # 单一参考价，如实标注
    assert q.source == "dergame"
    assert q.raw["caliber"] == ".357 Magnum"
    assert q.raw["grade"] == "3"
    # 数据源自带的 7 日统计被保留，用于与自算值互校
    assert q.raw["day7_change_pct"] == 32.91
    assert q.raw["day7_high"] == 315.0
    assert q.raw["day7_low"] == 179.0


def test_parse_quotes_handles_int_and_float_consistently(adapter, sample_search_payload):
    """同一响应里 7zf 既有 int(6) 也有 float(32.91)，都必须变成 float。"""
    quotes, _ = adapter.parse_quotes(sample_search_payload)
    q = next(x for x in quotes if x.raw["id"] == "37220200001")
    assert q.raw["day7_change_pct"] == 6.0
    assert isinstance(q.raw["day7_change_pct"], float)


def test_parse_quotes_skips_rows_without_id(adapter):
    payload = {"code": 0, "updateTime": None, "data": [
        {"name": "无ID物品", "primary": "弹药", "value": {"new": 100}},
        {"id": "1", "name": "正常", "primary": "弹药", "value": {"new": 100}},
    ]}
    quotes, _ = adapter.parse_quotes(payload)
    assert len(quotes) == 1


def test_parse_quotes_empty_payload(adapter):
    quotes, src = adapter.parse_quotes({})
    assert quotes == []
    assert src is None


def test_parse_quotes_ignores_non_dict_rows(adapter):
    quotes, _ = adapter.parse_quotes({"data": ["字符串", 123, None]})
    assert quotes == []


# ── 曲线解析 ────────────────────────────────────────────────────────────
def test_parse_history_converts_string_price_and_skips_dirty(adapter, sample_chart_payload):
    """avg_price 是字符串，且响应里有脏数据，两者都必须正确处理。"""
    points = adapter.parse_history("ammo_x", sample_chart_payload)
    # 6 条里 2 条脏（空价格、坏时间）应被跳过
    assert len(points) == 4
    assert all(p.ammo_id == "ammo_x" for p in points)
    assert points[0].price == 284.0
    assert isinstance(points[0].price, float)


def test_parse_history_sorted_ascending(adapter, sample_chart_payload):
    """曲线点必须按时间升序 —— 否则均线、回撤、分位数全部算错。"""
    points = adapter.parse_history("ammo_x", sample_chart_payload)
    assert points == sorted(points, key=lambda p: p.ts)


def test_parse_history_empty(adapter):
    assert adapter.parse_history("a", {}) == []
    assert adapter.parse_history("a", {"values": []}) == []
    assert adapter.parse_history("a", {"values": [{"time": "x", "avg_price": "y"}]}) == []


def test_adapter_never_sends_grade_parameter(adapter):
    """回归测试：`grade` 参数会让接口返回空，代码里绝不能出现它。

    这是实测踩出来的坑（见 docs/数据源实测报告-dergame.md 2.2 节）。
    """
    import inspect

    src = inspect.getsource(adapter.fetch_quotes)
    assert '"grade"' not in src and "'grade'" not in src, \
        "fetch_quotes 绝不能发送 grade 参数"
