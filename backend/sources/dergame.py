# -*- coding: utf-8 -*-
"""DerGame 数据源适配器。

数据源事实（全部来自实测，详见 docs/数据源实测报告-dergame.md）
------------------------------------------------------------
接口：POST https://dergame.cn/api/Items/search
请求：{"type": ["弹药"]}
响应：{"code":0, "updateTime":"YYYY-MM-DD HH:MM:SS", "msg":"...", "data":[ {...} ]}

实测踩过的坑，本模块逐一规避：
1. `grade` 参数会让接口返回空 —— 本适配器**从不发送 grade**，品质在本地筛。
2. `type` 的取值不等于数据里的 `primary` —— 所以仍要在本地按 primary=="弹药" 过滤，
   不能盲信服务端的过滤。
3. 响应里 `value.new` 可能是 null（不可交易物品）—— 必须跳过，不能当 0 处理。
4. `value.7zf` / `7df` 的类型在 int 与 float 之间摇摆 —— 统一转 float。
5. 100 条硬上限 —— 本接口按 type 过滤后实测 85 条，未触上限；但如果将来
   弹药数量增长到接近 100，必须改用切片方案（见 tools/discover_all_ammo.py）。

额外信息：响应里带 7 日最高/最低（7top/7down）与涨跌比例（7zf），
这些可以**直接用于分位数分析**，不必等本地积累历史 —— 这是本数据源的隐藏价值。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx

from backend.config import get_settings
from backend.sources.base import (
    FetchResult,
    HistoryPoint,
    ItemQuote,
    register,
)

log = logging.getLogger(__name__)

SOURCE_NAME = "dergame"


def _to_float(v: Any) -> float | None:
    """把 int / float / 数字字符串统一转 float；无法转换返回 None。

    实测 `value.7zf` 既出现过 int 也出现过 float，`avg_price` 则是字符串，
    所以这里必须宽容处理而不是假设类型。
    """
    if v is None:
        return None
    if isinstance(v, bool):          # bool 是 int 的子类，显式排除以免 True->1
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None     # 过滤 NaN


def _parse_src_time(text: str | None) -> int | None:
    """把数据源的 'YYYY-MM-DD HH:MM:SS' 按配置时区解析为 epoch 秒。

    数据源给的是**本地时间字符串且不带时区**，所以必须显式按 Asia/Shanghai 解释，
    否则会整体偏移 8 小时，导致新鲜度判断与告警时间全错。
    """
    if not text:
        return None
    s = text.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=get_settings().tz)
            return int(dt.timestamp())
        except ValueError:
            continue
    log.warning("无法解析数据源时间：%r", text)
    return None


@register(SOURCE_NAME)
class DerGameAdapter:
    """公开只读接口适配器。只读、无凭证、带礼貌间隔。"""

    name = SOURCE_NAME

    def __init__(
        self,
        search_url: str | None = None,
        chart_url: str | None = None,
        type_value: str | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        s = get_settings()
        self.search_url = search_url or s.source_search_url
        self.chart_url = chart_url or s.source_chart_url
        self.type_value = type_value or s.source_type_value
        self.timeout = timeout if timeout is not None else s.http_timeout_seconds
        self.user_agent = user_agent or s.source_user_agent
        self._client: httpx.Client | None = None

    # ── HTTP ────────────────────────────────────────────────────────────
    def _get_client(self) -> httpx.Client:
        """复用一个连接池：避免每次请求都重新握手，也让限流更友好。"""
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self.user_agent,
                    "Content-Type": "application/json",
                    "Accept": "application/json, */*",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 解析 ────────────────────────────────────────────────────────────
    def parse_quotes(self, payload: dict[str, Any]) -> tuple[list[ItemQuote], str | None]:
        """把响应体解析成 ItemQuote 列表。**纯函数**，便于用 fixture 单测。

        返回 (quotes, 数据源时间字符串)。
        """
        src_time_text = payload.get("updateTime")
        src_ts = _parse_src_time(src_time_text)
        rows = payload.get("data") or []

        quotes: list[ItemQuote] = []
        skipped_no_price = 0
        skipped_other_type = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            # 不盲信服务端过滤：本地再确认一次类型
            if row.get("primary") != self.type_value:
                skipped_other_type += 1
                continue

            value = row.get("value") or {}
            price = _to_float(value.get("new"))
            if price is None or price <= 0:
                # 不可交易物品没有价格。**跳过而不是写 0**，
                # 否则会污染曲线与分位数（0 会被算成历史最低价）。
                skipped_no_price += 1
                continue

            external_id = str(row.get("id") or "").strip()
            if not external_id:
                continue

            quotes.append(
                ItemQuote(
                    ammo_id="",                 # 由上层用 external_ids 解析后回填
                    price=price,
                    price_kind="reference",
                    unit_price=price,           # 本源全是散装 1x1
                    source=self.name,
                    src_ts=src_ts,
                    raw={
                        "id": external_id,
                        "name": row.get("name"),
                        "grade": row.get("grade"),
                        "caliber": row.get("second"),
                        "length": row.get("length"),
                        "width": row.get("width"),
                        # 以下 4 个字段是数据源直接给出的 7 日统计，用于互校
                        "day7_change_pct": _to_float(value.get("7zf")),
                        "day7_change_abs": _to_float(value.get("7df")),
                        "day7_high": _to_float(value.get("7top")),
                        "day7_low": _to_float(value.get("7down")),
                    },
                )
            )

        if skipped_no_price or skipped_other_type:
            log.info(
                "解析完成：有效 %d 条；跳过无价格 %d 条、非目标类型 %d 条",
                len(quotes), skipped_no_price, skipped_other_type,
            )
        return quotes, src_time_text

    def parse_history(self, ammo_id: str, payload: dict[str, Any]) -> list[HistoryPoint]:
        """解析曲线响应。**纯函数**，便于单测。

        响应形如 {"name": "...", "values": [{"time": "...", "avg_price": "284"}]}
        注意 `avg_price` 是**字符串**，必须显式转换。
        """
        points: list[HistoryPoint] = []
        for v in payload.get("values") or []:
            if not isinstance(v, dict):
                continue
            price = _to_float(v.get("avg_price"))
            ts = _parse_src_time(v.get("time"))
            if price is None or price <= 0 or ts is None:
                continue
            points.append(
                HistoryPoint(ammo_id=ammo_id, ts=ts, price=price,
                             price_kind="reference", source=self.name)
            )
        points.sort(key=lambda p: p.ts)
        return points

    # ── 采集 ────────────────────────────────────────────────────────────
    def fetch_quotes(self, item_ids: list[str] | None = None) -> FetchResult:
        """取全量报价。

        注意：本接口没有分页与批量参数，只能整类取回（弹药共 85 条，远低于 100 上限）。
        所以 `item_ids` 参数只用于**按需裁剪返回**，不改变实际请求。
        """
        t0 = time.perf_counter()
        body = {"type": [self.type_value]}   # 绝不发送 grade（发了一定返回空）
        try:
            resp = self._get_client().post(self.search_url, json=body)
        except httpx.HTTPError as e:
            latency = int((time.perf_counter() - t0) * 1000)
            return FetchResult(ok=False, latency_ms=latency, error=f"{type(e).__name__}: {e}")

        latency = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            return FetchResult(ok=False, latency_ms=latency,
                               error=f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as e:
            return FetchResult(ok=False, latency_ms=latency,
                               error=f"响应不是合法 JSON: {e}")

        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            return FetchResult(ok=False, latency_ms=latency,
                               error=f"接口返回错误 code={payload.get('code')} "
                                     f"msg={payload.get('msg')}")

        quotes, src_time = self.parse_quotes(payload)
        return FetchResult(ok=True, quotes=quotes, latency_ms=latency, src_update_time=src_time)

    def fetch_history(self, ammo_id: str, external_id: str | None = None,
                      chart_type: int | None = None) -> list[HistoryPoint] | None:
        """取单个物品的历史曲线。

        失败返回 None（而不是抛异常），让上层决定是降级还是跳过 ——
        历史回补是"尽力而为"的任务，一条失败不该中断整批。
        """
        if not external_id:
            log.warning("fetch_history 缺少 external_id，跳过 %s", ammo_id)
            return None
        s = get_settings()
        body = {
            "itemId": str(external_id),   # 实测字符串与数字都可用；统一用字符串避免精度问题
            "chartType": chart_type if chart_type is not None else s.chart_type,
        }
        try:
            resp = self._get_client().post(self.chart_url, json=body)
            if resp.status_code != 200:
                log.warning("历史曲线 HTTP %s：%s", resp.status_code, ammo_id)
                return None
            return self.parse_history(ammo_id, resp.json())
        except (httpx.HTTPError, ValueError) as e:
            log.warning("历史曲线获取失败 %s：%s", ammo_id, e)
            return None
