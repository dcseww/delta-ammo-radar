# -*- coding: utf-8 -*-
"""聚合的可调用入口。

`ingest` 内部会直接调用 `repository.rebuild_aggregates`，本模块把聚合包装成
一个独立任务，供调度器在采集之外**单独**运行（例如历史回补之后的补算，
或数据修复后的重算）。

为什么聚合要能独立跑
--------------------
聚合是幂等的纯计算（从明细重算），所以它可以在任何需要的时候安全重跑。
把"采集"与"聚合"分开，意味着某个表算错了不需要重新请求外部接口 —— 只重算即可。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.repository import apply_retention, rebuild_aggregates

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AggregateReport:
    buckets_1h: int = 0
    buckets_1d: int = 0
    duration_ms: int = 0

    def summary(self) -> str:
        return (f"聚合完成：小时桶 {self.buckets_1h} 个、日桶 {self.buckets_1d} 个，"
                f"耗时 {self.duration_ms} ms")


def run_aggregation(session: Session, *, since_ts: int = 0) -> AggregateReport:
    """重算 1h / 1d 聚合。"""
    started = time.perf_counter()
    n1h = rebuild_aggregates(session, granularity="1h", since_ts=since_ts)
    n1d = rebuild_aggregates(session, granularity="1d", since_ts=since_ts)
    rep = AggregateReport(buckets_1h=n1h, buckets_1d=n1d,
                          duration_ms=int((time.perf_counter() - started) * 1000))
    log.info(rep.summary())
    return rep


def run_retention(session: Session) -> dict[str, int]:
    """执行保留策略（内部会先补聚合再删明细，顺序见 repository 注释）。"""
    return apply_retention(session)
