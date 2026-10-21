# -*- coding: utf-8 -*-
"""进程内调度器。

为什么不用外部调度
------------------
自用单机项目引入 Celery / Redis / cron 是过度设计。APScheduler 在进程内即可
满足"每 10 分钟采集一次"这种需求，且不需要额外的运维成本。

关键行为
--------
1. **抖动（jitter）**：在间隔上叠加随机量，避免整点集中请求公开接口。
2. **指数退避**：采集失败后按 1s → 4s → 16s 重试；连续失败达阈值时记一次告警，
   之后每 30 分钟提醒一次，避免"数据源挂了导致通知风暴"。
3. **串行执行**：`max_instances=1`，保证上一次采集没跑完时不会并发触发第二次。
4. **写锁（write_lock）**：SQLite 只允许一个写事务。启动时的"立即采集"与
   "历史回补"是两个并发线程，两者都会写库 —— 不加锁就会撞出
   `database is locked`。这是实测抓到的真实 bug，不是理论风险。
5. **失败不崩**：所有任务体都捕获异常并记录，绝不让调度线程因一次网络错误而死掉。

启动时的历史回补
----------------
数据源提供 7 天逐小时历史，所以首次启动会跑一次回补任务，
让分位数等指标**立刻**可用（否则要等采集攒够样本）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from backend.config import get_settings
from backend.db import session_scope
from backend.pipeline import ingest as ingest_mod
from backend.pipeline.aggregate import run_retention
from backend.repository import backfill_coverage
from backend.sources.base import get_adapter

log = logging.getLogger(__name__)


@dataclass
class SchedulerState:
    """调度器的可观测状态，供 /api/health 与前端展示。"""

    last_run_ts: int | None = None
    last_ok: bool | None = None
    last_summary: str = ""
    consecutive_failures: int = 0
    next_run_ts: int | None = None
    running: bool = False
    backfill_done: bool = False
    backfill_summary: str = ""
    history: list[str] = field(default_factory=list)   # 最近若干次结果摘要


class RadarScheduler:
    """封装 APScheduler，把项目需要的调度语义收敛在一个类里。"""

    JOB_INGEST = "ingest"
    JOB_RETENTION = "retention"

    # 连续失败达到该次数后，每 FAILURE_REMINDER_SECONDS 提醒一次
    FAILURE_ALERT_THRESHOLD = 3
    FAILURE_REMINDER_SECONDS = 30 * 60

    def __init__(self, adapter=None) -> None:
        self.settings = get_settings()
        if adapter is None:
            # 显式导入适配器模块，确保注册表已填充。
            # 不依赖别处先导入：测试里注册表可能被清空过，
            # 不这样做就会得到 "未知数据源" 这种很难定位的错误。
            from backend.sources import dergame as _dergame  # noqa: F401

        self.adapter = adapter or get_adapter("dergame")

        # 写锁：SQLite 只允许一个写事务。粒度是"一次完整采集/回补"，
        # 而不是每个 INSERT —— 后者会让单次采集变成几十次加解锁，反而更慢。
        self.write_lock = threading.Lock()

        self.state = SchedulerState()
        self._scheduler = BackgroundScheduler(
            timezone=str(self.settings.tz),
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )
        self._lock = threading.Lock()
        self._last_failure_alert_ts: int | None = None
        self._on_alert: Callable[[str], None] | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    def set_alert_hook(self, fn: Callable[[str], None]) -> None:
        """注册一个系统级告警回调（用于"数据源连续失败"这类运维事件）。"""
        self._on_alert = fn

    def start(self, *, with_backfill: bool | None = None) -> None:
        interval = self.settings.poll_interval_seconds
        jitter = self.settings.poll_jitter_seconds

        if not self._scheduler.running:
            self._scheduler.add_job(
                self.run_ingest, "interval",
                seconds=max(60, interval),
                jitter=max(0, jitter),
                id=self.JOB_INGEST, replace_existing=True,
            )
            # 保留策略每天凌晨 3 点执行一次
            self._scheduler.add_job(
                self.run_retention, "cron", hour=3, minute=0,
                id=self.JOB_RETENTION, replace_existing=True,
            )
            self._scheduler.start()
            log.info("调度器已启动：采集间隔 %ds（抖动 %ds）", interval, jitter)

        do_backfill = (self.settings.backfill_on_startup
                       if with_backfill is None else with_backfill)

        # ★ 从数据库推导回补状态，而不是依赖内存标志。
        # 内存标志会在重启后丢失，导致两个真实问题（都实测踩到过）：
        #   1. 健康接口永远显示"回补未完成"；
        #   2. 整批回补重跑一遍，白白浪费 2 分钟和 83 次对外请求。
        self._refresh_backfill_state()

        if do_backfill and not self.state.backfill_done:
            # 放后台线程：回补要跑一两分钟，不能阻塞服务启动
            threading.Thread(target=self._safe_backfill, name="backfill", daemon=True).start()
        elif self.state.backfill_done:
            log.info("历史曲线已完整覆盖，跳过回补：%s", self.state.backfill_summary)

        # 立即先采一次，让页面一打开就有数据。
        # 注意：如果回补抢到了写锁，这一次会被自动跳过（见 run_ingest），
        # 这是刻意的 —— 10 分钟后本来就有下一轮，没必要排队等两分钟。
        threading.Thread(target=self.run_ingest, name="initial-ingest", daemon=True).start()

    def _refresh_backfill_state(self) -> None:
        """用数据库里的实际覆盖率刷新内存状态。"""
        try:
            with session_scope() as session:
                cov = backfill_coverage(session, self.adapter.name)
        except Exception:  # noqa: BLE001
            log.exception("读取回补覆盖率失败")
            return

        with self._lock:
            self.state.backfill_done = bool(cov["complete"])
            if cov["total"]:
                self.state.backfill_summary = (
                    f"历史曲线覆盖 {cov['covered']}/{cov['total']} 个可交易物品"
                    + ("" if cov["complete"] else f"，还差 {cov['missing']} 个"))
            else:
                self.state.backfill_summary = "没有可回补的可交易物品"

    def refresh_next_run(self) -> None:
        """把 APScheduler 的下次执行时间同步到状态里。

        初版只在"采集成功"时更新它，于是首次采集被回补挡住时，
        健康接口的 `next_run_ts` 会一直是 null，看起来像调度器没在工作。
        """
        job = self._scheduler.get_job(self.JOB_INGEST) if self._scheduler.running else None
        if job is not None and job.next_run_time is not None:
            with self._lock:
                self.state.next_run_ts = int(job.next_run_time.timestamp())

    def backfill_coverage(self) -> dict:
        """对外暴露当前覆盖率，供健康接口使用。"""
        with session_scope() as session:
            return backfill_coverage(session, self.adapter.name)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("调度器已停止")
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()

    # ── 任务体 ──────────────────────────────────────────────────────────
    def run_ingest(self) -> ingest_mod.IngestReport | None:
        """采集任务。带指数退避重试。

        用**非阻塞**方式取写锁：如果历史回补正在跑，本次采集直接跳过并记录，
        而不是排队阻塞 —— 避免任务堆积。
        """
        if not self.write_lock.acquire(blocking=False):
            msg = "历史回补正在写库，跳过本轮采集（下一轮正常执行）"
            log.info(msg)
            with self._lock:
                self.state.last_summary = msg
            return None

        with self._lock:
            self.state.running = True
        try:
            report = self._ingest_with_retry()
        except Exception as e:  # noqa: BLE001 - 调度线程绝不能因任务异常而终止
            log.exception("采集任务异常")
            with self._lock:
                self.state.last_ok = False
                self.state.last_summary = f"任务异常：{e}"
                self.state.consecutive_failures += 1
            self._maybe_alert_failure()
            return None
        finally:
            with self._lock:
                self.state.running = False
            self.write_lock.release()

        self._record(report)
        return report

    def _ingest_with_retry(self) -> ingest_mod.IngestReport:
        s = self.settings
        last: ingest_mod.IngestReport | None = None
        for attempt in range(max(1, s.max_retries)):
            with session_scope() as session:
                last = ingest_mod.ingest_once(session, self.adapter)
            if last.ok:
                return last
            if attempt < s.max_retries - 1:
                delay = 4 ** attempt          # 1s, 4s, 16s
                log.warning("第 %d 次采集失败（%s），%ds 后重试",
                            attempt + 1, last.error, delay)
                threading.Event().wait(delay)
        assert last is not None
        return last

    def _safe_backfill(self) -> None:
        """历史回补。失败不影响主流程 —— 只是少了历史，采集照常工作。

        同样要拿写锁，并且回补内部**逐项提交**（见 ingest.backfill_history）：
        否则会把写事务持有两分钟，期间任何采集都写不进去。
        """
        self.write_lock.acquire()
        try:
            with session_scope() as session:
                rep = ingest_mod.backfill_history(session, self.adapter)
            # 回补结束后用数据库的实际覆盖率刷新状态（而不是盲目置 True）：
            # 若有个别物品回补失败，状态会如实显示"还差 N 个"。
            self._refresh_backfill_state()
            log.info(rep.summary())
        except Exception as e:  # noqa: BLE001
            log.exception("历史回补失败（不影响常规采集）")
            with self._lock:
                self.state.backfill_summary = f"回补失败：{e}"
        finally:
            self.write_lock.release()

    def run_retention(self) -> dict[str, int]:
        if not self.write_lock.acquire(blocking=False):
            log.info("写锁被占用，跳过本次保留策略")
            return {}
        try:
            with session_scope() as session:
                return run_retention(session)
        except Exception:  # noqa: BLE001
            log.exception("保留策略执行失败")
            return {}
        finally:
            self.write_lock.release()

    # ── 状态 ────────────────────────────────────────────────────────────
    def _record(self, report: ingest_mod.IngestReport) -> None:
        import time

        with self._lock:
            self.state.last_run_ts = int(time.time())
            self.state.last_ok = report.ok
            self.state.last_summary = report.summary()
            self.state.history.insert(0, report.summary())
            del self.state.history[20:]
            if report.ok:
                self.state.consecutive_failures = 0
                self._last_failure_alert_ts = None
            else:
                self.state.consecutive_failures += 1

        job = self._scheduler.get_job(self.JOB_INGEST) if self._scheduler.running else None
        if job is not None and job.next_run_time is not None:
            with self._lock:
                self.state.next_run_ts = int(job.next_run_time.timestamp())

    def _maybe_alert_failure(self) -> None:
        """连续失败达阈值时提醒一次，之后每 30 分钟最多一次。"""
        import time

        with self._lock:
            streak = self.state.consecutive_failures
        if streak < self.FAILURE_ALERT_THRESHOLD:
            return
        now = int(time.time())
        if (self._last_failure_alert_ts is not None
                and now - self._last_failure_alert_ts < self.FAILURE_REMINDER_SECONDS):
            return
        self._last_failure_alert_ts = now
        msg = (f"数据源已连续 {streak} 次采集失败，"
               f"最后错误：{self.state.last_summary}")
        log.error(msg)
        if self._on_alert:
            try:
                self._on_alert(msg)
            except Exception:  # noqa: BLE001
                log.exception("系统级告警回调失败")


# ── 进程级单例 ──────────────────────────────────────────────────────────
_scheduler: RadarScheduler | None = None


def get_scheduler() -> RadarScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = RadarScheduler()
    return _scheduler


def reset_scheduler() -> None:
    """销毁单例。仅供测试使用。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown()
    _scheduler = None
