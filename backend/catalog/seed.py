# -*- coding: utf-8 -*-
"""子弹基础表（ammo_catalog.yaml）的导入。

职责
----
把 `backend/catalog/ammo_catalog.yaml`（唯一需要人工核对的数据文件）
读进数据库的 `ammo` 表。

为什么基础表用 YAML 而不是"每次从接口现取"
------------------------------------------
接口只给当前在售/在架的物品，一旦某个物品下架，它的历史价格就失去了名称与口径，
变成一堆无法解读的 ID。所以基础表必须独立固化在项目里：

- 它是**唯一的人工地基**，用户可以对它做取舍（enabled）、补充信息（is_pack）；
- 接口改版或关站时，历史数据仍然可读；
- 单元测试可以完全不依赖网络。

导入策略
--------
- `ammo_id` 为主键，存在则更新元数据；
- **`enabled` 字段永不被覆盖**：那是用户的手工选择；
- `external_ids` 会被写入，用于把接口记录对应回本系统的物品。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.repository import AmmoRecord, upsert_ammo

log = logging.getLogger(__name__)

TIER_BY_GRADE = {"1": "白", "2": "绿", "3": "蓝", "4": "紫", "5": "金", "6": "红"}


def load_catalog(path: Path | None = None) -> list[dict]:
    """读取基础表 YAML。"""
    p = path or get_settings().catalog_path
    if not p.exists():
        raise FileNotFoundError(
            f"找不到子弹基础表：{p}\n"
            f"请先用 tools/discover_all_ammo.py 生成它：\n"
            f"  python tools/discover_all_ammo.py --url https://dergame.cn/api/Items/search "
            f"--out {p}"
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"基础表格式应为列表，实际是 {type(data).__name__}")
    return data


def to_records(entries: list[dict]) -> list[AmmoRecord]:
    """把 YAML 条目转成仓储层的记录对象。"""
    out: list[AmmoRecord] = []
    for e in entries:
        if not e.get("ammo_id") or not e.get("name"):
            log.warning("跳过缺少 ammo_id/name 的条目：%r", e)
            continue
        grade = str(e.get("grade") or "")
        out.append(AmmoRecord(
            ammo_id=str(e["ammo_id"]),
            name=str(e["name"]),
            caliber=e.get("caliber"),
            tier=e.get("tier") or TIER_BY_GRADE.get(grade),
            grade=grade or None,
            is_pack=int(e.get("is_pack") or 0),
            pack_size=int(e.get("pack_size") or 1),
            length=int(e["length"]) if str(e.get("length") or "").isdigit() else 1,
            width=int(e["width"]) if str(e.get("width") or "").isdigit() else 1,
            tradeable=int(e.get("tradeable", 1)),
            enabled=int(e.get("enabled", 1)),
        ))
    return out


def seed(session: Session, path: Path | None = None) -> dict:
    """把基础表导入数据库，并把 external_ids 写回。

    返回统计信息，便于 CLI 打印与测试断言。
    """
    entries = load_catalog(path)
    records = to_records(entries)

    # external_ids 不在 AmmoRecord 里（它属于数据源的映射信息），单独写
    from backend.models import Ammo

    n = upsert_ammo(session, records)
    with_ext = 0
    for e in entries:
        ext = e.get("external_ids")
        if not ext:
            continue
        row = session.get(Ammo, str(e["ammo_id"]))
        if row is None:
            continue
        row.external_ids = json.dumps(ext, ensure_ascii=False)
        with_ext += 1

    stats = {
        "entries": len(entries),
        "imported": n,
        "with_external_ids": with_ext,
        "tradeable": sum(1 for r in records if r.tradeable),
        "packs": sum(1 for r in records if r.is_pack),
    }
    log.info("基础表导入完成：%s", stats)
    return stats
