# -*- coding: utf-8 -*-
"""重置数据库（带强制备份）。

什么时候需要它
--------------
本项目用 SQLAlchemy 的 `create_all` 建表，**不引入 Alembic 迁移工具**
（自用单机项目，迁移工具的复杂度大于收益，见开发文档 ADR 记录）。
代价是：**表结构变更时，已存在的表不会被自动改**
（`create_all` 只创建缺失的表，不修改已有表）。

所以加了字段、改了列类型之后，需要重置数据库。既然本地数据可以重新回补
（7 天历史 2 分钟就能拉回来），重置是完全可以接受的代价。

安全设计
--------
- **默认只预览，不删**：不加 `--yes` 只打印将要发生什么；
- **删除前强制备份**：把 .sqlite3 复制成带时间戳的文件，绝不直接删；
- 连 WAL 与 SHM 一起处理（SQLite 的 WAL 模式会留这两个文件，
  只删主文件会残留不一致状态）。

用法
----
    python scripts/reset_db.py              # 只看会做什么，不动手
    python scripts/reset_db.py --yes        # 备份并重置
    python scripts/reset_db.py --yes --no-backup   # 不备份（不建议）
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from backend.config import get_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="重置数据库（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="确认执行（不给就只预览）")
    ap.add_argument("--no-backup", action="store_true", help="不备份（不建议）")
    args = ap.parse_args()

    s = get_settings()
    db = s.db_path
    sidecars = [Path(str(db) + suffix) for suffix in ("-wal", "-shm")]

    print(f"数据库：{db}")
    if not db.exists():
        print("文件不存在 —— 服务下次启动会直接新建一个干净的库，无需重置。")
        return 0

    size_mb = db.stat().st_size / 1024 / 1024
    print(f"大小  ：{size_mb:.2f} MB")

    # 顺便报告一下将要丢掉什么，让决定有依据
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        for table, label in (("ammo", "物品"), ("price_tick", "价格点"),
                             ("price_agg_1h", "小时聚合")):
            try:
                n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{label:<8}：{n}")
            except sqlite3.OperationalError:
                print(f"{label:<8}：(表不存在)")
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"（读取内容失败：{e}）")

    print("\n将执行：")
    if args.no_backup:
        print("  ! 直接删除（未启用备份）")
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = db.with_name(f"{db.stem}.backup-{stamp}{db.suffix}")
        print(f"  1. 备份为 {backup.name}")
        print("  2. 删除主库与 -wal / -shm 副文件")

    if not args.yes:
        print("\n这是预览模式。确认后请加 --yes 执行：")
        print(f"  python scripts/reset_db.py --yes")
        return 0

    if not args.no_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = db.with_name(f"{db.stem}.backup-{stamp}{db.suffix}")
        shutil.copy2(db, backup)
        print(f"[OK] 已备份：{backup}")

    db.unlink()
    for sc in sidecars:
        if sc.exists():
            sc.unlink()
            print(f"[OK] 已删除副文件：{sc.name}")
    print("[OK] 数据库已重置。下次启动服务会自动重建表并回补历史。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
