# -*- coding: utf-8 -*-
"""ORM 模型：与开发文档第 5 章的 DDL 一一对应。

表清单
------
ammo            子弹基础表（由 ammo_catalog.yaml 导入）
price_tick      价格明细（时序主体）
price_agg_1h    小时聚合（长期保留）
price_agg_1d    日聚合（永久保留）
alert_rule      告警规则
alert_log       告警触发历史
source_health   数据源健康状态
watchlist       自选列表

时间约定
--------
- `ts` / `bucket_ts` 一律为 **epoch 秒（UTC 基准）**，避免时区歧义；
- 展示时才按 settings.timezone（默认 Asia/Shanghai）格式化；
- 日聚合的 bucket 对齐到**本地时区**的自然日 00:00。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utc_now_ts() -> int:
    """当前 epoch 秒。全项目统一用它取时间，便于测试注入。"""
    return int(datetime.now(timezone.utc).timestamp())


class Ammo(Base):
    """子弹基础表。来源：backend/catalog/ammo_catalog.yaml（人工核对过的那份）。"""

    __tablename__ = "ammo"

    ammo_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    caliber: Mapped[str | None] = mapped_column(String(60))
    tier: Mapped[str | None] = mapped_column(String(8))          # 白/绿/蓝/紫/金/红
    grade: Mapped[str | None] = mapped_column(String(4))         # 1..6
    is_pack: Mapped[int] = mapped_column(Integer, default=0)     # 1=打包出售
    pack_size: Mapped[int] = mapped_column(Integer, default=1)   # 包内含发数
    length: Mapped[int | None] = mapped_column(Integer, default=1)
    width: Mapped[int | None] = mapped_column(Integer, default=1)
    tradeable: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[int] = mapped_column(Integer, default=1)     # 0=不采集
    note: Mapped[str | None] = mapped_column(Text)

    # 外部数据源 ID，JSON 文本：{"dergame": ["37220000001"]}
    external_ids: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_ammo_enabled", "enabled"),
        Index("ix_ammo_caliber", "caliber"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Ammo {self.ammo_id} {self.name!r}>"


class PriceTick(Base):
    """价格明细。时序主体，也是所有曲线与指标的原始输入。"""

    __tablename__ = "price_tick"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ammo_id: Mapped[str] = mapped_column(ForeignKey("ammo.ammo_id"), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    # 折算后的单发价。本数据源全是散装 1x1，故与 price 相同；
    # 保留该字段是为了将来接入打包物项时不改表结构。
    unit_price: Mapped[float | None] = mapped_column(Float)
    # 价格口径：reference(参考价) / ask_min(在售最低) / bid_max(收购最高)
    price_kind: Mapped[str] = mapped_column(String(16), default="reference", nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[int] = mapped_column(Integer, nullable=False)       # 采集时间
    src_ts: Mapped[int | None] = mapped_column(Integer)            # 数据源自带的更新时间
    raw: Mapped[str | None] = mapped_column(Text)                  # 原始 JSON 片段，便于回溯
    suspect: Mapped[int] = mapped_column(Integer, default=0)       # 1=疑似异常，不进曲线
    # 1 = 来自实时轮询；0 = 来自历史回补。
    # 需要这个字段是因为两者可能落在同一个整点上：回补的是"整点均价"，
    # 实时采集的是"此刻最新价"。没有区分时，后写者会覆盖前者 ——
    # 实测中判异标记就是这样被回补行覆盖掉的（证据悄悄消失）。
    is_live: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        # 唯一索引保证幂等：同一物品+口径+来源+时间戳只保留一条
        UniqueConstraint("ammo_id", "price_kind", "source", "ts", name="ux_tick"),
        Index("ix_tick_lookup", "ammo_id", "price_kind", "ts"),
        Index("ix_tick_ts", "ts"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tick {self.ammo_id} {self.price} @{self.ts}>"


class PriceAgg1h(Base):
    """小时聚合。明细清理后，长期趋势靠它。"""

    __tablename__ = "price_agg_1h"

    ammo_id: Mapped[str] = mapped_column(ForeignKey("ammo.ammo_id"), primary_key=True)
    price_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    bucket_ts: Mapped[int] = mapped_column(Integer, primary_key=True)  # 对齐到整点
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    avg: Mapped[float | None] = mapped_column(Float)
    samples: Mapped[int] = mapped_column(Integer, default=0)


class PriceAgg1d(Base):
    """日聚合。**含 open/high/low，因此 7 日最高/最低可从本地库自算**。"""

    __tablename__ = "price_agg_1d"

    ammo_id: Mapped[str] = mapped_column(ForeignKey("ammo.ammo_id"), primary_key=True)
    price_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    bucket_ts: Mapped[int] = mapped_column(Integer, primary_key=True)  # 本地自然日 00:00
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    avg: Mapped[float | None] = mapped_column(Float)
    samples: Mapped[int] = mapped_column(Integer, default=0)


class AlertRule(Base):
    """告警规则。判定逻辑见 backend/alerts/rules.py（纯函数，可单测）。"""

    __tablename__ = "alert_rule"

    rule_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ammo_id: Mapped[str | None] = mapped_column(String(120))   # NULL = 应用于全部
    rule_type: Mapped[str] = mapped_column(String(24), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    window_min: Mapped[int] = mapped_column(Integer, default=60)
    confirm_min: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_min: Mapped[int] = mapped_column(Integer, default=30)
    channels: Mapped[str] = mapped_column(Text, default='["browser"]')
    quiet_hours: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(Integer, default=utc_now_ts)


class AlertLog(Base):
    """告警触发历史。"""

    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rule.rule_id"))
    ammo_id: Mapped[str | None] = mapped_column(String(120))
    triggered_at: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)         # 触发时的指标快照 JSON
    notified: Mapped[int] = mapped_column(Integer, default=0)
    notify_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_alert_log_time", "triggered_at"),)


class SourceHealth(Base):
    """数据源健康状态。UI 的数据新鲜度横幅依赖它。"""

    __tablename__ = "source_health"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_ok_ts: Mapped[int | None] = mapped_column(Integer)
    last_fail_ts: Mapped[int | None] = mapped_column(Integer)
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)


class Watchlist(Base):
    """自选列表。第一版由 API 维护，前端也能本地持久化一份。"""

    __tablename__ = "watchlist"

    ammo_id: Mapped[str] = mapped_column(ForeignKey("ammo.ammo_id"), primary_key=True)
    added_at: Mapped[int] = mapped_column(Integer, default=utc_now_ts)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
