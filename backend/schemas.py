# -*- coding: utf-8 -*-
"""API 出参模型。

用小写字段名直接对齐前端，避免前端做字段名转换。
所有价格一律同时给出 `price`（原始）与 `unit_price`（折算单发价），
让前端不需要自己算 —— 算错了会成为最难发现的一类 bug。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AmmoRow(BaseModel):
    ammo_id: str
    name: str
    caliber: str | None = None
    tier: str | None = None
    grade: str | None = None
    is_pack: int = 0
    pack_size: int = 1
    length: int | None = None
    width: int | None = None
    tradeable: int = 1
    enabled: int = 1

    # ── 价格 ──────────────────────────────────────────────────────────
    price: float | None = None
    unit_price: float | None = None
    price_kind: str = "reference"
    price_ts: int | None = None
    price_age_seconds: int | None = None

    # ── 指标 ──────────────────────────────────────────────────────────
    today_change_pct: float | None = None
    day7_change_pct: float | None = None
    day30_change_pct: float | None = None
    # 数据源自带的 7 日指标（实测字段名 7zf）。
    # ⚠️ **不要把它当作涨跌百分比**：实测它在价格贴近 7 日最低点时仍可能为正的大数值
    # （例：区间 515~1125、当前价 515，而 7zf=118.45），说明其定义与"涨跌幅"不同。
    # 保留原值仅为透明性，UI 必须标注"数据源自报指标（定义未知）"。
    source_index_7zf: float | None = None
    percentile_7d: float | None = None
    percentile_30d: float | None = None
    percentile_label: str = "无数据"
    percentile_samples_7d: int = 0
    percentile_reliable: bool = False
    high_7d: float | None = None
    low_7d: float | None = None
    drawdown_from_7d_high_pct: float | None = None
    ma_24h: float | None = None
    sigma_24h: float | None = None
    z_score: float | None = None
    opportunity_score: float | None = None
    samples: int = 0

    in_watchlist: bool = False
    notes: list[str] = Field(default_factory=list)


class AmmoListResponse(BaseModel):
    updated_at: int | None = None
    stale: bool = True
    age_seconds: int | None = None
    total: int = 0
    items: list[AmmoRow] = Field(default_factory=list)


class PricePoint(BaseModel):
    ts: int
    price: float


class PriceSeriesResponse(BaseModel):
    ammo_id: str
    name: str = ""
    unit_price_label: str = "元/发"
    price_kind: str = "reference"
    bucket: str = "raw"
    range: str = "24h"
    count: int = 0
    points: list[PricePoint] = Field(default_factory=list)
    note: str | None = None


class RankItem(BaseModel):
    ammo_id: str
    name: str
    caliber: str | None = None
    tier: str | None = None
    price: float | None = None
    value: float | None = None        # 排序依据的值
    today_change_pct: float | None = None
    percentile_7d: float | None = None
    percentile_label: str = "无数据"


class RankResponse(BaseModel):
    type: str
    updated_at: int | None = None
    items: list[RankItem] = Field(default_factory=list)
    note: str | None = None


class AlertRuleIn(BaseModel):
    ammo_id: str | None = None
    rule_type: str
    threshold: float
    window_min: int = 60
    confirm_min: int = 0
    cooldown_min: int = 30
    quiet_hours: str | None = None
    enabled: bool = True


class AlertRuleOut(AlertRuleIn):
    rule_id: int
    created_at: int | None = None
    last_triggered_at: int | None = None


class AlertLogOut(BaseModel):
    id: int
    rule_id: int | None = None
    ammo_id: str | None = None
    ammo_name: str = ""
    rule_type: str = ""
    triggered_at: int
    price: float | None = None
    reason: str = ""
    notified: int = 0
    notify_error: str | None = None
    detail: dict = Field(default_factory=dict)


class PendingAlertsResponse(BaseModel):
    """供前端轮询取走的"浏览器内提醒"。取走即清空，避免重复弹窗。"""

    pending: int = 0
    items: list[AlertLogOut] = Field(default_factory=list)


class SourceHealthOut(BaseModel):
    source: str
    last_ok_ts: int | None = None
    last_fail_ts: int | None = None
    fail_streak: int = 0
    ok_count: int = 0
    fail_count: int = 0
    last_latency_ms: int | None = None
    last_error: str | None = None


class HealthResponse(BaseModel):
    status: str                       # ok / degraded / down
    message: str
    now_ts: int
    data_freshness: dict
    ammo_counts: dict
    tick_counts: dict
    sources: list[SourceHealthOut] = Field(default_factory=list)
    scheduler: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class IngestTriggerResponse(BaseModel):
    ok: bool
    summary: str
    fetched: int = 0
    written: int = 0
    unchanged: int = 0
    suspect: int = 0
    alerts_triggered: int = 0
    duration_ms: int = 0
    error: str | None = None
