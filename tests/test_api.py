# -*- coding: utf-8 -*-
"""API 集成测试（进程内，用 TestClient）。

覆盖范围
--------
- 真实的 lifespan（建表 + 自动导入基础表）；
- 采集 → 入库 → 指标 → 排行榜 → 曲线 的完整读取链路；
- 告警规则的增删改查与**入口校验**（特别是 percentile_below 的 0~1 阈值）；
- 打包物项检查接口（决定 UI 是否展示套利分析）。

刻意不启动调度器（`app.state.testing = True`）：测试里不需要后台轮询，
而且后台线程会让断言变得不确定。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from backend import db as db_mod
from backend.config import get_settings
from backend.db import session_scope
from backend.pipeline import ingest as ingest_mod
from backend.repository import set_ammo_enabled
from backend.sources.base import FetchResult, ItemQuote

NOW = int(time.time())


@pytest.fixture()
def catalog(tmp_path, monkeypatch) -> Path:
    """把基础表写到**临时**路径，并把配置指过去。

    这里踩过两个坑，都值得记录：
    1. **测试绝不能写配置里的真实路径**（backend/catalog/ammo_catalog.yaml）。
       第一次实现直接写真实路径，结果测试跑完把 111 条真实基础表覆盖成 3 条假数据。
    2. **改路径必须走环境变量**。pydantic-settings 的模型默认 `validate_assignment=False`，
       直接 `setattr(settings, "catalog_path", ...)` 不报错但也不生效 ——
       这种"静默失败"比报错更危险，因为它让你以为隔离做好了。
    """
    import importlib

    from backend import config as config_mod

    # 用环境变量改路径，因为 pydantic-settings 的模型默认 validate_assignment=False，
    # 直接 setattr 不报错也不生效 —— 这种"静默失败"比报错更危险。
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "ammo_catalog.yaml"))
    config_mod.get_settings.cache_clear()
    settings = config_mod.get_settings()

    # 让所有已经 `from backend.config import get_settings` 的模块拿到同一份新配置。
    for modname in ("backend.catalog.seed", "backend.app"):
        try:
            mod = importlib.import_module(modname)
            monkeypatch.setattr(mod, "get_settings", lambda: settings, raising=False)
        except ImportError:
            pass

    # 保险校验：万一将来配置方式变了，这里必须立刻失败，
    # 而不是又静默地写进真实文件、把 111 条真实基础表覆盖掉。
    assert str(settings.catalog_path).startswith(str(tmp_path)), (
        f"测试基础表路径未被正确重定向：{settings.catalog_path}")

    entries = [
        {
            "ammo_id": "ammo_a", "name": "测试弹 A", "caliber": ".357 Magnum",
            "tier": "蓝", "grade": "3", "is_pack": 0, "pack_size": 1,
            "length": "1", "width": "1", "tradeable": 1, "enabled": 1,
            "external_ids": {"dergame": ["11"], "variants": [{"grade": "3", "id": "11"}]},
        },
        {
            "ammo_id": "ammo_b", "name": "测试弹 B", "caliber": "5.56x45mm",
            "tier": "紫", "grade": "4", "is_pack": 0, "pack_size": 1,
            "length": "1", "width": "1", "tradeable": 1, "enabled": 1,
            "external_ids": {"dergame": ["22"], "variants": [{"grade": "4", "id": "22"}]},
        },
        {
            "ammo_id": "ammo_c", "name": "不可交易弹 C", "caliber": "9x19mm",
            "tier": "红", "grade": "6", "is_pack": 0, "pack_size": 1,
            "length": "1", "width": "1", "tradeable": 0, "enabled": 1,
            "external_ids": {"dergame": ["33"], "variants": [{"grade": "6", "id": "33"}]},
        },
    ]
    settings.catalog_path.write_text(
        yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")
    return settings.catalog_path


def quote(ext_id: str, price: float, name: str = "") -> ItemQuote:
    return ItemQuote(ammo_id="", price=price, price_kind="reference", unit_price=price,
                     source="dergame", src_ts=NOW,
                     raw={"id": ext_id, "name": name or ext_id, "grade": "3",
                          "caliber": ".357 Magnum", "day7_change_pct": 12.5})


class FakeAdapter:
    name = "dergame"

    def __init__(self, quotes):
        self._quotes = quotes

    def fetch_quotes(self):
        return FetchResult(ok=True, quotes=list(self._quotes), latency_ms=3,
                           src_update_time="2026-10-14 18:30:01")

    def fetch_history(self, ammo_id, external_id=None):
        return None


@pytest.fixture()
def client(catalog, monkeypatch):
    """构造 TestClient，并把应用的调度器适配器换成假的（不发真实网络请求）。"""
    from backend.models import Base

    db_mod.reset_engine()
    db_mod.init_db()

    def wipe(s) -> None:
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()

    with session_scope() as s:
        wipe(s)

    from backend.app import app
    app.state.testing = True

    # 关键：把真实适配器换成假的，避免测试打外部接口
    import backend.pipeline.scheduler as sched_mod
    sched_mod.reset_scheduler()
    sched = sched_mod.get_scheduler()
    sched.adapter = FakeAdapter([quote("11", 237.0), quote("22", 150.0)])

    try:
        with TestClient(app) as c:
            yield c
    finally:
        sched_mod.reset_scheduler()
        # 后清：不把测试数据留给下一个测试文件（否则失败会出现在别的测试里）
        with session_scope() as s:
            wipe(s)


def seed_and_ingest():
    """在测试连接里写入一次采集数据（复用应用的假适配器）。"""
    import backend.pipeline.scheduler as sched_mod
    sched = sched_mod.get_scheduler()
    with session_scope() as s:
        return ingest_mod.ingest_once(s, sched.adapter, now_ts=NOW)


# ── 基础接口 ────────────────────────────────────────────────────────────
def test_health_reports_pack_gap_and_counts(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ammo_counts"]["total"] == 3
    assert j["ammo_counts"]["tradeable"] == 2
    # 还没采集 → 状态应为 down，并给出可操作的提示
    assert j["status"] in ("down", "degraded")
    assert "采集" in j["message"]


def test_openapi_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_ammo_list_before_ingest_has_items_without_price(client):
    r = client.get("/api/ammo")
    assert r.status_code == 200
    j = r.json()
    assert j["total"] == 2                      # 默认只看可交易
    # 没有价格时必须为 None，绝不能是 0（0 会被误读成"价格是 0"）
    assert all(i["price"] is None for i in j["items"])
    assert all(i["percentile_label"] == "无数据" for i in j["items"])


def test_ammo_list_filters(client):
    assert client.get("/api/ammo?tier=蓝").json()["total"] == 1
    assert client.get("/api/ammo?caliber=5.56x45mm").json()["total"] == 1
    assert client.get("/api/ammo?q=测试弹").json()["total"] == 2
    assert client.get("/api/ammo?q=不存在").json()["total"] == 0
    # 关掉"只看可交易"应包含不可交易物品
    assert client.get("/api/ammo?only_tradeable=false").json()["total"] == 3


def test_ammo_detail_404(client):
    assert client.get("/api/ammo/nope").status_code == 404


def test_ammo_detail_is_independent_of_list_filters(client):
    """详情接口必须与列表筛选无关。

    回归测试：详情抽屉曾只用「当前筛选后的那一页表格」来渲染，于是从排行榜
    点开一个不在筛选结果里的子弹时，抽屉标题退化成 ammo_id、指标全空 ——
    看起来像"没选中子弹"。抽屉现在改为按 ammo_id 取权威详情，
    这个接口就得保证：**只要物品存在，任何筛选组合下都能取到完整一行**。
    """
    # 列表被筛空，详情仍须可用
    assert client.get("/api/ammo?q=不存在").json()["total"] == 0
    r = client.get("/api/ammo/ammo_a")
    assert r.status_code == 200
    item = r.json()

    # 抽屉渲染需要的字段一个都不能少
    assert item["ammo_id"] == "ammo_a"
    assert item["name"] == "测试弹 A"            # 不能退化成 id
    assert item["caliber"] == ".357 Magnum"
    assert item["tier"] == "蓝"
    assert "price" in item and "percentile_label" in item
    # 元信息里的格子尺寸（抽屉副标题用）
    assert item["length"] == 1 and item["width"] == 1

    # 与列表里同一条应完全一致（不含 in_watchlist 时）
    listed = {i["ammo_id"]: i for i in client.get("/api/ammo").json()["items"]}["ammo_a"]
    assert item["price"] == listed["price"]
    assert item["percentile_7d"] == listed["percentile_7d"]


# ── 采集后 ──────────────────────────────────────────────────────────────
def test_ingest_trigger_writes_and_serves(client):
    rep = seed_and_ingest()
    assert rep.ok and rep.written == 2

    j = client.get("/api/ammo").json()
    assert j["total"] == 2
    for item in j["items"]:
        assert item["price"] is not None
        assert item["price"] > 0
        assert item["price_kind"] == "reference"
        # 自算的 7 日涨跌在只有 1 个点时无意义，但字段必须存在
        assert "day7_change_pct" in item

    # 数据源自报的指标必须原样带出，且**字段名明确表示它不是涨跌幅**
    # （实测 7zf 不是涨跌百分比：价格贴近 7 日最低点时它仍可能是大正数）
    by_id = {i["ammo_id"]: i for i in j["items"]}
    assert by_id["ammo_a"]["source_index_7zf"] == 12.5
    assert "day7_change_pct_from_source" not in by_id["ammo_a"], \
        "该字段已被取代，不应再出现在响应里"


def test_health_ok_after_ingest(client):
    seed_and_ingest()
    j = client.get("/api/health").json()
    assert j["status"] == "ok"
    assert j["tick_counts"]["total"] == 2
    assert j["data_freshness"]["has_data"] is True
    assert j["sources"] and j["sources"][0]["source"] == "dergame"


def test_price_series_returns_points_and_disclaimer(client):
    seed_and_ingest()
    j = client.get("/api/price/ammo_a?range=24h").json()
    assert j["count"] == 1
    assert j["points"][0]["price"] == 237.0
    assert j["price_kind"] == "reference"
    # 必须明确告知用户这是单一参考价，不能算买卖价差
    assert j["note"] and "参考价" in j["note"]


def test_price_series_rejects_bad_params(client):
    seed_and_ingest()
    assert client.get("/api/price/ammo_a?range=999").status_code == 400
    assert client.get("/api/price/ammo_a?bucket=nope").status_code == 400


def test_price_series_404(client):
    seed_and_ingest()
    assert client.get("/api/price/nope?range=24h").status_code == 404


def test_rank_types_and_notes(client):
    seed_and_ingest()
    for t in ("up", "down", "volatility", "opportunity", "cheap"):
        r = client.get(f"/api/rank?type={t}")
        assert r.status_code == 200, t
        assert r.json()["type"] == t
    assert client.get("/api/rank?type=nope").status_code == 400
    # opportunity 必须带免责说明（它是启发式排序，不是收益预测）
    assert "不是收益预测" in client.get("/api/rank?type=opportunity").json()["note"]


def test_grades_filter_and_disabled_items(client):
    seed_and_ingest()
    with session_scope() as s:
        set_ammo_enabled(s, "ammo_a", False)
        s.commit()
    # enabled 只影响采集，不影响列表展示；列表仍能看到它
    ids = {i["ammo_id"] for i in client.get("/api/ammo").json()["items"]}
    assert "ammo_a" in ids
    assert client.get("/api/ammo?only_enabled=true").json()["total"] == 1


# ── 打包物项检查（决定 UI 是否展示套利分析）────────────────────────────
def test_pack_check_reports_no_packs(client):
    j = client.get("/api/health/pack-check").json()
    assert j["has_pack_items"] is False
    assert j["pack_count"] == 0
    # 关键：必须明确告知不可用，而不是让 UI 去猜
    assert "不可用" in j["message"] or "没有" in j["message"]


# ── 自选 ────────────────────────────────────────────────────────────────
def test_watchlist_crud(client):
    assert client.get("/api/watchlist").json() == []
    assert client.post("/api/watchlist/ammo_a").status_code == 201
    assert client.get("/api/watchlist").json() == ["ammo_a"]
    # 重复添加是幂等的
    assert client.post("/api/watchlist/ammo_a").json()["created"] is False
    assert client.delete("/api/watchlist/ammo_a").status_code == 204
    assert client.get("/api/watchlist").json() == []
    assert client.delete("/api/watchlist/ammo_a").status_code == 404
    assert client.post("/api/watchlist/nope").status_code == 404


def test_watchlist_flag_in_ammo_list(client):
    client.post("/api/watchlist/ammo_a")
    items = {i["ammo_id"]: i for i in client.get("/api/ammo").json()["items"]}
    assert items["ammo_a"]["in_watchlist"] is True
    assert items["ammo_b"]["in_watchlist"] is False
    assert client.get("/api/ammo?only_watch=true").json()["total"] == 1


# ── 告警规则 ────────────────────────────────────────────────────────────
def test_rule_crud(client):
    body = {"rule_type": "price_below", "threshold": 200, "cooldown_min": 15}
    assert client.get("/api/alerts/rules").json() == []
    created = client.post("/api/alerts/rules", json=body).json()
    assert created["rule_id"] > 0 and created["enabled"] is True

    assert len(client.get("/api/alerts/rules").json()) == 1

    toggled = client.post(f"/api/alerts/rules/{created['rule_id']}/toggle").json()
    assert toggled["enabled"] is False

    upd = client.put(f"/api/alerts/rules/{created['rule_id']}",
                     json={"rule_type": "price_above", "threshold": 500}).json()
    assert upd["rule_type"] == "price_above" and upd["threshold"] == 500

    assert client.delete(f"/api/alerts/rules/{created['rule_id']}").status_code == 204
    assert client.get("/api/alerts/rules").json() == []
    assert client.delete("/api/alerts/rules/999").status_code == 404


def test_duplicate_rule_rejected(client):
    """**关键回归测试**：完全相同的规则必须被拒绝。

    冷却期只防"单条规则重复触发"，防不住"多条等效规则各触发一次"。
    实测踩到过：调试时留下 3 条相同规则，结果每轮采集产生 13 条重复告警，
    而用户看到的表现是"冷却没生效" —— 根因与表象完全不一致，极难排查。
    """
    body = {"rule_type": "price_below", "threshold": 300}
    assert client.post("/api/alerts/rules", json=body).status_code == 201

    # 再来一条一模一样的 → 409
    r = client.post("/api/alerts/rules", json=body)
    assert r.status_code == 409
    assert "完全相同" in r.json()["detail"]

    # 阈值不同就不是等效规则，应放行
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 301}
                       ).status_code == 201
    # 规则类型不同也应放行
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_above", "threshold": 300}
                       ).status_code == 201
    # 指定对象后与"作用于全部物品"不算重复
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 300,
                             "ammo_id": "ammo_a"}).status_code == 201
    # 同一对象再来一条相同的才算重复
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 300,
                             "ammo_id": "ammo_a"}).status_code == 409


def test_percentile_rule_rejects_percent_threshold(client):
    """**关键入口校验**：percentile_below 的阈值必须是 0~1。

    用户很自然地会填 15（以为是 15%），那样规则永远不会触发，
    而且完全静默 —— 这类"看起来配好了但从不响"的问题必须在入口拦住。
    """
    r = client.post("/api/alerts/rules",
                    json={"rule_type": "percentile_below", "threshold": 15})
    assert r.status_code == 400
    assert "0~1" in r.json()["detail"]

    ok = client.post("/api/alerts/rules",
                     json={"rule_type": "percentile_below", "threshold": 0.15})
    assert ok.status_code == 201


def test_rule_validation_rejects_bad_input(client):
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "nope", "threshold": 1}).status_code == 400
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "pct_up", "threshold": -5}).status_code == 400
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 0}).status_code == 400
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "vol_spike", "threshold": -1}).status_code == 400
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 1,
                             "quiet_hours": "乱写"}).status_code == 400
    assert client.post("/api/alerts/rules",
                       json={"rule_type": "price_below", "threshold": 1,
                             "ammo_id": "不存在"}).status_code == 400


def test_pending_alerts_and_log(client):
    """建一条必然触发的规则，跑一次采集，验证告警进入队列与历史。"""
    seed_and_ingest()
    client.post("/api/alerts/rules",
                json={"rule_type": "price_below", "threshold": 99999, "cooldown_min": 0})

    rep = seed_and_ingest()
    assert rep.alerts_triggered == 2        # 两个可交易物品都命中

    pending = client.get("/api/alerts/pending").json()
    assert len(pending["items"]) == 2
    assert all("抄底" in n["reason"] for n in pending["items"])

    # 取走即清空，避免重复弹窗
    again = client.get("/api/alerts/pending").json()
    assert again["items"] == []

    logs = client.get("/api/alerts/log").json()
    assert len(logs) == 2
    assert all(l["ammo_name"] for l in logs)


def test_alert_cooldown_prevents_repeat(client):
    """冷却期内的第二次采集不应重复触发。"""
    seed_and_ingest()
    client.post("/api/alerts/rules",
                json={"rule_type": "price_below", "threshold": 99999, "cooldown_min": 60})
    seed_and_ingest()
    client.get("/api/alerts/pending")          # 清空第一次

    rep = seed_and_ingest()
    assert rep.alerts_triggered == 0, "冷却期未生效"
    assert client.get("/api/alerts/pending").json()["items"] == []


def test_alert_log_filter_by_ammo(client):
    seed_and_ingest()
    client.post("/api/alerts/rules",
                json={"rule_type": "price_below", "threshold": 99999, "cooldown_min": 0})
    seed_and_ingest()
    logs = client.get("/api/alerts/log?ammo_id=ammo_a").json()
    assert len(logs) == 1 and logs[0]["ammo_id"] == "ammo_a"
