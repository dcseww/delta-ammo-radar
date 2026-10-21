# -*- coding: utf-8 -*-
"""告警规则 CRUD、触发历史，以及浏览器内提醒的取件接口。

第一版范围（用户已确认）：只做**浏览器内提醒 + 触发历史**。
所以这里没有桌面弹窗/手机推送的接口 —— 那些通道通过
`backend.alerts.notify.register_channel` 扩展，不影响本模块。

浏览器提醒的实现方式
--------------------
后端把触发的告警放进一个**有界内存队列**（notify._PENDING），
前端轮询 `/api/alerts/pending` 取走并弹窗，取走即清空，避免重复提醒。
之所以用内存队列而不是数据库表：告警有时效性，
服务重启后旧的未读通知已经没有提醒价值，落库反而要维护已读状态。
但**触发历史仍然完整落库**（alert_log），所以不会丢失记录。
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query

from backend.alerts import notify as notify_mod
from backend.alerts.rules import ALL_RULE_TYPES, parse_quiet_hours
from backend.db import session_scope
from backend.models import AlertRule, utc_now_ts
from backend.repository import (
    add_watch,
    last_triggered_map,
    list_alert_logs,
    list_ammo,
    list_rules,
    list_watchlist,
    remove_watch,
)
from backend.schemas import (
    AlertLogOut,
    AlertRuleIn,
    AlertRuleOut,
    PendingAlertsResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["alerts"])


# ── 规则 CRUD ───────────────────────────────────────────────────────────
@router.get("/alerts/rules", response_model=list[AlertRuleOut])
def get_rules() -> list[AlertRuleOut]:
    with session_scope() as session:
        last = last_triggered_map(session)
        out: list[AlertRuleOut] = []
        for r in list_rules(session):
            out.append(AlertRuleOut(
                rule_id=r.rule_id, ammo_id=r.ammo_id, rule_type=r.rule_type,
                threshold=float(r.threshold), window_min=r.window_min or 60,
                confirm_min=r.confirm_min or 0, cooldown_min=r.cooldown_min or 30,
                quiet_hours=r.quiet_hours, enabled=bool(r.enabled),
                created_at=r.created_at, last_triggered_at=last.get(r.rule_id),
            ))
        return out


@router.post("/alerts/rules", response_model=AlertRuleOut, status_code=201)
def create_rule(payload: AlertRuleIn) -> AlertRuleOut:
    _validate_rule(payload)
    with session_scope() as session:
        # ★ 查重：拦截"作用对象 + 类型 + 阈值"完全相同的规则。
        #
        # 为什么必须在入口拦：多条等效规则各自独立计时与触发，
        # 冷却期只能拦住"单条规则重复触发"，拦不住"三条一样规则同时触发"。
        # 实测踩到过 —— 调试时留下 3 条相同规则，结果每轮采集产生 13 条重复告警。
        # 用户看到的表现是"冷却没生效"，而根因是规则重复；
        # 这种"看起来像 A 实际是 B"的故障最难排查，所以要在源头堵住。
        for existing in list_rules(session):
            if (existing.rule_type == payload.rule_type
                    and (existing.ammo_id or None) == (payload.ammo_id or None)
                    and abs(float(existing.threshold) - float(payload.threshold)) < 1e-9):
                raise HTTPException(
                    status_code=409,
                    detail=(f"已存在一条完全相同的规则（#{existing.rule_id}："
                            f"{existing.rule_type} 阈值 {existing.threshold}"
                            f"{'，对象 ' + existing.ammo_id if existing.ammo_id else '，作用于全部物品'}）。"
                            f"多条等效规则会各自触发，导致重复告警 —— "
                            f"请直接修改或启用已有规则，而不是新建一条。"))

        row = AlertRule(
            ammo_id=payload.ammo_id, rule_type=payload.rule_type,
            threshold=float(payload.threshold), window_min=payload.window_min,
            confirm_min=payload.confirm_min, cooldown_min=payload.cooldown_min,
            quiet_hours=payload.quiet_hours, enabled=1 if payload.enabled else 0,
            channels=json.dumps(["browser"]), created_at=utc_now_ts(),
        )
        session.add(row)
        session.flush()
        return AlertRuleOut(
            rule_id=row.rule_id, ammo_id=row.ammo_id, rule_type=row.rule_type,
            threshold=float(row.threshold), window_min=row.window_min,
            confirm_min=row.confirm_min, cooldown_min=row.cooldown_min,
            quiet_hours=row.quiet_hours, enabled=bool(row.enabled),
            created_at=row.created_at,
        )


@router.put("/alerts/rules/{rule_id}", response_model=AlertRuleOut)
def update_rule(rule_id: int, payload: AlertRuleIn) -> AlertRuleOut:
    _validate_rule(payload)
    with session_scope() as session:
        row = session.get(AlertRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
        row.ammo_id = payload.ammo_id
        row.rule_type = payload.rule_type
        row.threshold = float(payload.threshold)
        row.window_min = payload.window_min
        row.confirm_min = payload.confirm_min
        row.cooldown_min = payload.cooldown_min
        row.quiet_hours = payload.quiet_hours
        row.enabled = 1 if payload.enabled else 0
        return AlertRuleOut(
            rule_id=row.rule_id, ammo_id=row.ammo_id, rule_type=row.rule_type,
            threshold=float(row.threshold), window_min=row.window_min,
            confirm_min=row.confirm_min, cooldown_min=row.cooldown_min,
            quiet_hours=row.quiet_hours, enabled=bool(row.enabled),
            created_at=row.created_at,
        )


@router.delete("/alerts/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int) -> None:
    with session_scope() as session:
        row = session.get(AlertRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
        session.delete(row)


def _validate_rule(payload: AlertRuleIn) -> None:
    """在写入前挡住明显错误的规则。

    特别是 `percentile_below` 的阈值必须是 0~1 的小数：
    用户很可能顺手填 15（以为是 15%），那种规则永远不会触发，
    而且失败得很安静 —— 这类"静默错误"必须在入口就拦住。
    """
    if payload.rule_type not in ALL_RULE_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"未知规则类型 {payload.rule_type!r}，可选 {list(ALL_RULE_TYPES)}")
    if payload.rule_type == "percentile_below" and not (0.0 <= payload.threshold <= 1.0):
        raise HTTPException(
            status_code=400,
            detail=f"percentile_below 的阈值必须是 0~1 之间的小数（例如 0.15 表示 15%），"
                   f"收到 {payload.threshold}。如果填 15 则永远不会触发。")
    if payload.rule_type in ("pct_up", "pct_down") and payload.threshold <= 0:
        raise HTTPException(status_code=400, detail="涨跌幅阈值必须为正数（方向由规则类型决定）")
    if payload.rule_type in ("price_above", "price_below") and payload.threshold <= 0:
        raise HTTPException(status_code=400, detail="价格阈值必须为正数")
    if payload.rule_type == "vol_spike" and payload.threshold <= 0:
        raise HTTPException(status_code=400, detail="z-score 阈值必须为正数")
    if payload.quiet_hours and parse_quiet_hours(payload.quiet_hours) is None:
        raise HTTPException(status_code=400,
                            detail=f"静默时段格式应为 HH:MM-HH:MM，收到 {payload.quiet_hours!r}")
    if payload.ammo_id:
        with session_scope() as session:
            if not any(a.ammo_id == payload.ammo_id for a in list_ammo(session)):
                raise HTTPException(status_code=400, detail=f"物品 {payload.ammo_id} 不存在")


@router.post("/alerts/rules/{rule_id}/toggle", response_model=AlertRuleOut)
def toggle_rule(rule_id: int) -> AlertRuleOut:
    """开关规则。列表页的开关按钮直接调它，不必回传整条规则。"""
    with session_scope() as session:
        row = session.get(AlertRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")
        row.enabled = 0 if row.enabled else 1
        return AlertRuleOut(
            rule_id=row.rule_id, ammo_id=row.ammo_id, rule_type=row.rule_type,
            threshold=float(row.threshold), window_min=row.window_min,
            confirm_min=row.confirm_min, cooldown_min=row.cooldown_min,
            quiet_hours=row.quiet_hours, enabled=bool(row.enabled),
            created_at=row.created_at,
        )


# ── 触发历史与浏览器提醒 ────────────────────────────────────────────────
@router.get("/alerts/log", response_model=list[AlertLogOut])
def get_alert_log(
    limit: int = Query(100, ge=1, le=500),
    ammo_id: str | None = Query(None),
) -> list[AlertLogOut]:
    with session_scope() as session:
        names = {a.ammo_id: a.name for a in list_ammo(session)}
        rules = {r.rule_id: r.rule_type for r in list_rules(session)}
        out: list[AlertLogOut] = []
        for row in list_alert_logs(session, limit=limit, ammo_id=ammo_id):
            detail: dict = {}
            reason = ""
            if row.detail:
                try:
                    detail = json.loads(row.detail)
                    reason = detail.get("reason", "")
                except (ValueError, TypeError):
                    detail = {"raw": row.detail}
            out.append(AlertLogOut(
                id=row.id, rule_id=row.rule_id, ammo_id=row.ammo_id,
                ammo_name=names.get(row.ammo_id or "", ""),
                rule_type=rules.get(row.rule_id or -1, ""),
                triggered_at=row.triggered_at, price=row.price,
                reason=reason, notified=row.notified,
                notify_error=row.notify_error, detail=detail,
            ))
        return out


@router.get("/alerts/pending", response_model=PendingAlertsResponse)
def take_pending_alerts(limit: int = Query(20, ge=1, le=100)) -> PendingAlertsResponse:
    """取走待弹出的浏览器提醒。**取走即清空**，前端因此不会重复弹窗。

    如果没有新告警，返回空列表 —— 前端每 30 秒轮询一次这个接口即可。
    """
    with session_scope() as session:
        names = {a.ammo_id: a.name for a in list_ammo(session)}
        notes = notify_mod.drain_pending(limit=limit)
        items = [
            AlertLogOut(
                id=0, rule_id=n.rule_id, ammo_id=n.ammo_id,
                ammo_name=n.ammo_name or names.get(n.ammo_id, ""),
                rule_type=n.rule_type, triggered_at=n.triggered_at,
                price=n.price, reason=n.reason, notified=1, detail=n.detail,
            )
            for n in notes
        ]
        return PendingAlertsResponse(pending=notify_mod.peek_pending(), items=items)


# ── 自选 ────────────────────────────────────────────────────────────────
@router.get("/watchlist", response_model=list[str])
def get_watchlist() -> list[str]:
    with session_scope() as session:
        return list_watchlist(session)


@router.post("/watchlist/{ammo_id}", status_code=201)
def post_watchlist(ammo_id: str) -> dict:
    with session_scope() as session:
        if not any(a.ammo_id == ammo_id for a in list_ammo(session)):
            raise HTTPException(status_code=404, detail=f"物品 {ammo_id} 不存在")
        created = add_watch(session, ammo_id)
        return {"ammo_id": ammo_id, "created": created}


@router.delete("/watchlist/{ammo_id}", status_code=204)
def delete_watchlist(ammo_id: str) -> None:
    with session_scope() as session:
        if not remove_watch(session, ammo_id):
            raise HTTPException(status_code=404, detail=f"{ammo_id} 不在自选中")
