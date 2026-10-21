# -*- coding: utf-8 -*-
"""基础表生成规则的单元测试（离线）。

为什么要单独测这些规则
----------------------
`tools/discover_all_ammo.py` 生成的是整个项目的**唯一人工地基**：
`ammo_id` 是主键，一个物品一条价格序列。这里出错的后果不是报错，
而是**静默的数据污染**，实测踩到过两次：

1. 同名不同品质被合并成一个 `ammo_id`（`.300BLK` 有 grade 3/4/5，
   价格 444 / 1604 / 4871）→ 价格来源在轮询间漂移，产生 -67% 的假跳变，
   被数据质量校验拦下，但根因是主键设计错误。
2. `slugify` 只保留 ASCII → `碳纤维刺骨箭矢` 与 `碳纤维穿甲箭矢` 都变成
   `unknown` 而撞主键 → 两个不同物品共用一条价格序列。

所以这些规则必须有测试锁住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.discover_all_ammo import build_catalog, slugify  # noqa: E402


def rec(name, rid, grade, price, caliber="5.56x45mm"):
    return {
        "id": str(rid), "name": name, "grade": str(grade), "second": caliber,
        "length": "1", "width": "1", "value": {"new": price},
    }


# ── slugify ─────────────────────────────────────────────────────────────
def test_slugify_keeps_chinese():
    """**关键回归测试**：中文名必须保留可区分的字符，不能都塌成 unknown。"""
    a = slugify("碳纤维刺骨箭矢")
    b = slugify("碳纤维穿甲箭矢")
    assert a != b, "两个不同的中文名被压成了同一个 slug"
    assert a == "碳纤维刺骨箭矢"
    assert "unknown" not in (a, b)


def test_slugify_normalizes_ascii():
    assert slugify(".300BLK") == "300blk"
    assert slugify("5.56*45mm M855 APC") == "5_56_45mm_m855_apc"
    assert slugify("  .357 Magnum  ") == "357_magnum"


def test_slugify_never_empty():
    assert slugify("!!!") == "unknown"
    assert slugify("") == "unknown"


# ── 同名不同品质必须拆分 ────────────────────────────────────────────────
def test_same_name_different_grade_splits_into_distinct_ids():
    """**关键回归测试**：同名不同品质必须各自独立成条。

    `.300BLK` 的三个品质价格是 444 / 1604 / 4871。若合并成一个 ammo_id，
    "同一个物品"的价格会在轮询之间跳来跳去 —— 这是假数据，不是行情。
    """
    by_name = {
        ".300BLK": [
            rec(".300BLK", 37280500001, 5, 4871, "7.62x35mm"),
            rec(".300BLK", 37280400001, 4, 1604, "7.62x35mm"),
            rec(".300BLK", 37280300001, 3, 444, "7.62x35mm"),
        ]
    }
    catalog = build_catalog(by_name)
    assert len(catalog) == 3

    ids = [c["ammo_id"] for c in catalog]
    assert len(set(ids)) == 3, f"ammo_id 未拆分：{ids}"
    assert set(ids) == {"300blk_g5", "300blk_g4", "300blk_g3"}

    # 每个条目只能绑定一个数据源 ID —— 多绑就意味着价格来源不唯一
    for c in catalog:
        assert len(c["external_ids"]["dergame"]) == 1, \
            f"{c['ammo_id']} 绑定了多个数据源 ID：{c['external_ids']['dergame']}"

    # 品质与价格要对应上，不能串位
    by_id = {c["ammo_id"]: c for c in catalog}
    assert by_id["300blk_g5"]["tier"] == "金"
    assert by_id["300blk_g3"]["tier"] == "蓝"


def test_unique_name_keeps_plain_slug():
    """名字唯一时不应该加上多余的后缀。"""
    by_name = {"M855": [rec("M855", 111, 3, 200)]}
    catalog = build_catalog(by_name)
    assert len(catalog) == 1
    assert catalog[0]["ammo_id"] == "m855"


def test_collision_falls_back_to_id_suffix():
    """slug 相同但确实是不同物品时，用数据源 ID 后缀兜底，保证主键唯一。"""
    by_name = {
        "物品A": [rec("物品A", 123450001, 3, 100)],
        "物品A ": [rec("物品A ", 123450002, 4, 200)],   # 尾部空格，slug 相同
    }
    catalog = build_catalog(by_name)
    ids = [c["ammo_id"] for c in catalog]
    assert len(set(ids)) == len(ids), f"主键冲突：{ids}"


def test_tradeable_flag_from_price_presence():
    """无价格 = 不可交易，必须置 tradeable=0（采集时跳过）。"""
    by_name = {
        "有价弹": [rec("有价弹", 1, 3, 100)],
        "无价弹": [{"id": "2", "name": "无价弹", "grade": "6", "second": "x",
                    "length": "1", "width": "1", "value": {"new": None}}],
    }
    catalog = build_catalog(by_name)
    flags = {c["name"]: c["tradeable"] for c in catalog}
    assert flags["有价弹"] == 1
    assert flags["无价弹"] == 0


def test_catalog_ids_globally_unique_for_realistic_mix():
    """混合场景下主键必须全局唯一 —— 这是写文件前的最后一道自检。"""
    by_name = {
        ".300BLK": [rec(".300BLK", 37280500001, 5, 4871),
                    rec(".300BLK", 37280300001, 3, 444)],
        "碳纤维刺骨箭矢": [rec("碳纤维刺骨箭矢", 37270400001, 4, 3000)],
        "碳纤维穿甲箭矢": [rec("碳纤维穿甲箭矢", 37270500001, 5, 4000)],
        "5.56*45mm M855": [rec("5.56*45mm M855", 1, 3, 200)],
    }
    catalog = build_catalog(by_name)
    ids = [c["ammo_id"] for c in catalog]
    assert len(set(ids)) == len(ids), f"主键冲突：{ids}"
    assert len(catalog) == 5
