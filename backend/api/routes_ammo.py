# -*- coding: utf-8 -*-
"""子弹列表、详情、价格曲线与排行榜。

设计要点
--------
1. **指标在读取时计算**：本数据源只有 111 个物品、单物品最多几百个点，
   Python 侧现算完全够快（毫秒级），因此不引入指标缓存表 —— 少一张表就少一处不一致。
2. **数据源自带的 7 日统计也一并返回**（`day7_change_pct_from_source`），
   用于与自算值互校：两者差异过大意味着我们对口径的理解有误。
3. **单一价格口径如实标注**：本数据源只有参考价，接口一律带 `price_kind`，
   `note` 字段会明确说明"这是参考价，不是买卖双端报价"。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.config import get_settings
from backend.db import session_scope
from backend.models import PriceAgg1d, PriceAgg1h, PriceTick
from backend.pipeline import metrics as metrics_mod
from backend.repository import (
    global_latest_ts,
    latest_tick_with_source_stats,
    list_ammo,
    list_watchlist,
    price_series,
)
from backend.schemas import (
    AmmoListResponse,
    AmmoRow,
    PricePoint,
    PriceSeriesResponse,
    RankItem,
    RankResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ammo"])

# 排行榜可用类型
RANK_TYPES = ("up", "down", "volatility", "opportunity", "cheap")


def _tz_offset_seconds() -> int:
    s = get_settings()
    return int(datetime.now(s.tz).utcoffset().total_seconds())


def _build_rows(session, *, ammo_filter=None, now_ts: int | None = None) -> list[AmmoRow]:
    """把基础表 + 最新价 + 指标组装成返回行。"""
    settings = get_settings()
    now = now_ts if now_ts is not None else int(time.time())
    tz_off = _tz_offset_seconds()
    watch = set(list_watchlist(session))

    ammos = list_ammo(session)
    if ammo_filter:
        ammos = [a for a in ammos if ammo_filter(a)]

    rows: list[AmmoRow] = []
    for a in ammos:
        series = price_series(session, a.ammo_id, since_ts=now - 31 * 86400)
        row = AmmoRow(
            ammo_id=a.ammo_id, name=a.name, caliber=a.caliber, tier=a.tier,
            grade=a.grade, is_pack=a.is_pack, pack_size=a.pack_size,
            length=a.length, width=a.width, tradeable=a.tradeable, enabled=a.enabled,
            in_watchlist=a.ammo_id in watch,
        )

        if series:
            m = metrics_mod.compute_metrics(series, now, tz_off)
            row.price = m.latest
            row.unit_price = m.latest
            row.price_ts = m.latest_ts
            row.price_age_seconds = max(0, now - m.latest_ts) if m.latest_ts else None
            row.today_change_pct = _round(m.today_change_pct)
            row.day7_change_pct = _round(m.day7_change_pct)
            row.day30_change_pct = _round(m.day30_change_pct)
            row.percentile_7d = _round(m.percentile_7d, 4)
            row.percentile_30d = _round(m.percentile_30d, 4)
            row.percentile_label = metrics_mod.percentile_label(m.percentile_7d)
            row.percentile_samples_7d = m.percentile_samples_7d
            row.percentile_reliable = m.percentile_reliable
            row.high_7d = m.high_7d
            row.low_7d = m.low_7d
            row.drawdown_from_7d_high_pct = _round(m.drawdown_from_7d_high_pct)
            row.ma_24h = _round(m.ma_24h, 2)
            row.sigma_24h = _round(m.sigma_24h, 5)
            row.z_score = _round(m.z_score, 3)
            row.opportunity_score = _round(metrics_mod.opportunity_score(m), 1)
            row.samples = m.samples
            row.notes = list(m.notes)

        # 数据源自带的 7 日指标（字段 7zf）。
        # ⚠️ 实测确认它**不是涨跌百分比**（见 schemas.source_index_7zf 的注释），
        # 因此这里原样返回并明确标注，绝不在 UI 上当作涨跌展示。
        tick = latest_tick_with_source_stats(session, a.ammo_id)
        if tick and tick.raw:
            try:
                raw = json.loads(tick.raw)
                v = raw.get("day7_change_pct")
                row.source_index_7zf = _round(float(v), 2) if v is not None else None
            except (ValueError, TypeError):
                pass

        rows.append(row)
    return rows


def _round(v: float | None, digits: int = 2) -> float | None:
    return None if v is None else round(float(v), digits)


def _sort_rows(rows: list[AmmoRow], sort: str, order: str) -> list[AmmoRow]:
    """排序。缺失值一律排在最后 —— 让"没有数据的物品"不占据榜首。"""
    keymap = {
        "name": lambda r: r.name,
        "price": lambda r: r.price,
        "today_change": lambda r: r.today_change_pct,
        "change_7d": lambda r: r.day7_change_pct,
        "percentile": lambda r: r.percentile_7d,
        "opportunity": lambda r: r.opportunity_score,
        "volatility": lambda r: r.sigma_24h,
        "caliber": lambda r: r.caliber or "",
        "score": lambda r: r.opportunity_score,
    }
    key = keymap.get(sort, keymap["opportunity"])
    reverse = (order == "desc")

    present = [r for r in rows if key(r) is not None]
    missing = [r for r in rows if key(r) is None]
    present.sort(key=key, reverse=reverse)
    return present + missing


@router.get("/ammo", response_model=AmmoListResponse)
def get_ammo(
    q: str | None = Query(None, description="按名称或口径模糊搜索"),
    tier: str | None = Query(None, description="按品质筛选：白/绿/蓝/紫/金/红"),
    caliber: str | None = Query(None, description="按口径精确筛选"),
    only_watch: bool = Query(False, description="只看自选"),
    only_tradeable: bool = Query(True, description="只看可交易（默认为是）"),
    only_enabled: bool = Query(False, description="只看已启用的"),
    sort: str = Query("opportunity", description="排序字段"),
    order: str = Query("desc", description="asc / desc"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> AmmoListResponse:
    """子弹列表 + 最新快照。"""
    s = get_settings()
    now = int(time.time())

    with session_scope() as session:
        def flt(a) -> bool:
            if only_tradeable and not a.tradeable:
                return False
            if only_enabled and not a.enabled:
                return False
            if tier and (a.tier or "") != tier:
                return False
            if caliber and (a.caliber or "") != caliber:
                return False
            if q:
                needle = q.strip().lower()
                hay = f"{a.name} {a.caliber or ''} {a.ammo_id}".lower()
                if needle not in hay:
                    return False
            return True

        rows = _build_rows(session, ammo_filter=flt, now_ts=now)
        if only_watch:
            rows = [r for r in rows if r.in_watchlist]

        rows = _sort_rows(rows, sort, order)
        total = len(rows)
        page = rows[offset:offset + limit]

        last = global_latest_ts(session)
        age = max(0, now - last) if last else None
        return AmmoListResponse(
            updated_at=last,
            stale=(age is None) or age > s.stale_after_minutes * 60,
            age_seconds=age,
            total=total,
            items=page,
        )


@router.get("/ammo/{ammo_id}", response_model=AmmoRow)
def get_ammo_detail(ammo_id: str) -> AmmoRow:
    now = int(time.time())
    with session_scope() as session:
        rows = _build_rows(session, ammo_filter=lambda a: a.ammo_id == ammo_id, now_ts=now)
        if not rows:
            raise HTTPException(status_code=404, detail=f"未找到物品 {ammo_id}")
        return rows[0]


@router.get("/price/{ammo_id}", response_model=PriceSeriesResponse)
def get_price_series(
    ammo_id: str,
    range: str = Query("24h", description="1h / 6h / 24h / 7d / 30d"),
    bucket: str = Query("auto", description="auto / raw / 1h / 1d"),
    price_kind: str = Query("reference"),
) -> PriceSeriesResponse:
    """价格曲线。

    bucket=auto 时按 range 自动选择粒度：短区间用明细，长区间用聚合，
    这样前端不需要关心数据量，接口也不会因为 30 天明细过一次大响应。
    """
    range_seconds = {
        "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400,
    }
    if range not in range_seconds:
        raise HTTPException(status_code=400,
                            detail=f"不支持的 range={range!r}，可选 {list(range_seconds)}")

    if bucket == "auto":
        bucket = "raw" if range in ("1h", "6h", "24h") else "1h"
    if bucket not in ("raw", "1h", "1d"):
        raise HTTPException(status_code=400, detail=f"不支持的 bucket={bucket!r}")

    now = int(time.time())
    since = now - range_seconds[range]

    with session_scope() as session:
        ammo = next((a for a in list_ammo(session) if a.ammo_id == ammo_id), None)
        if ammo is None:
            raise HTTPException(status_code=404, detail=f"未找到物品 {ammo_id}")

        if bucket == "raw":
            pts = price_series(session, ammo_id, since_ts=since, price_kind=price_kind)
            points = [PricePoint(ts=t, price=p) for t, p in pts]
        elif bucket == "1h":
            rows = session.execute(
                select(PriceAgg1h.bucket_ts, PriceAgg1h.close)
                .where(PriceAgg1h.ammo_id == ammo_id, PriceAgg1h.price_kind == price_kind,
                       PriceAgg1h.bucket_ts >= since)
                .order_by(PriceAgg1h.bucket_ts)
            ).all()
            points = [PricePoint(ts=int(t), price=float(c)) for t, c in rows if c is not None]
        else:
            rows = session.execute(
                select(PriceAgg1d.bucket_ts, PriceAgg1d.close)
                .where(PriceAgg1d.ammo_id == ammo_id, PriceAgg1d.price_kind == price_kind,
                       PriceAgg1d.bucket_ts >= since)
                .order_by(PriceAgg1d.bucket_ts)
            ).all()
            points = [PricePoint(ts=int(t), price=float(c)) for t, c in rows if c is not None]

        note = None
        if price_kind == "reference":
            note = ("本数据源只提供单一『参考价』，没有交易行的在售最低价/收购最高价双端报价，"
                    "因此本曲线不能用于计算真实买卖价差。")

        return PriceSeriesResponse(
            ammo_id=ammo_id, name=ammo.name, price_kind=price_kind,
            bucket=bucket, range=range, count=len(points), points=points, note=note,
        )


@router.get("/rank", response_model=RankResponse)
def get_rank(
    type: str = Query("up", description="up / down / volatility / opportunity / cheap"),
    limit: int = Query(20, ge=1, le=100),
) -> RankResponse:
    """排行榜。"""
    if type not in RANK_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"不支持的 type={type!r}，可选 {list(RANK_TYPES)}")
    now = int(time.time())
    with session_scope() as session:
        rows = _build_rows(session, ammo_filter=lambda a: bool(a.tradeable), now_ts=now)

        note = None
        if type == "up":
            rows = [r for r in rows if r.today_change_pct is not None]
            rows.sort(key=lambda r: r.today_change_pct, reverse=True)
            items = [_rank_item(r, r.today_change_pct) for r in rows[:limit]]
        elif type == "down":
            rows = [r for r in rows if r.today_change_pct is not None]
            rows.sort(key=lambda r: r.today_change_pct)
            items = [_rank_item(r, r.today_change_pct) for r in rows[:limit]]
        elif type == "volatility":
            rows = [r for r in rows if r.sigma_24h is not None]
            rows.sort(key=lambda r: r.sigma_24h, reverse=True)
            items = [_rank_item(r, r.sigma_24h) for r in rows[:limit]]
        elif type == "cheap":
            rows = [r for r in rows if r.percentile_7d is not None and r.percentile_reliable]
            rows.sort(key=lambda r: r.percentile_7d)
            items = [_rank_item(r, r.percentile_7d) for r in rows[:limit]]
            note = "只统计分位数样本充足的物品；分位越低表示越接近自身 7 日低位。"
        else:  # opportunity
            rows = [r for r in rows if r.opportunity_score is not None]
            rows.sort(key=lambda r: r.opportunity_score, reverse=True)
            items = [_rank_item(r, r.opportunity_score) for r in rows[:limit]]
            note = ("机会分 = 分位越低加分 + 距 7 日高点回撤越深加分 - 波动越大扣分，"
                    "范围 0~100。这是启发式排序，不是收益预测。")

        return RankResponse(type=type, updated_at=global_latest_ts(session),
                            items=items, note=note)


def _rank_item(r: AmmoRow, value: float | None) -> RankItem:
    return RankItem(
        ammo_id=r.ammo_id, name=r.name, caliber=r.caliber, tier=r.tier,
        price=r.price, value=_round(value, 3),
        today_change_pct=r.today_change_pct,
        percentile_7d=r.percentile_7d, percentile_label=r.percentile_label,
    )
