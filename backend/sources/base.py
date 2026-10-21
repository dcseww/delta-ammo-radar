# -*- coding: utf-8 -*-
"""数据源适配器抽象。

设计目的（对应开发文档的风险 R1）
------------------------------
公开只读接口随时可能改版或关站。为了让"换源"只改一个文件而不是重写整个项目，
所有外部依赖都必须藏在本模块定义的接口后面。

契约
----
适配器只负责三件事：
1. 把外部响应**取回来**；
2. 把字段**映射**成统一的 ItemQuote；
3. 如实报告**采集健康状态**。

适配器**不做**的事：不写数据库、不做业务判断、不算指标。
那些属于 pipeline 层的职责，这样适配器可以被轻松替换与单测。

新增一个数据源的步骤
--------------------
1. 继承 SourceAdapter；
2. 实现 fetch_quotes()（必须）与 fetch_history()（可选，拿不到就返回 None）；
3. 在 registry 里注册；
4. 写契约测试，用录制的响应 fixture 离线回放。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ItemQuote:
    """一个物品在某一时刻的一条报价（统一格式）。"""

    ammo_id: str                  # 本系统内部 ID（已由外部 ID 解析而来）
    price: float                  # 原始价格
    price_kind: str = "reference"  # 价格口径
    unit_price: float | None = None  # 折算单发价
    source: str = "unknown"
    src_ts: int | None = None     # 数据源自带的更新时间（epoch 秒）
    raw: dict[str, Any] = field(default_factory=dict)  # 原始记录，便于回溯排错

    def __post_init__(self) -> None:
        # 本数据源全是散装弹药，单发价与价格相同；接打包物项时这里才有区别。
        if self.unit_price is None:
            self.unit_price = self.price


@dataclass(slots=True)
class HistoryPoint:
    """历史曲线上的一个点。"""

    ammo_id: str
    ts: int                       # epoch 秒（已按配置时区解析）
    price: float
    price_kind: str = "reference"
    source: str = "unknown"


@dataclass(slots=True)
class FetchResult:
    """一次采集的结果。带健康信息，便于 pipeline 记录与告警。"""

    ok: bool
    quotes: list[ItemQuote] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None
    src_update_time: str | None = None   # 数据源声明的数据时间（原样保留）

    @property
    def count(self) -> int:
        return len(self.quotes)


@runtime_checkable
class SourceAdapter(Protocol):
    """数据源适配器协议。registry 用它做结构性校验。"""

    name: str

    def fetch_quotes(self) -> FetchResult:
        """取全量当前报价。失败**不抛异常**，而是返回 ok=False 的 FetchResult。"""
        ...

    def fetch_history(self, ammo_id: str) -> list[HistoryPoint] | None:
        """取单个物品的历史曲线。不支持历史的数据源返回 None。"""
        ...


# ── 注册表 ──────────────────────────────────────────────────────────────
_ADAPTERS: dict[str, type] = {}


def register(name: str):
    """装饰器：把适配器类注册到全局表，供配置按名字实例化。"""

    def deco(cls):
        if name in _ADAPTERS:
            raise ValueError(f"适配器名重复注册：{name}")
        cls.name = name
        _ADAPTERS[name] = cls
        return cls

    return deco


def available_sources() -> list[str]:
    return sorted(_ADAPTERS)


def get_adapter(name: str, **kwargs) -> SourceAdapter:
    """按名字实例化适配器。找不到就抛出清晰的错误，而不是静默返回空数据。"""
    if name not in _ADAPTERS:
        raise KeyError(
            f"未知数据源 {name!r}。已注册：{available_sources()}。"
            f"新增数据源请继承 backend.sources.base.SourceAdapter 并用 @register 注册。"
        )
    return _ADAPTERS[name](**kwargs)


def reset_registry() -> None:
    """清空注册表。仅供测试使用。"""
    _ADAPTERS.clear()


def as_quotes(rows: Iterable[dict], mapper) -> list[ItemQuote]:
    """小工具：把原始记录列表批量映射为 ItemQuote，跳过无法映射的行。

    这样适配器里的循环不用各写一遍 try/except，也保证"单条脏数据不会炸掉整次采集"。
    """
    out: list[ItemQuote] = []
    for row in rows:
        try:
            q = mapper(row)
        except Exception as e:  # noqa: BLE001 - 单条脏数据不应中断整次采集
            log.warning("跳过无法映射的记录：%s (%s)", e, str(row)[:120])
            continue
        if q is not None:
            out.append(q)
    return out
