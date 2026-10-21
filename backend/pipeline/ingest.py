# -*- coding: utf-8 -*-
"""采集编排：一次采集从"取数"到"入库、聚合、判规则"的完整流程。

流程
----
    adapter.fetch_quotes()
        ↓  原始报价
    解析外部 ID → 本系统 ammo_id      （没有这层，价格无法归属）
        ↓
    validate_quote()                  （负价、未来时间、跳变判异）
        ↓
    upsert_ticks()                    （幂等，suspect 记录也入库但标记）
        ↓
    rebuild_aggregates()              （1h / 1d）
        ↓
    evaluate_all() → notify → alert_log
        ↓
    record_health()                   （供 UI 显示数据新鲜度）

设计原则
--------
1. **采集失败不抛异常**：返回带 ok=False 的报告，让调度器决定退避与告警。
   一次网络抖动不该让整个服务进程崩掉。
2. **单条脏数据不中断整批**：无法解析的记录跳过并计数，最后汇总报告。
3. **suspect 记录照常入库**：不参与曲线与告警，但保留原始证据，
   便于事后排查"当时到底收到了什么"。
4. **纯函数化的判定**：规则判定在 alerts.rules 里，本模块只负责编排。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.alerts import notify as notify_mod
from backend.alerts.rules import RuleSpec, evaluate_all, sustained
from backend.config import get_settings
from backend.pipeline import metrics as metrics_mod
from backend.pipeline.validate import validate_quote
from backend.repository import (
    build_external_id_map,
    global_latest_ts,
    insert_alert_log,
    items_missing_history,
    last_triggered_map,
    latest_live_tick,
    list_ammo,
    list_rules,
    price_series,
    record_health,
    rebuild_aggregates,
    upsert_ticks,
    TickWrite,
)
from backend.sources.base import HistoryPoint, SourceAdapter

log = logging.getLogger(__name__)

# suspect 记录在 raw 里保留的原因标签，便于事后分析为什么被拒
SOURCE_NAME_FALLBACK = "dergame"


@dataclass(slots=True)
class IngestReport:
    """一次采集的结构化结果。调度器与 CLI 都消费它。"""

    ok: bool = False
    source: str = ""
    fetched: int = 0            # 数据源返回的弹药条数
    resolved: int = 0           # 成功对应到本系统物品的条数
    unresolved: int = 0         # 对应不上（基础表里没有）的条数
    written: int = 0            # 实际写入（含覆盖）的条数
    unchanged: int = 0          # 数据源未更新因而跳过的条数
    suspect: int = 0            # 被判异标记的条数
    latency_ms: int = 0
    src_update_time: str | None = None
    alerts_triggered: int = 0
    unresolved_ids: list[str] = field(default_factory=list)
    suspect_reasons: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0

    def summary(self) -> str:
        if not self.ok:
            return f"采集失败：{self.error}"
        tail = f"，跳过未更新 {self.unchanged} 条" if self.unchanged else ""
        return (f"采集成功：拉取 {self.fetched} 条 → 归属 {self.resolved} 条 → "
                f"写入 {self.written} 条（判异 {self.suspect} 条）{tail}，"
                f"触发告警 {self.alerts_triggered} 条，耗时 {self.duration_ms} ms")


def local_minute_of_day(ts: int, tz_offset_seconds: int) -> int:
    """把 epoch 秒转成"本地时间的当日第几分钟"（0~1439），用于静默时段判断。"""
    shifted = ts + tz_offset_seconds
    return (shifted % 86400) // 60


def ingest_once(
    session: Session,
    adapter: SourceAdapter,
    *,
    now_ts: int | None = None,
    run_alerts: bool = True,
) -> IngestReport:
    """执行一次完整采集。

    `now_ts` 可注入，便于测试构造确定的时间场景。
    """
    settings = get_settings()
    started = time.perf_counter()
    now = now_ts if now_ts is not None else int(time.time())
    tz_off = int(datetime.now(settings.tz).utcoffset().total_seconds())

    report = IngestReport(source=getattr(adapter, "name", SOURCE_NAME_FALLBACK))

    # ── 1. 取数 ─────────────────────────────────────────────────────────
    result = adapter.fetch_quotes()
    report.latency_ms = result.latency_ms
    report.src_update_time = result.src_update_time

    if not result.ok:
        report.error = result.error
        record_health(session, report.source, ok=False,
                      latency_ms=result.latency_ms, error=result.error, at_ts=now)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        log.warning("采集失败（%s）：%s", report.source, result.error)
        return report

    report.fetched = result.count
    record_health(session, report.source, ok=True,
                  latency_ms=result.latency_ms, at_ts=now)

    # ── 2. 外部 ID → 内部 ammo_id ───────────────────────────────────────
    id_map = build_external_id_map(session, report.source)
    if not id_map:
        report.error = ("基础表里没有任何该数据源的 external_ids。"
                        "请先运行：python scripts/seed_catalog.py")
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    # 只采 enabled 的物品（用户可以把不关心的置 0）
    enabled_ids = {a.ammo_id for a in list_ammo(session, enabled_only=True)}
    suspect_threshold = settings.suspect_jump_pct
    stale_seconds = settings.stale_after_minutes * 60

    ticks: list[TickWrite] = []
    for q in result.quotes:
        external_id = str(q.raw.get("id") or "")
        ammo_id = id_map.get(external_id)
        if ammo_id is None:
            report.unresolved += 1
            if len(report.unresolved_ids) < 20:
                report.unresolved_ids.append(external_id)
            continue
        report.resolved += 1

        if ammo_id not in enabled_ids:
            continue      # 用户已停用该物品

        # ★ 数据源未更新就跳过，不重复写同一份数据。
        # 判定条件是「src_ts 与上次相同 **且** 价格也没变」—— 两个都要满足。
        #
        # 只比 src_ts 是错的：数据源的 updateTime 是分钟级，同一分钟内价格仍可能变动，
        # 那时只比 src_ts 会把真实的价格变化当成"没更新"丢掉（测试抓到过）。
        # 只比价格也是错的：那会在数据源完全没刷新时反复写入同样的点，让数据库膨胀。
        if q.src_ts is not None:
            prev_live = latest_live_tick(session, ammo_id, q.price_kind, q.source)
            if (prev_live is not None
                    and prev_live.src_ts is not None
                    and q.src_ts <= prev_live.src_ts
                    and abs((prev_live.price or 0) - float(q.price)) < 1e-9):
                report.unchanged += 1
                continue

        # 上一次有效采集（用于跳变判异）
        prev = session.execute(
            text(
                "SELECT price, ts FROM price_tick "
                "WHERE ammo_id=:a AND price_kind=:k AND suspect=0 "
                "ORDER BY ts DESC LIMIT 1"),
            {"a": ammo_id, "k": q.price_kind},
        ).first()

        v = validate_quote(
            price=q.price, ts=now,
            now_ts=now,
            prev_price=float(prev[0]) if prev else None,
            prev_ts=int(prev[1]) if prev else None,
            jump_threshold_pct=suspect_threshold,
            stale_after_seconds=stale_seconds,
        )

        if v.suspect:
            report.suspect += 1
            report.suspect_reasons[v.reason] = report.suspect_reasons.get(v.reason, 0) + 1
            reason_note = v.describe()

        raw_payload = dict(q.raw)
        raw_payload["_suspect_reason"] = None if v.ok else v.reason

        ticks.append(TickWrite(
            ammo_id=ammo_id,
            price=float(q.price),
            unit_price=float(q.unit_price or q.price),
            price_kind=q.price_kind,
            source=q.source,
            ts=now,
            src_ts=q.src_ts,
            raw=json.dumps(raw_payload, ensure_ascii=False),
            suspect=0 if v.ok else 1,
            is_live=1,          # 实时行：不会被后续回补行覆盖
        ))

    report.written = upsert_ticks(session, ticks)
    session.flush()

    # ── 3. 聚合 ─────────────────────────────────────────────────────────
    rebuild_aggregates(session, granularity="1h", since_ts=now - 2 * 86400)
    rebuild_aggregates(session, granularity="1d", since_ts=now - 32 * 86400)

    # ── 4. 告警判定 ─────────────────────────────────────────────────────
    if run_alerts and settings.alert_enabled:
        report.alerts_triggered = _run_alert_rules(session, now, tz_off)

    report.ok = True
    report.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(report.summary())
    return report


def _run_alert_rules(session: Session, now_ts: int, tz_off: int) -> int:
    """判定所有启用的规则，写日志并投递通知。返回触发条数。"""
    settings = get_settings()
    rules = list_rules(session, enabled_only=True)
    if not rules:
        return 0

    last_map = last_triggered_map(session)
    specs: list[RuleSpec] = []
    for r in rules:
        specs.append(RuleSpec(
            rule_id=r.rule_id,
            ammo_id=r.ammo_id,
            rule_type=r.rule_type,
            threshold=float(r.threshold),
            window_min=r.window_min or 60,
            confirm_min=r.confirm_min or 0,
            cooldown_min=r.cooldown_min or settings.alert_cooldown_minutes,
            quiet_hours=r.quiet_hours if r.quiet_hours is not None else settings.alert_quiet_hours,
            enabled=bool(r.enabled),
            last_triggered_at=last_map.get(r.rule_id),
        ))

    # 只算涉及的物品，避免全表计算
    target_ids = {s.ammo_id for s in specs if s.ammo_id}
    if not target_ids:
        target_ids = {a.ammo_id for a in list_ammo(session, enabled_only=True)}

    metrics_by_ammo = {}
    for ammo_id in target_ids:
        series = price_series(session, ammo_id, since_ts=now_ts - 31 * 86400)
        if series:
            metrics_by_ammo[ammo_id] = metrics_mod.compute_metrics(series, now_ts, tz_off)

    minute_of_day = local_minute_of_day(now_ts, tz_off)
    triggered = evaluate_all(specs, metrics_by_ammo, now_ts=now_ts,
                             local_minute_of_day=minute_of_day,
                             default_cooldown_min=settings.alert_cooldown_minutes)

    # 持续确认过滤（需要序列，单独处理）
    confirmed = []
    for ev in triggered:
        spec = next((s for s in specs if s.rule_id == ev.rule_id), None)
        if spec and spec.confirm_min > 0 and ev.ammo_id:
            tail = price_series(session, ev.ammo_id, since_ts=now_ts - spec.confirm_min * 60 * 2)
            if not sustained(tail, spec.rule_type, spec.threshold, spec.confirm_min, now_ts):
                continue
        confirmed.append(ev)

    ammo_names = {a.ammo_id: a.name for a in list_ammo(session)}
    n = 0
    for ev in confirmed:
        n += 1
        note = notify_mod.Notification(
            ammo_id=ev.ammo_id or "",
            ammo_name=ammo_names.get(ev.ammo_id or "", ""),
            rule_id=ev.rule_id,
            rule_type=ev.rule_type,
            price=ev.price,
            reason=ev.reason,
            triggered_at=now_ts,
            detail=ev.detail,
        )
        errors = notify_mod.dispatch(note, ["browser"])
        insert_alert_log(
            session, rule_id=ev.rule_id, ammo_id=ev.ammo_id, triggered_at=now_ts,
            price=ev.price,
            detail=json.dumps({"reason": ev.reason, **ev.detail}, ensure_ascii=False),
            notified=0 if errors else 1,
            notify_error="; ".join(errors) if errors else None,
        )
    return n


# ── 历史回补 ────────────────────────────────────────────────────────────
@dataclass(slots=True)
class BackfillReport:
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    points_written: int = 0
    skipped_no_external_id: int = 0
    duration_ms: int = 0

    def summary(self) -> str:
        return (f"历史回补：请求 {self.requested} 个物品 → 成功 {self.succeeded}、"
                f"失败 {self.failed}、跳过 {self.skipped_no_external_id}，"
                f"写入 {self.points_written} 个价格点，耗时 {self.duration_ms} ms")


def backfill_history(
    session: Session,
    adapter: SourceAdapter,
    *,
    limit: int | None = None,
    interval_seconds: float | None = None,
    sleep_fn=time.sleep,
    progress_every: int = 10,
    only_missing: bool = True,
) -> BackfillReport:
    """回补历史价格曲线。

    这是本项目的一个重要优势：数据源直接提供 7 天逐小时历史，
    因此**首次启动即可拥有分位数分析所需的样本**，不必等采集攒够天数。

    四条工程约束（都是踩过坑才加的）
    --------------------------------
    1. **间隔必须短**：初版把间隔设为 90 秒，83 个物品要跑 2 小时，
       而且长时间持有写锁，采集全部被挡住。现在默认 1.5 秒，整批约 2 分钟。
    2. **逐项提交**：每处理完一个物品就 commit，把写锁切成很多短持有，
       否则采集会撞 `database is locked`。
    3. **进度日志**：每 `progress_every` 个物品打一条日志。
       没有进度日志时，"卡住了"和"慢慢在跑"完全无法区分。
    4. **只补缺失（only_missing）**：只处理还没有足够历史点的物品，
       因此整批任务**天然可断点续跑**。初版每次都全量重跑，
       重启一次就白白重拉 83 个物品（实测踩到）。
    """
    settings = get_settings()
    interval = (interval_seconds if interval_seconds is not None
                else settings.backfill_interval_seconds)
    started = time.perf_counter()
    report = BackfillReport()

    import json as _json

    ammos = [a for a in list_ammo(session, enabled_only=True, tradeable_only=True)
             if a.external_ids]

    if only_missing:
        # 用数据库里的实际覆盖情况决定要补哪些 —— 不依赖任何内存标志，
        # 所以服务重启后能准确接着上次的进度继续。
        missing = set(items_missing_history(session, adapter.name))
        ammos = [a for a in ammos if a.ammo_id in missing]

    if limit is not None:
        ammos = ammos[:limit]

    total = len(ammos)
    if total == 0:
        log.info("历史回补：所有可交易物品都已有足够历史点，无需回补")
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        return report

    log.info("开始历史回补：%d 个物品待补，间隔 %.1fs，预计约 %.1f 分钟",
             total, interval, total * interval / 60)

    for i, a in enumerate(ammos, 1):
        ext = None
        try:
            ids = (_json.loads(a.external_ids) or {}).get(adapter.name) or []
            ext = str(ids[0]) if ids else None
        except (ValueError, TypeError):
            ext = None

        report.requested += 1
        if not ext:
            report.skipped_no_external_id += 1
        else:
            points = adapter.fetch_history(a.ammo_id, external_id=ext)
            if points is None:
                report.failed += 1
            else:
                write_history_points(session, adapter.name, points)
                report.succeeded += 1
                report.points_written += len(points)

        # ★ 逐项提交：把写锁切成很多短持有，让采集有机会插入执行
        session.commit()

        if progress_every > 0 and (i % progress_every == 0 or i == total):
            elapsed = time.perf_counter() - started
            eta = (elapsed / i) * (total - i) if i else 0
            log.info("回补进度 %d/%d（成功 %d、失败 %d、点 %d）已用 %.0fs，预计还需 %.0fs",
                     i, total, report.succeeded, report.failed,
                     report.points_written, elapsed, eta)

        if i < total:
            sleep_fn(interval)

    session.flush()
    rebuild_aggregates(session, granularity="1h", since_ts=0)
    rebuild_aggregates(session, granularity="1d", since_ts=0)
    report.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(report.summary())
    return report


def write_history_points(session: Session, source: str,
                         points: list[HistoryPoint]) -> int:
    """把历史曲线点写成 price_tick 记录。

    历史点**跳过跳变判异**：它们来自数据源自身的连续序列，
    且相邻点间隔 1 小时，跳变阈值（针对 15 分钟内相邻采集）本就不适用。
    """
    ticks = [
        TickWrite(
            ammo_id=p.ammo_id, price=float(p.price), unit_price=float(p.price),
            price_kind=p.price_kind, source=source, ts=p.ts, src_ts=p.ts,
            raw=json.dumps({"_origin": "history_backfill"}, ensure_ascii=False),
            suspect=0,
            is_live=0,          # 历史行：不得覆盖同整点的实时行
        )
        for p in points
    ]
    return upsert_ticks(session, ticks)


def data_freshness(session: Session, now_ts: int | None = None) -> dict:
    """数据新鲜度，供 UI 顶部横幅与健康检查使用。"""
    settings = get_settings()
    now = now_ts if now_ts is not None else int(time.time())
    last = global_latest_ts(session)
    if last is None:
        return {"has_data": False, "stale": True, "age_seconds": None,
                "threshold_seconds": settings.stale_after_minutes * 60,
                "last_ts": None}
    age = max(0, now - last)
    return {
        "has_data": True,
        "stale": age > settings.stale_after_minutes * 60,
        "age_seconds": age,
        "threshold_seconds": settings.stale_after_minutes * 60,
        "last_ts": last,
    }
