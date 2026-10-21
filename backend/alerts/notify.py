# -*- coding: utf-8 -*-
"""通知通道。

第一版范围（按用户确认）
------------------------
只做**浏览器内提醒 + 触发历史**。也就是说：
- 后端负责判定规则、写 alert_log、把待推送的告警放进一个内存队列；
- 前端通过轮询或 WebSocket 取走队列并在页面内弹出提醒；
- 不做桌面弹窗、不做手机推送（第二版再加，接口已预留）。

为什么这样切
------------
桌面弹窗与手机推送都需要额外的依赖或外部 Key（plyer / Server酱 / Bark）。
把它们隔离在 notify 模块后面，第一版先不实现，将来加通道不需要改规则逻辑。

通道契约
--------
每个通道是一个实现 `send(payload) -> None` 的函数，失败抛异常。
`dispatch()` 负责逐个尝试、记录错误，并**保证一个通道失败不影响其它通道**。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Notification:
    """一条待推送的通知。字段设计成可直接序列化给前端。"""

    ammo_id: str
    ammo_name: str = ""
    rule_id: int | None = None
    rule_type: str = ""
    price: float | None = None
    reason: str = ""
    triggered_at: int = field(default_factory=lambda: int(time.time()))
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── 内存队列：浏览器通道的投递介质 ──────────────────────────────────────
# 有界队列，防止前端长时间不开页面导致内存无限增长。
# 上限之外丢弃最旧的，因为告警有时效性，"最新的"比"最全的"更重要。
_PENDING: deque[Notification] = deque(maxlen=200)


def enqueue_browser(n: Notification) -> None:
    _PENDING.append(n)


def drain_pending(limit: int = 50) -> list[Notification]:
    """取走并清空最多 limit 条待推送通知。"""
    out: list[Notification] = []
    while _PENDING and len(out) < limit:
        out.append(_PENDING.popleft())
    return out


def peek_pending() -> int:
    """当前排队条数。用于健康检查与前端指示。"""
    return len(_PENDING)


def clear_pending() -> None:
    """清空队列。仅供测试使用。"""
    _PENDING.clear()


# ── 通道注册 ────────────────────────────────────────────────────────────
ChannelFn = Callable[[Notification], None]
_CHANNELS: dict[str, ChannelFn] = {
    "browser": enqueue_browser,
}


def register_channel(name: str, fn: ChannelFn) -> None:
    """注册一个新通道。

    第二版接桌面弹窗/手机推送时，只需在这里加一行，不必改规则判定逻辑：

        def _desktop(n): plyer.notification.notify(title=..., message=n.reason)
        register_channel("desktop", _desktop)
    """
    _CHANNELS[name] = fn


def available_channels() -> list[str]:
    return sorted(_CHANNELS)


def dispatch(n: Notification, channels: list[str]) -> list[str]:
    """把通知投递到指定通道。返回失败信息列表（空列表表示全部成功）。

    刻意不抛异常：告警投递失败不应该让整轮采集或规则判定中断。
    """
    errors: list[str] = []
    for ch in channels:
        fn = _CHANNELS.get(ch)
        if fn is None:
            errors.append(f"{ch}: 未注册的通道")
            continue
        try:
            fn(n)
        except Exception as e:  # noqa: BLE001 - 单个通道失败不影响其它通道
            log.warning("通知通道 %s 投递失败：%s", ch, e)
            errors.append(f"{ch}: {e}")
    return errors
