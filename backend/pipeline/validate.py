# -*- coding: utf-8 -*-
"""价格校验：决定一条报价能否进入曲线与告警。

对应开发文档 5.3 节的"数据质量红线"。所有函数都是**纯函数**，
不碰数据库、不读时钟，因此可以用固定数据完整单测。

一条报价可能被拒绝的原因
------------------------
1. 价格 <= 0 或缺失 —— 会污染分位数（0 会被当成历史最低价）；
2. 时间戳异常（来自未来，或早于上一笔采集）—— 顺序错乱会让曲线回折；
3. 相对上一次采集**跳变过大** —— 通常意味着解析错位或数据源故障。

关于跳变阈值的重要设计决定
--------------------------
阈值只用于**相邻两次采集之间**的比较，且仅在两次采样间隔很短（默认 <= 15 分钟）时生效。
原因：实测存在 7 日涨幅 +79% 的真实行情。如果用"7 日累计涨幅"判异，
真实暴涨会被误杀，反而看不到最该关注的行情。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Reason = Literal[
    "ok",
    "non_positive_price",
    "future_timestamp",
    "backwards_timestamp",
    "jump_too_large",
    "stale",
]

# 只有采样间隔不超过该秒数时，才认为"两次相邻采集"具备可比性，从而启用跳变判异。
MAX_COMPARABLE_GAP_SECONDS = 15 * 60


@dataclass(slots=True)
class ValidationResult:
    """校验结论。`suspect=True` 的记录入库但标记，不参与曲线与告警。"""

    ok: bool
    reason: Reason = "ok"
    change_pct: float | None = None

    @property
    def suspect(self) -> bool:
        return not self.ok

    def describe(self) -> str:
        return {
            "ok": "通过",
            "non_positive_price": "价格非正或缺失",
            "future_timestamp": "时间戳在未来",
            "backwards_timestamp": "时间戳早于上一笔",
            "jump_too_large": f"相邻跳变过大（{self.change_pct:.1f}%）" if self.change_pct is not None
                              else "相邻跳变过大",
            "stale": "数据过期",
        }[self.reason]


def pct_change(old: float, new: float) -> float | None:
    """涨跌百分比。old <= 0 时无意义，返回 None 而不是 inf。"""
    if old is None or new is None or old <= 0:
        return None
    return (new - old) / old * 100.0


def validate_quote(
    *,
    price: float | None,
    ts: int,
    now_ts: int,
    prev_price: float | None = None,
    prev_ts: int | None = None,
    jump_threshold_pct: float = 60.0,
    stale_after_seconds: int | None = None,
) -> ValidationResult:
    """校验一条报价。

    参数
    ----
    price               本次价格
    ts                  本次采集时间（epoch 秒）
    now_ts              当前时间（epoch 秒），由调用方注入以便测试
    prev_price/prev_ts  同一物品上一次**有效**采集，用于跳变判定
    jump_threshold_pct  相邻跳变阈值（百分比）
    stale_after_seconds 超过该时长未更新的数据视为过期；None 表示不检查
    """
    if price is None or price <= 0:
        return ValidationResult(False, "non_positive_price")

    # 未来时间戳：容忍 5 分钟时钟偏差，超出则视为异常
    if ts > now_ts + 300:
        return ValidationResult(False, "future_timestamp")

    if prev_ts is not None and ts < prev_ts:
        return ValidationResult(False, "backwards_timestamp")

    if stale_after_seconds is not None and now_ts - ts > stale_after_seconds:
        return ValidationResult(False, "stale")

    # 跳变判异：仅当两次采样间隔足够短，比较才有意义
    if prev_price is not None and prev_price > 0 and prev_ts is not None:
        gap = ts - prev_ts
        if 0 <= gap <= MAX_COMPARABLE_GAP_SECONDS:
            change = pct_change(prev_price, price)
            if change is not None and abs(change) > jump_threshold_pct:
                return ValidationResult(False, "jump_too_large", change)

    return ValidationResult(True, "ok")
