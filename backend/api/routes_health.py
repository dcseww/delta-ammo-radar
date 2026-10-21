# -*- coding: utf-8 -*-
"""健康检查与运行状态。

`/api/health` 是排查问题的第一入口，因此它必须回答：
- 数据有多新？是否已经过期？（data_freshness）
- 采集是否还在跑？下一次什么时候跑？连续失败几次？（scheduler）
- 数据源最近成功/失败于何时？（sources）
- 有多少物品、多少价格点、多少被判异？（counts）

刻意**不返回任何密钥**（本路线也不涉及密钥），配置只回显非敏感项。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from backend.config import get_settings
from backend.db import session_scope
from backend.pipeline import ingest as ingest_mod
from backend.pipeline.scheduler import get_scheduler
from backend.repository import (
    count_ammo,
    list_health,
    tick_counts,
)
from backend.schemas import (
    HealthResponse,
    IngestTriggerResponse,
    SourceHealthOut,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    s = get_settings()
    now = int(time.time())

    with session_scope() as session:
        freshness = ingest_mod.data_freshness(session, now)
        ammo_counts = count_ammo(session)
        ticks = tick_counts(session)
        sources = [
            SourceHealthOut(
                source=h.source, last_ok_ts=h.last_ok_ts, last_fail_ts=h.last_fail_ts,
                fail_streak=h.fail_streak or 0, ok_count=h.ok_count or 0,
                fail_count=h.fail_count or 0, last_latency_ms=h.last_latency_ms,
                last_error=h.last_error,
            )
            for h in list_health(session)
        ]

    # ── 总体状态判定 ────────────────────────────────────────────────────
    if not freshness["has_data"]:
        status = "down"
        message = ("还没有任何采集数据。请先运行采集（POST /api/ingest/trigger）"
                   "或等待调度器首次执行。")
    elif freshness["stale"]:
        age_min = (freshness["age_seconds"] or 0) // 60
        status = "degraded"
        message = (f"数据已 {age_min} 分钟未更新（阈值 "
                   f"{s.stale_after_minutes} 分钟），价格可能已过期。")
    else:
        status = "ok"
        age_sec = freshness["age_seconds"] or 0
        message = f"数据正常，最后更新于 {age_sec} 秒前。"

    sched = get_scheduler()
    # 保证下次执行时间是最新的（首次采集可能被回补挡住，`next_run_ts` 会因此为空）
    sched.refresh_next_run()

    if sched.state.consecutive_failures >= sched.FAILURE_ALERT_THRESHOLD:
        if status != "down":
            status = "degraded"
        message += f" 数据源已连续 {sched.state.consecutive_failures} 次采集失败。"

    # 覆盖率从数据库实时算，不依赖内存标志（重启后依然准确）
    try:
        coverage = sched.backfill_coverage()
    except Exception:  # noqa: BLE001
        log.exception("读取回补覆盖率失败")
        coverage = {}

    return HealthResponse(
        status=status,
        message=message,
        now_ts=now,
        data_freshness=freshness,
        ammo_counts=ammo_counts,
        tick_counts=ticks,
        sources=sources,
        scheduler={
            "last_run_ts": sched.state.last_run_ts,
            "last_ok": sched.state.last_ok,
            "last_summary": sched.state.last_summary,
            "consecutive_failures": sched.state.consecutive_failures,
            "next_run_ts": sched.state.next_run_ts,
            "running": sched.state.running,
            "backfill_done": sched.state.backfill_done,
            "backfill_summary": sched.state.backfill_summary,
            "backfill_coverage": coverage,
            "history": sched.state.history[:10],
        },
        config={
            "poll_interval_seconds": s.poll_interval_seconds,
            "stale_after_minutes": s.stale_after_minutes,
            "tick_retention_days": s.tick_retention_days,
            "suspect_jump_pct": s.suspect_jump_pct,
            "timezone": s.timezone,
            "db_path": str(s.db_path),
            "source_search_url": s.source_search_url,
        },
    )


@router.post("/ingest/trigger", response_model=IngestTriggerResponse)
def trigger_ingest() -> IngestTriggerResponse:
    """手动触发一次采集。用于"打开页面就想看到最新价"和排障。

    刻意同步执行：自用场景下一次采集约 1 秒，异步返回任务 ID 的复杂度不值得。
    但必须和后台调度**共用同一把写锁**：SQLite 只能有一个写者，
    否则用户在页面上点「刷新数据」时正好撞上定时采集，就会拿到 database is locked。
    """
    sched = get_scheduler()
    if sched.state.running:
        raise HTTPException(status_code=409, detail="已有一次采集正在执行，请稍后再试")
    if not sched.write_lock.acquire(blocking=False):
        raise HTTPException(status_code=409,
                            detail="历史回补正在写库，请等它完成后再手动采集")

    from backend.db import session_scope as _ss

    try:
        with _ss() as session:
            rep = ingest_mod.ingest_once(session, sched.adapter)
    finally:
        sched.write_lock.release()

    return IngestTriggerResponse(
        ok=rep.ok, summary=rep.summary(), fetched=rep.fetched, written=rep.written,
        unchanged=rep.unchanged, suspect=rep.suspect,
        alerts_triggered=rep.alerts_triggered,
        duration_ms=rep.duration_ms, error=rep.error,
    )


@router.get("/health/pack-check")
def pack_check() -> dict:
    """检查基础表里是否存在打包物项。

    这个接口存在的理由：开发文档 6.3 节的「倒子弹」套利分析需要「包」的价格，
    而本数据源实测**没有**打包弹药。UI 必须据此**如实隐藏**该模块，
    而不是拿缺失的数据算出一个看起来合理的错误数字。

    所以这不是一个装饰性接口，它是"避免展示错误结论"的开关。
    """
    from backend.repository import list_ammo as _la

    with session_scope() as session:
        packs = [a for a in _la(session) if a.is_pack]
        return {
            "has_pack_items": bool(packs),
            "pack_count": len(packs),
            "packs": [{"ammo_id": a.ammo_id, "name": a.name, "pack_size": a.pack_size}
                      for a in packs],
            "message": (
                "检测到打包物项，可以计算包内单发价与散卖价差。"
                if packs else
                "基础表中没有任何打包物项（is_pack=1）。当前数据源只提供散装弹药，"
                "因此『倒子弹』套利分析不可用 —— UI 应当隐藏该模块，而不是显示估算值。"
            ),
        }
