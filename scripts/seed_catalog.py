# -*- coding: utf-8 -*-
"""导入子弹基础表到数据库。

用法
----
    python scripts/seed_catalog.py
    python scripts/seed_catalog.py --catalog backend/catalog/ammo_catalog.yaml

注意：导入**不会覆盖** `enabled` 字段 —— 那是你在基础表或 API 里的手工取舍，
每次重新导入都被重置会让人白干。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接以脚本方式运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from backend.catalog.seed import seed  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.db import init_db, session_scope  # noqa: E402
from backend.repository import count_ammo, list_ammo  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导入子弹基础表到数据库")
    ap.add_argument("--catalog", default=None, help="基础表路径（默认用配置里的）")
    args = ap.parse_args()

    settings = get_settings()
    init_db()

    path = Path(args.catalog) if args.catalog else settings.catalog_path
    print(f"基础表：{path}")
    print(f"数据库：{settings.db_path}")

    if not path.exists():
        print(f"\n[FAIL] 找不到基础表 {path}")
        print("       请先生成它：python tools/discover_all_ammo.py "
              "--url https://dergame.cn/api/Items/search")
        return 1

    with session_scope() as session:
        stats = seed(session, path)

    print(f"\n导入完成：")
    print(f"  条目总数    {stats['entries']}")
    print(f"  已写入      {stats['imported']}")
    print(f"  带外部ID    {stats['with_external_ids']}")
    print(f"  可交易      {stats['tradeable']}")
    print(f"  打包物项    {stats['packs']}")

    with session_scope() as session:
        counts = count_ammo(session)
        print(f"\n数据库现状：{counts}")

        packs = [a for a in list_ammo(session) if a.is_pack]
        print()
        if packs:
            print(f"[注意] 检测到 {len(packs)} 个打包物项，可以计算包内单发价与散卖价差。")
        else:
            print("[提示] 基础表中没有打包物项（is_pack=1）。")
            print("       当前数据源只提供散装弹药，所以『倒子弹』套利分析不可用。")
            print("       UI 应隐藏该模块，而不是显示估算值（见开发文档 6.3 节）。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
