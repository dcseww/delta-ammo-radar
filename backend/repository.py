# -*- coding: utf-8 -*-
"""数据访问层（仓储）。

职责边界
--------
- **只做数据存取**：不包含业务判断、不算指标、不发通知。
- **对外暴露领域对象**：查询返回 dataclass 或 dict，而不是 ORM 实体，
  避免 ORM 会话泄露到上层，也让 API 层与测试更好写。

关于幂等
--------
`price_tick` 上有唯一约束 (ammo_id, price_kind, source, ts)。
写入用 SQLite 的 `INSERT ... ON CONFLICT DO UPDATE`，因此：
- 同一时刻重复采集 → 覆盖，不产生重复点；
- 修订值（数据源回填了修正价）→ 自动更新，不需要额外逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.models import (
    AlertLog,
    AlertRule,
    Ammo,
    PriceAgg1d,
    PriceAgg1h,
    PriceTick,
    SourceHealth,
    Watchlist,
    utc_now_ts,
)

log = logging.getLogger(__name__)


# ── 基础表 ──────────────────────────────────────────────────────────────
@dataclass(slots=True)
class AmmoRecord:
    ammo_id: str
    name: str
    caliber: str | None
    tier: str | None
    grade: str | None
    is_pack: int
    pack_size: int
    length: int | None
    width: int | None
    tradeable: int
    enabled: int


def upsert_ammo(session: Session, records: Iterable[AmmoRecord]) -> int:
    """导入/更新基础表。返回写入条数。

    注意：**不覆盖 enabled 字段**。enabled 是用户的手工选择，
    每次重新导入基础表都把它重置会让用户的取舍丢失。
    """
    n = 0
    for r in records:
        row = session.get(Ammo, r.ammo_id)
        if row is None:
            session.add(Ammo(
                ammo_id=r.ammo_id, name=r.name, caliber=r.caliber, tier=r.tier,
                grade=r.grade, is_pack=r.is_pack, pack_size=r.pack_size,
                length=r.length, width=r.width, tradeable=r.tradeable,
                enabled=r.enabled,
            ))
        else:
            row.name = r.name
            row.caliber = r.caliber
            row.tier = r.tier
            row.grade = r.grade
            row.is_pack = r.is_pack
            row.pack_size = r.pack_size
            row.length = r.length
            row.width = r.width
            row.tradeable = r.tradeable
            # row.enabled 刻意不动
        n += 1
    return n


def list_ammo(session: Session, *, enabled_only: bool = False,
              tradeable_only: bool = False) -> list[Ammo]:
    stmt = select(Ammo)
    if enabled_only:
        stmt = stmt.where(Ammo.enabled == 1)
    if tradeable_only:
        stmt = stmt.where(Ammo.tradeable == 1)
    return list(session.scalars(stmt.order_by(Ammo.caliber, Ammo.name)))


def count_ammo(session: Session) -> dict[str, int]:
    total = session.scalar(select(func.count()).select_from(Ammo)) or 0
    enabled = session.scalar(
        select(func.count()).select_from(Ammo).where(Ammo.enabled == 1)) or 0
    tradeable = session.scalar(
        select(func.count()).select_from(Ammo).where(Ammo.tradeable == 1)) or 0
    return {"total": total, "enabled": enabled, "tradeable": tradeable}


def set_ammo_enabled(session: Session, ammo_id: str, enabled: bool) -> bool:
    row = session.get(Ammo, ammo_id)
    if row is None:
        return False
    row.enabled = 1 if enabled else 0
    return True


# ── 外部 ID 解析 ────────────────────────────────────────────────────────
def build_external_id_map(session: Session, source: str) -> dict[str, str]:
    """构造 {外部ID: ammo_id} 映射，用于把数据源的记录对应到本系统的物品。

    为什么必须显式做这一步：数据源给的 id 是它自己的编号（如 "37220000001"），
    与我们的 ammo_id 完全不同。没有这层解析，价格就无法归属到正确的物品上。
    """
    import json

    mapping: dict[str, str] = {}
    for row in session.scalars(select(Ammo)):
        if not row.external_ids:
            continue
        try:
            ext = json.loads(row.external_ids)
        except (ValueError, TypeError):
            continue
        ids = ext.get(source) or []
        if isinstance(ids, str):
            ids = [ids]
        # variants 里也带 id，一并纳入
        for v in ext.get("variants") or []:
            if isinstance(v, dict) and v.get("id"):
                ids = list(ids) + [str(v["id"])]
        for i in ids:
            mapping[str(i)] = row.ammo_id
    return mapping


# ── 价格明细 ────────────────────────────────────────────────────────────
@dataclass(slots=True)
class TickWrite:
    ammo_id: str
    price: float
    unit_price: float
    price_kind: str
    source: str
    ts: int
    src_ts: int | None = None
    raw: str | None = None
    suspect: int = 0
    is_live: int = 0


def upsert_ticks(session: Session, ticks: Sequence[TickWrite]) -> int:
    """幂等批量写入价格明细。

    冲突处理刻意做了一处不对称：**实时行不会被回补行覆盖**。
    两者可能落在同一个整点上（回补给的是整点均价，实时给的是此刻最新价），
    若无条件覆盖，判异标记这类只存在于实时行的证据会被悄悄抹掉 —— 实测发生过。
    """
    if not ticks:
        return 0
    rows = [
        {
            "ammo_id": t.ammo_id, "price": t.price, "unit_price": t.unit_price,
            "price_kind": t.price_kind, "source": t.source, "ts": t.ts,
            "src_ts": t.src_ts, "raw": t.raw, "suspect": t.suspect,
            "is_live": t.is_live,
        }
        for t in ticks
    ]
    stmt = sqlite_insert(PriceTick).values(rows)
    # CASE WHEN：已有行是实时行（is_live=1）而新行是回补行（is_live=0）时保留原值。
    # ⚠️ 每个要保护的列都必须显式套上这个 CASE —— 漏掉一个就会被静默覆盖。
    # 初版就漏了 is_live 本身，导致价格保住了、来源标记却被改掉，
    # 于是下一次回补又会认为"这是历史行"，保护随之失效（测试抓到过）。
    keep_existing = and_(PriceTick.is_live == 1, stmt.excluded.is_live == 0)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ammo_id", "price_kind", "source", "ts"],
        set_={
            "price": case((keep_existing, PriceTick.price), else_=stmt.excluded.price),
            "unit_price": case((keep_existing, PriceTick.unit_price),
                               else_=stmt.excluded.unit_price),
            "src_ts": case((keep_existing, PriceTick.src_ts), else_=stmt.excluded.src_ts),
            "raw": case((keep_existing, PriceTick.raw), else_=stmt.excluded.raw),
            "suspect": case((keep_existing, PriceTick.suspect), else_=stmt.excluded.suspect),
            "is_live": case((keep_existing, PriceTick.is_live), else_=stmt.excluded.is_live),
        },
    )
    session.execute(stmt)
    return len(rows)


def latest_live_tick(session: Session, ammo_id: str, price_kind: str = "reference",
                     source: str | None = None) -> PriceTick | None:
    """取某物品最近一条**实时行**。

    用于"数据源是否真的更新了"的判断：必须同时比对上一次实时行的
    `src_ts` **与价格**，才能区分两种情况：

    - 数据源没刷新 → 应跳过写入，避免重复点与数据库膨胀；
    - 同一分钟内价格变了 → 必须写入，否则会丢掉真实的价格变化。

    只比 `src_ts` 会把第二种情况误判成"没更新"（测试抓到过）。
    """
    stmt = (select(PriceTick)
            .where(PriceTick.ammo_id == ammo_id,
                   PriceTick.price_kind == price_kind,
                   PriceTick.is_live == 1)
            .order_by(PriceTick.ts.desc(), PriceTick.id.desc())
            .limit(1))
    if source:
        stmt = stmt.where(PriceTick.source == source)
    return session.scalars(stmt).first()


def latest_tick(session: Session, ammo_id: str, price_kind: str = "reference",
                include_suspect: bool = False) -> PriceTick | None:
    """取某物品最新一条记录。

    排序刻意用 (ts DESC, id DESC) 而不是只按 ts：
    同一 (ammo_id, price_kind, ts) 可能被不同来源各写一次 —— 例如历史回补与
    实时采集都落在同一个整点。此时"ts 最大"无法确定取到哪一条，
    结果取决于物理行序（不确定性）。加上 id DESC 后，**最后写入者胜**，
    行为可预测、可复现。

    `include_suspect=True` 时连判异记录一起返回，用于排查"当时到底收到了什么"。
    """
    stmt = (select(PriceTick)
            .where(PriceTick.ammo_id == ammo_id,
                   PriceTick.price_kind == price_kind)
            .order_by(PriceTick.ts.desc(), PriceTick.id.desc())
            .limit(1))
    if not include_suspect:
        stmt = stmt.where(PriceTick.suspect == 0)
    return session.scalars(stmt).first()


def price_series(session: Session, ammo_id: str, *, since_ts: int,
                 price_kind: str = "reference", include_suspect: bool = False
                 ) -> list[tuple[int, float]]:
    """取价格序列 [(ts, price)]，升序。这是所有指标计算的输入。"""
    stmt = select(PriceTick.ts, PriceTick.price).where(
        PriceTick.ammo_id == ammo_id,
        PriceTick.price_kind == price_kind,
        PriceTick.ts >= since_ts,
    )
    if not include_suspect:
        stmt = stmt.where(PriceTick.suspect == 0)
    return [(int(ts), float(p)) for ts, p in session.execute(stmt.order_by(PriceTick.ts))]


def latest_tick_with_source_stats(session: Session, ammo_id: str,
                                  price_kind: str = "reference") -> PriceTick | None:
    """取最近一条**带数据源自报统计**的记录。

    为什么单独需要这个查询：
    历史回补与实时采集可能落在同一个时间戳上（回补的是整点，采集是当前时刻）。
    此时"最新一条"取决于写入顺序，可能取到回补写的那条 —— 它只有
    `_origin`，没有数据源自报的 7 日涨跌。于是"自算 vs 源报"的互校字段
    会时有时无，看起来像随机故障。

    这里显式挑出真正带源报字段的那条，与写入顺序解耦。
    """
    return session.scalars(
        select(PriceTick)
        .where(PriceTick.ammo_id == ammo_id,
               PriceTick.price_kind == price_kind,
               PriceTick.suspect == 0,
               PriceTick.raw.like('%"day7_change_pct": %'))
        .order_by(PriceTick.ts.desc(), PriceTick.id.desc())
        .limit(1)
    ).first()


def backfill_coverage(session: Session, source: str = "dergame",
                      min_points: int = 24) -> dict:
    """统计历史回补覆盖率：有多少可交易物品已经有足够的历史点。

    **为什么必须从数据库算，而不是记在内存里**
    ------------------------------------------
    初版把"回补是否完成"只记在调度器的内存标志里，结果有两个真实后果：
    1. 服务一重启标志就丢，健康接口永远显示"未完成"；
    2. 更糟的是**整批重跑** —— 重启后会把已经回补好的 83 个物品全部重拉一遍，
       白白浪费 2 分钟和 83 次对外请求。

    覆盖率是可从数据推导的事实，就应该从数据读，而不是维护一个易失的副本。

    `min_points` 默认 24：少于这个数量不足以算分位数（见 metrics.MIN_TRUSTWORTHY_SAMPLES）。
    """
    from sqlalchemy import func as _f

    total = session.scalar(
        select(_f.count()).select_from(Ammo)
        .where(Ammo.tradeable == 1, Ammo.enabled == 1)) or 0

    covered = session.scalar(
        select(_f.count()).select_from(
            select(PriceTick.ammo_id)
            .join(Ammo, Ammo.ammo_id == PriceTick.ammo_id)
            .where(Ammo.tradeable == 1, Ammo.enabled == 1,
                   PriceTick.suspect == 0, PriceTick.source == source)
            .group_by(PriceTick.ammo_id)
            .having(_f.count() >= min_points)
            .subquery()
        )) or 0

    return {
        "total": int(total),
        "covered": int(covered),
        "missing": int(total - covered),
        "complete": total > 0 and covered >= total,
        "min_points": min_points,
    }


def items_missing_history(session: Session, source: str = "dergame",
                          min_points: int = 24) -> list[str]:
    """返回还没有足够历史点的 ammo_id 列表。

    回补任务据此**只补缺失的物品**，从而让整批任务天然可断点续跑：
    中断后重启只处理剩下的，而不是从头再来。
    """
    from sqlalchemy import func as _f

    rows = session.execute(
        select(PriceTick.ammo_id, _f.count().label("n"))
        .where(PriceTick.suspect == 0, PriceTick.source == source)
        .group_by(PriceTick.ammo_id)
    ).all()
    have = {a for a, n in rows if n >= min_points}

    all_tradeable = session.scalars(
        select(Ammo.ammo_id).where(Ammo.tradeable == 1, Ammo.enabled == 1)).all()
    return [a for a in all_tradeable if a not in have]


def latest_tick_ts(session: Session, ammo_id: str) -> int | None:
    v = session.scalar(
        select(func.max(PriceTick.ts)).where(PriceTick.ammo_id == ammo_id))
    return int(v) if v is not None else None


def global_latest_ts(session: Session) -> int | None:
    v = session.scalar(select(func.max(PriceTick.ts)))
    return int(v) if v is not None else None


def tick_counts(session: Session) -> dict[str, int]:
    total = session.scalar(select(func.count()).select_from(PriceTick)) or 0
    suspect = session.scalar(
        select(func.count()).select_from(PriceTick).where(PriceTick.suspect == 1)) or 0
    return {"total": total, "suspect": suspect, "valid": total - suspect}


# ── 聚合 ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class BucketRow:
    ammo_id: str
    price_kind: str
    bucket_ts: int
    open: float
    high: float
    low: float
    close: float
    avg: float
    samples: int


def rebuild_aggregates(session: Session, *, granularity: str = "1h",
                       since_ts: int = 0) -> int:
    """按明细重算聚合表（幂等：先删后写指定区间）。

    granularity: "1h" 对齐整点；"1d" 对齐**本地时区**的自然日 00:00。
    刻意用 Python 侧分桶而不是 SQL 的 strftime，因为日聚合必须按本地时区切，
    而 SQLite 的日期函数默认按 UTC，容易造成"今日涨跌"错一天。
    """
    s = get_settings()
    tz_off = int(datetime.now(s.tz).utcoffset().total_seconds())
    bucket_seconds = 3600 if granularity == "1h" else 86400

    def bucket_of(ts: int) -> int:
        shifted = ts + tz_off
        return (shifted // bucket_seconds) * bucket_seconds - tz_off

    rows = session.execute(
        select(PriceTick.ammo_id, PriceTick.price_kind, PriceTick.ts, PriceTick.price)
        .where(PriceTick.suspect == 0, PriceTick.ts >= since_ts)
        .order_by(PriceTick.ts)
    ).all()

    grouped: dict[tuple[str, str, int], list[float]] = {}
    for ammo_id, kind, ts, price in rows:
        grouped.setdefault((ammo_id, kind, bucket_of(int(ts))), []).append(float(price))

    if not grouped:
        return 0

    model = PriceAgg1h if granularity == "1h" else PriceAgg1d

    # 只重算本次涉及的 bucket，避免全表重写
    for (ammo_id, kind, bucket_ts) in grouped:
        session.execute(delete(model).where(
            model.ammo_id == ammo_id, model.price_kind == kind,
            model.bucket_ts == bucket_ts))

    payload = []
    for (ammo_id, kind, bucket_ts), prices in grouped.items():
        payload.append({
            "ammo_id": ammo_id, "price_kind": kind, "bucket_ts": bucket_ts,
            "open": prices[0], "high": max(prices), "low": min(prices),
            "close": prices[-1], "avg": sum(prices) / len(prices),
            "samples": len(prices),
        })

    # 分批插入，避免单条 SQL 参数过多
    for i in range(0, len(payload), 500):
        session.execute(sqlite_insert(model).values(payload[i:i + 500]))
    return len(payload)


def daily_high_low(session: Session, ammo_id: str, days: int, now_ts: int
                   ) -> tuple[float | None, float | None]:
    """从日聚合取近 N 日的最高/最低价。用于与数据源自带的 7 日高低互校。"""
    since = now_ts - days * 86400
    row = session.execute(
        select(func.max(PriceAgg1d.high), func.min(PriceAgg1d.low))
        .where(PriceAgg1d.ammo_id == ammo_id, PriceAgg1d.bucket_ts >= since)
    ).one()
    return (float(row[0]) if row[0] is not None else None,
            float(row[1]) if row[1] is not None else None)


def apply_retention(session: Session, now_ts: int | None = None) -> dict[str, int]:
    """按保留策略清理数据。

    顺序很重要：**必须先聚合再删明细**，否则被删区间的小时聚合就永远补不回来了。
    因此本函数先调用 rebuild_aggregates 覆盖待删区间，再执行删除。
    """
    s = get_settings()
    now = now_ts if now_ts is not None else utc_now_ts()
    cutoff = now - s.tick_retention_days * 86400

    # 先补聚合（覆盖即将被删的区间）
    rebuild_aggregates(session, granularity="1h", since_ts=0)
    rebuild_aggregates(session, granularity="1d", since_ts=0)

    deleted_ticks = session.execute(
        delete(PriceTick).where(PriceTick.ts < cutoff)).rowcount or 0

    agg_cutoff = now - s.agg_1h_retention_days * 86400
    deleted_agg = session.execute(
        delete(PriceAgg1h).where(PriceAgg1h.bucket_ts < agg_cutoff)).rowcount or 0

    log.info("保留策略执行完毕：删除明细 %d 条、小时聚合 %d 条", deleted_ticks, deleted_agg)
    return {"ticks_deleted": deleted_ticks, "agg_1h_deleted": deleted_agg}


# ── 健康状态 ────────────────────────────────────────────────────────────
def record_health(session: Session, source: str, *, ok: bool, latency_ms: int | None = None,
                  error: str | None = None, at_ts: int | None = None) -> SourceHealth:
    ts = at_ts if at_ts is not None else utc_now_ts()
    row = session.get(SourceHealth, source)
    if row is None:
        row = SourceHealth(source=source)
        session.add(row)
    if ok:
        row.last_ok_ts = ts
        row.fail_streak = 0
        row.ok_count = (row.ok_count or 0) + 1
        row.last_latency_ms = latency_ms
        row.last_error = None
    else:
        row.last_fail_ts = ts
        row.fail_streak = (row.fail_streak or 0) + 1
        row.fail_count = (row.fail_count or 0) + 1
        row.last_error = (error or "")[:500]
    return row


def list_health(session: Session) -> list[SourceHealth]:
    return list(session.scalars(select(SourceHealth)))


# ── 告警 ────────────────────────────────────────────────────────────────
def list_rules(session: Session, enabled_only: bool = False) -> list[AlertRule]:
    stmt = select(AlertRule)
    if enabled_only:
        stmt = stmt.where(AlertRule.enabled == 1)
    return list(session.scalars(stmt.order_by(AlertRule.rule_id)))


def last_triggered_map(session: Session) -> dict[int, int]:
    """{rule_id: 最近触发时间}，供规则冷却判定使用。"""
    rows = session.execute(
        select(AlertLog.rule_id, func.max(AlertLog.triggered_at))
        .where(AlertLog.rule_id.is_not(None))
        .group_by(AlertLog.rule_id)
    ).all()
    return {int(r): int(t) for r, t in rows if r is not None}


def insert_alert_log(session: Session, *, rule_id: int | None, ammo_id: str | None,
                     triggered_at: int, price: float | None, detail: str,
                     notified: int = 0, notify_error: str | None = None) -> AlertLog:
    row = AlertLog(rule_id=rule_id, ammo_id=ammo_id, triggered_at=triggered_at,
                   price=price, detail=detail, notified=notified,
                   notify_error=notify_error)
    session.add(row)
    return row


def list_alert_logs(session: Session, limit: int = 100, ammo_id: str | None = None
                    ) -> list[AlertLog]:
    stmt = select(AlertLog)
    if ammo_id:
        stmt = stmt.where(AlertLog.ammo_id == ammo_id)
    return list(session.scalars(stmt.order_by(AlertLog.triggered_at.desc()).limit(limit)))


# ── 自选 ────────────────────────────────────────────────────────────────
def list_watchlist(session: Session) -> list[str]:
    return list(session.scalars(
        select(Watchlist.ammo_id).order_by(Watchlist.sort_order, Watchlist.added_at)))


def add_watch(session: Session, ammo_id: str) -> bool:
    if session.get(Watchlist, ammo_id) is not None:
        return False
    session.add(Watchlist(ammo_id=ammo_id, added_at=utc_now_ts()))
    return True


def remove_watch(session: Session, ammo_id: str) -> bool:
    row = session.get(Watchlist, ammo_id)
    if row is None:
        return False
    session.delete(row)
    return True
