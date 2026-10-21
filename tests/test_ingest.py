# -*- coding: utf-8 -*-
"""采集编排的端到端测试（**离线**，用假适配器，不联网）。

为什么这一层测试最重要
----------------------
`ingest_once` 是把"外部数据"变成"可分析数据"的唯一通道。它的三个行为
一旦出错，整条链路都会错，而且**界面上看不出来**：

1. **外部 ID → 内部 ID 的归属**：对应错了，A 物品的价格会显示在 B 物品下面；
2. **幂等**：不幂等会导致同一时刻写入多个点，曲线出现锯齿、均值失真；
3. **判异记录的处理**：判异记录必须入库（留证据）但**不进曲线**（不污染指标）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from backend.catalog import seed as seed_mod
from backend.db import session_scope
from backend.pipeline import ingest as ingest_mod
from backend.repository import (
    count_ammo,
    latest_tick,
    list_ammo,
    price_series,
    tick_counts,
    upsert_ticks,
    TickWrite,
)
from backend.sources.base import FetchResult, HistoryPoint, ItemQuote

NOW = 1_800_000_000


class FakeAdapter:
    """假数据源：完全离线，返回预先构造的报价。

    刻意实现与真实适配器相同的结构（name / fetch_quotes / fetch_history），
    这样测试验证的是**编排逻辑**，而不是 HTTP 细节。
    """

    name = "dergame"

    def __init__(self, quotes=None, history=None, fail_times=0):
        self._quotes = quotes if quotes is not None else []
        self._history = history or []
        self._fail_times = fail_times
        self.calls = 0

    def fetch_quotes(self) -> FetchResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            return FetchResult(ok=False, latency_ms=1, error="模拟网络失败")
        return FetchResult(ok=True, quotes=list(self._quotes), latency_ms=5,
                           src_update_time="2026-10-14 18:30:01")

    def fetch_history(self, ammo_id: str, external_id: str | None = None):
        return [p for p in self._history if p.ammo_id == ammo_id] or None


def make_quote(external_id: str, price: float, name: str = "") -> ItemQuote:
    return ItemQuote(
        ammo_id="", price=price, price_kind="reference", unit_price=price,
        source="dergame", src_ts=NOW,
        raw={"id": external_id, "name": name or external_id, "grade": "3",
             "caliber": ".357 Magnum", "day7_change_pct": 10.0},
    )


@pytest.fixture()
def catalog_file(tmp_path: Path) -> Path:
    """写一份最小可用的基础表。

    刻意用 tmp_path 而不是配置里的真实路径：测试绝不能写进
    `backend/catalog/ammo_catalog.yaml`，否则会污染真实的基础表，
    并让其他测试（以及最终的定时任务）看到假数据。
    """
    entries = [
        {
            "ammo_id": "357_magnum_ap", "name": ".357 Magnum AP", "caliber": ".357 Magnum",
            "tier": "蓝", "grade": "3", "is_pack": 0, "pack_size": 1,
            "length": "1", "width": "1", "tradeable": 1, "enabled": 1,
            "external_ids": {"dergame": ["111"], "variants": [{"grade": "3", "id": "111"}]},
        },
        {
            "ammo_id": "357_magnum_hp", "name": ".357 Magnum HP", "caliber": ".357 Magnum",
            "tier": "绿", "grade": "2", "is_pack": 0, "pack_size": 1,
            "length": "1", "width": "1", "tradeable": 1, "enabled": 1,
            "external_ids": {"dergame": ["222"], "variants": [{"grade": "2", "id": "222"}]},
        },
    ]
    p = tmp_path / "ammo_catalog.yaml"
    p.write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")
    return p


# ── 基础表导入 ──────────────────────────────────────────────────────────
def test_seed_imports_catalog(session, catalog_file):
    stats = seed_mod.seed(session, catalog_file)
    assert stats["entries"] == 2
    assert stats["with_external_ids"] == 2
    assert stats["packs"] == 0
    assert count_ammo(session)["total"] == 2


def test_seed_does_not_overwrite_enabled(session, catalog_file):
    """**关键回归测试**：重新导入基础表不能重置用户的 enabled 选择。

    否则每次刷新基础表，用户手工关掉的物品都会全部复活，
    采集范围失控、请求数暴涨。
    """
    seed_mod.seed(session, catalog_file)
    from backend.repository import set_ammo_enabled

    assert set_ammo_enabled(session, "357_magnum_ap", False)
    session.commit()

    seed_mod.seed(session, catalog_file)      # 再导入一次
    session.commit()

    row = next(a for a in list_ammo(session) if a.ammo_id == "357_magnum_ap")
    assert row.enabled == 0, "enabled 被重新导入覆盖了"


def test_seed_raises_clear_error_when_missing(session, tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        seed_mod.seed(session, tmp_path / "不存在.yaml")
    assert "discover_all_ammo" in str(e.value), "错误信息应告诉用户怎么生成基础表"


# ── 采集主流程 ──────────────────────────────────────────────────────────
def test_ingest_once_resolves_ids_and_writes(session, catalog_file):
    seed_mod.seed(session, catalog_file)
    session.commit()

    adapter = FakeAdapter([make_quote("111", 237.0), make_quote("222", 150.0)])
    rep = ingest_mod.ingest_once(session, adapter, now_ts=NOW)

    assert rep.ok
    assert rep.fetched == 2
    assert rep.resolved == 2
    assert rep.written == 2
    assert rep.suspect == 0

    # 外部 ID 必须被正确归属到内部 ammo_id
    assert price_series(session, "357_magnum_ap", since_ts=0) == [(NOW, 237.0)]
    assert price_series(session, "357_magnum_hp", since_ts=0) == [(NOW, 150.0)]


def test_ingest_is_idempotent(session, catalog_file):
    """同一时刻重复采集不得产生重复点。

    不幂等的后果：曲线出现重复时间点的锯齿，均线与分位数全部失真，
    而且随着调度器每 10 分钟跑一次，错误会持续累积。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    adapter = FakeAdapter([make_quote("111", 237.0)])
    for _ in range(3):
        ingest_mod.ingest_once(session, adapter, now_ts=NOW)
        session.commit()

    series = price_series(session, "357_magnum_ap", since_ts=0)
    assert len(series) == 1, f"重复采集产生了多个点：{series}"
    assert series[0] == (NOW, 237.0)


def test_ingest_updates_price_on_same_timestamp(session, catalog_file):
    """同一时刻价格被修正时应覆盖。

    注意 FakeAdapter 的 src_ts 固定，所以这里复现的是"同一份源数据里价格变了"
    的场景 —— 去重逻辑必须允许它写入（这正是把门槛从"只比 src_ts"
    改成"同时比 src_ts 与价格"的原因）。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 237.0)]), now_ts=NOW)
    session.commit()
    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 250.0)]), now_ts=NOW)
    session.commit()

    assert price_series(session, "357_magnum_ap", since_ts=0) == [(NOW, 250.0)]


def test_ingest_counts_unresolved_ids(session, catalog_file):
    """基础表里没有的外部 ID 必须被计数并报告，而不是静默丢弃。

    否则"数据源新增了物品"这件事会一直不被发现。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    adapter = FakeAdapter([make_quote("111", 237.0), make_quote("999", 1.0)])
    rep = ingest_mod.ingest_once(session, adapter, now_ts=NOW)
    assert rep.resolved == 1
    assert rep.unresolved == 1
    assert "999" in rep.unresolved_ids


def test_ingest_marks_suspect_and_keeps_out_of_series(session, catalog_file):
    """判异记录必须落库（留证据）但不进曲线（不污染指标）。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    # 第一次正常
    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 100.0)]), now_ts=NOW)
    session.commit()

    # 10 分钟后价格翻 3 倍 → 应被判异
    later = NOW + 600
    rep = ingest_mod.ingest_once(
        session, FakeAdapter([make_quote("111", 300.0)]), now_ts=later)
    session.commit()

    assert rep.suspect == 1
    assert "jump_too_large" in rep.suspect_reasons

    # 曲线里只有正常那条
    series = price_series(session, "357_magnum_ap", since_ts=0)
    assert series == [(NOW, 100.0)], f"判异记录混进了曲线：{series}"

    # 但记录仍在库里，便于事后排查
    counts = tick_counts(session)
    assert counts["suspect"] == 1
    assert counts["valid"] == 1


def test_ingest_skips_disabled_ammo(session, catalog_file):
    """用户把物品置 0 后，采集必须跳过它。"""
    seed_mod.seed(session, catalog_file)
    from backend.repository import set_ammo_enabled

    set_ammo_enabled(session, "357_magnum_ap", False)
    session.commit()

    adapter = FakeAdapter([make_quote("111", 237.0), make_quote("222", 150.0)])
    rep = ingest_mod.ingest_once(session, adapter, now_ts=NOW)
    session.commit()

    assert rep.written == 1
    assert price_series(session, "357_magnum_ap", since_ts=0) == []
    assert len(price_series(session, "357_magnum_hp", since_ts=0)) == 1


def test_ingest_reports_failure_without_raising(session, catalog_file):
    """采集失败必须返回 ok=False，绝不能抛异常把调度线程搞死。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    rep = ingest_mod.ingest_once(session, FakeAdapter([], fail_times=5), now_ts=NOW)
    assert not rep.ok
    assert rep.error is not None
    assert "模拟网络失败" in rep.error


def test_ingest_records_health(session, catalog_file):
    from backend.repository import list_health

    seed_mod.seed(session, catalog_file)
    session.commit()

    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 1.0)]), now_ts=NOW)
    session.commit()
    h = {x.source: x for x in list_health(session)}["dergame"]
    assert h.ok_count == 1 and h.fail_streak == 0
    assert h.last_latency_ms == 5

    # 再来一次失败，失败计数与连续失败都要更新
    ingest_mod.ingest_once(session, FakeAdapter([], fail_times=9), now_ts=NOW + 60)
    session.commit()
    h = {x.source: x for x in list_health(session)}["dergame"]
    assert h.fail_count == 1 and h.fail_streak == 1
    assert h.last_error


def test_ingest_writes_raw_payload_for_traceability(session, catalog_file):
    """raw 字段必须保留原始记录，否则出了问题无法回溯"当时收到了什么"。"""
    seed_mod.seed(session, catalog_file)
    session.commit()
    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 237.0)]), now_ts=NOW)
    session.commit()

    tick = latest_tick(session, "357_magnum_ap")
    assert tick is not None and tick.raw
    raw = json.loads(tick.raw)
    assert raw["id"] == "111"
    assert raw["day7_change_pct"] == 10.0
    assert raw["_suspect_reason"] is None


# ── 历史回补 ────────────────────────────────────────────────────────────
def test_backfill_writes_history_points(session, catalog_file):
    """历史回补必须能写入曲线点，且跳过判异（历史点间隔 1 小时，不适用 15 分钟阈值）。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    history = [
        HistoryPoint(ammo_id="357_magnum_ap", ts=NOW - 3 * 3600, price=200.0),
        HistoryPoint(ammo_id="357_magnum_ap", ts=NOW - 2 * 3600, price=210.0),
        # 相对上一点涨 90%，但因为间隔 1 小时且是历史数据，不应被判异
        HistoryPoint(ammo_id="357_magnum_ap", ts=NOW - 3600, price=399.0),
    ]
    adapter = FakeAdapter(history=history)
    rep = ingest_mod.backfill_history(session, adapter, interval_seconds=0,
                                      sleep_fn=lambda _s: None)
    session.commit()

    assert rep.succeeded == 1
    assert rep.points_written == 3
    series = price_series(session, "357_magnum_ap", since_ts=0)
    assert len(series) == 3
    assert series[-1] == (NOW - 3600, 399.0)


def test_backfill_reports_failures_without_stopping(session, catalog_file):
    """单个物品回补失败不能中断整批 —— 这是"尽力而为"的任务。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    class FlakyAdapter(FakeAdapter):
        def fetch_history(self, ammo_id, external_id=None):
            return None          # 全部失败

    rep = ingest_mod.backfill_history(session, FlakyAdapter(), interval_seconds=0,
                                      sleep_fn=lambda _s: None)
    assert rep.requested == 2
    assert rep.failed == 2
    assert rep.succeeded == 0


# ── 回补的断点续跑与覆盖率 ──────────────────────────────────────────────
def _history_for(ammo_id: str, n: int = 30, base: float = 200.0):
    """造 n 个小时级历史点（默认 30 个，超过可信分位数所需的最小样本量）。"""
    return [HistoryPoint(ammo_id=ammo_id, ts=NOW - (n - i) * 3600, price=base + i)
            for i in range(n)]


def test_backfill_only_fetches_missing_items(session, catalog_file):
    """**关键回归测试**：回补只补缺失的物品，不整批重跑。

    初版每次都全量重跑，实测后果是：服务重启一次就把已经回补好的 83 个物品
    全部重拉一遍 —— 白白浪费 2 分钟和 83 次对外请求。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    history = _history_for("357_magnum_ap") + _history_for("357_magnum_hp")
    adapter = FakeAdapter(history=history)
    rep1 = ingest_mod.backfill_history(session, adapter, interval_seconds=0,
                                       sleep_fn=lambda _s: None)
    session.commit()
    assert rep1.succeeded == 2

    # 第二次回补：两个物品都已有足够历史 → 应当什么都不做
    called = []

    class CountingAdapter(FakeAdapter):
        def fetch_history(self, ammo_id, external_id=None):
            called.append(ammo_id)
            return [p for p in self._history if p.ammo_id == ammo_id] or None

    rep2 = ingest_mod.backfill_history(session, CountingAdapter(history=history),
                                       interval_seconds=0, sleep_fn=lambda _s: None)
    assert rep2.requested == 0, "已有完整的物品不该被再次回补"
    assert called == [], f"不该发起任何历史请求，实际：{called}"


def test_backfill_resumes_after_partial_completion(session, catalog_file):
    """只缺一个物品时，回补应只请求那一个 —— 这就是断点续跑。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    # 先让第一个物品有完整历史
    ingest_mod.write_history_points(session, "dergame", _history_for("357_magnum_ap"))
    session.commit()

    called = []

    class CountingAdapter(FakeAdapter):
        def fetch_history(self, ammo_id, external_id=None):
            called.append(ammo_id)
            return _history_for(ammo_id)

    rep = ingest_mod.backfill_history(session, CountingAdapter(), interval_seconds=0,
                                      sleep_fn=lambda _s: None)
    session.commit()
    assert rep.requested == 1
    assert called == ["357_magnum_hp"], f"只应补缺失的那个，实际：{called}"


def test_backfill_coverage_from_database(session, catalog_file):
    """覆盖率必须从**数据库**推导，而不是依赖内存标志。

    初版把"回补完成"只记在调度器内存里，服务一重启标志就丢，
    健康接口会永远显示"未完成"。
    """
    from backend.repository import backfill_coverage, items_missing_history

    seed_mod.seed(session, catalog_file)
    session.commit()

    cov = backfill_coverage(session, "dergame")
    assert cov["total"] == 2 and cov["covered"] == 0
    assert cov["complete"] is False
    assert cov["missing"] == 2
    assert sorted(items_missing_history(session, "dergame")) == [
        "357_magnum_ap", "357_magnum_hp"]

    # 只补一个
    ingest_mod.write_history_points(session, "dergame", _history_for("357_magnum_ap"))
    session.commit()
    cov = backfill_coverage(session, "dergame")
    assert cov["covered"] == 1 and cov["missing"] == 1 and cov["complete"] is False

    # 补完第二个
    ingest_mod.write_history_points(session, "dergame", _history_for("357_magnum_hp"))
    session.commit()
    cov = backfill_coverage(session, "dergame")
    assert cov["covered"] == 2 and cov["complete"] is True
    assert items_missing_history(session, "dergame") == []


def test_backfill_coverage_ignores_suspect_and_thin_history(session, catalog_file):
    """判异记录与样本过少的物品都不算"已覆盖"。"""
    from backend.repository import backfill_coverage

    seed_mod.seed(session, catalog_file)
    session.commit()

    # 只有 3 个点 → 少于 min_points(24)，不算覆盖
    ingest_mod.write_history_points(session, "dergame", _history_for("357_magnum_ap", n=3))
    session.commit()
    cov = backfill_coverage(session, "dergame")
    assert cov["covered"] == 0, "样本过少不该算已覆盖"

    # 写足量的点并标为 suspect → 同样不算
    ingest_mod.write_history_points(session, "dergame", _history_for("357_magnum_ap"))
    session.commit()
    session.execute(text("UPDATE price_tick SET suspect=1 WHERE ammo_id='357_magnum_ap'"))
    session.commit()
    cov = backfill_coverage(session, "dergame")
    assert cov["covered"] == 0, "判异记录不该算已覆盖"


def test_backfill_reports_zero_when_nothing_to_do(session, catalog_file):
    """没有可补物品时不应报错，且报告是全零。"""
    seed_mod.seed(session, catalog_file)
    session.commit()
    ingest_mod.write_history_points(session, "dergame",
                                    _history_for("357_magnum_ap") + _history_for("357_magnum_hp"))
    session.commit()

    rep = ingest_mod.backfill_history(session, FakeAdapter(), interval_seconds=0,
                                      sleep_fn=lambda _s: None)
    assert rep.requested == 0 and rep.succeeded == 0 and rep.failed == 0


# ── 数据源未更新则不重复写 / 实时行不被回补覆盖 ─────────────────────────
def test_ingest_skips_when_source_not_updated(session, catalog_file):
    """**关键回归测试**：数据源未刷新时不重复写同一份数据。

    数据源的 updateTime 是分钟级，而我们的轮询是 10 分钟一次时通常已更新；
    但如果同一分钟内重复触发（例如用户连点两次"刷新数据"），
    初版会把同一份价格再写一遍 —— 数据库膨胀、曲线出现无意义的重复点。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    adapter = FakeAdapter([make_quote("111", 237.0)])   # src_ts 固定为 NOW
    r1 = ingest_mod.ingest_once(session, adapter, now_ts=NOW)
    session.commit()
    assert r1.written == 1 and r1.unchanged == 0

    # 同一份数据（src_ts 相同）再来一次 → 应当被跳过
    r2 = ingest_mod.ingest_once(session, adapter, now_ts=NOW + 60)
    session.commit()
    assert r2.unchanged == 1, "数据源未更新时应跳过"
    assert r2.written == 0

    # 库里没有多出多余的点
    assert len(price_series(session, "357_magnum_ap", since_ts=0)) == 1

    # 数据源刷新后（src_ts 变大）→ 应当写入
    fresh = [ItemQuote(ammo_id="", price=250.0, price_kind="reference", unit_price=250.0,
                       source="dergame", src_ts=NOW + 600,
                       raw={"id": "111", "name": "n", "grade": "3", "caliber": "x"})]
    r3 = ingest_mod.ingest_once(session, FakeAdapter(fresh), now_ts=NOW + 660)
    session.commit()
    assert r3.written == 1 and r3.unchanged == 0
    assert len(price_series(session, "357_magnum_ap", since_ts=0)) == 2


def test_backfill_does_not_overwrite_live_row(session, catalog_file):
    """**关键回归测试**：回补行不得覆盖同一整点上的实时行。

    实测踩到过：实时采集写下的一条判异记录，被随后运行的历史回补覆盖，
    证据悄悄消失（数据库里再也查不到那 2 条判异）。
    回补只是补历史，不该抹掉实时数据。
    """
    seed_mod.seed(session, catalog_file)
    session.commit()

    # 实时写一条（带判异标记与源报字段）
    # 注意 source 必须与回补写入时一致（都是 dergame），否则唯一键不同、
    # 两条会并存，就测不到"覆盖保护"这个逻辑本身。
    live = TickWrite(
        ammo_id="357_magnum_ap", price=999.0, unit_price=999.0,
        price_kind="reference", source="dergame", ts=NOW, src_ts=NOW,
        raw=json.dumps({"id": "111", "_suspect_reason": "jump_too_large"}),
        suspect=1, is_live=1,
    )
    upsert_ticks(session, [live])
    session.commit()

    # 回补写同一个时间戳（历史行，同 source，必然触发唯一键冲突）
    ingest_mod.write_history_points(session, "dergame", [
        HistoryPoint(ammo_id="357_magnum_ap", ts=NOW, price=100.0)])
    session.commit()

    # 实时行必须完好保留
    tick = latest_tick(session, "357_magnum_ap", include_suspect=True)
    assert tick.price == 999.0, f"实时行被回补覆盖了：price={tick.price}"
    assert tick.suspect == 1, "判异标记被回补抹掉了"
    assert tick.is_live == 1, "实时行的来源标记被回补改了"


def test_backfill_writes_when_no_live_row(session, catalog_file):
    """没有实时行时，回补应当正常写入。"""
    seed_mod.seed(session, catalog_file)
    session.commit()

    rep = ingest_mod.backfill_history(
        session, FakeAdapter(history=_history_for("357_magnum_ap")),
        interval_seconds=0, sleep_fn=lambda _s: None)
    session.commit()
    assert rep.succeeded == 1
    assert len(price_series(session, "357_magnum_ap", since_ts=0)) == 30


# ── 新鲜度 ──────────────────────────────────────────────────────────────
def test_data_freshness_without_data(session):
    f = ingest_mod.data_freshness(session, NOW)
    assert f["has_data"] is False
    assert f["stale"] is True


def test_data_freshness_fresh_and_stale(session, catalog_file):
    seed_mod.seed(session, catalog_file)
    session.commit()
    ingest_mod.ingest_once(session, FakeAdapter([make_quote("111", 100.0)]), now_ts=NOW)
    session.commit()

    assert ingest_mod.data_freshness(session, NOW + 60)["stale"] is False
    assert ingest_mod.data_freshness(session, NOW + 3600)["stale"] is True
