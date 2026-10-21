# -*- coding: utf-8 -*-
"""回补进度探查：直接读 SQLite，看历史回补到底跑了多少。

为什么需要它
------------
回补要请求 83 个物品（默认间隔 1.4 秒，约 2 分钟），期间写锁被占，
`/api/ingest/trigger` 会（正确地）返回 409。此时只能直接查库来判断：
是"还在慢慢跑"，还是"已经卡死/失败"。

只读打开数据库，不加锁，不影响正在运行的服务。

用法
----
    python scripts/check_backfill.py
    python scripts/check_backfill.py --watch 60      # 观察 60 秒的变化
"""

from __future__ import annotations

import argparse
import sqlite3
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


def snapshot(db: Path) -> dict:
    """只读快照。用 immutable/ro 模式避免干扰运行中的写者。"""
    uri = f"file:{db.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        cur = con.cursor()
        out: dict = {}
        out["ticks"] = cur.execute("SELECT COUNT(*) FROM price_tick").fetchone()[0]
        out["suspect"] = cur.execute(
            "SELECT COUNT(*) FROM price_tick WHERE suspect=1").fetchone()[0]
        out["ammo_with_ticks"] = cur.execute(
            "SELECT COUNT(DISTINCT ammo_id) FROM price_tick").fetchone()[0]
        out["history_origin"] = cur.execute(
            "SELECT COUNT(*) FROM price_tick WHERE raw LIKE '%history_backfill%'"
        ).fetchone()[0]
        out["agg_1h"] = cur.execute("SELECT COUNT(*) FROM price_agg_1h").fetchone()[0]
        out["min_ts"] = cur.execute("SELECT MIN(ts) FROM price_tick").fetchone()[0]
        out["max_ts"] = cur.execute("SELECT MAX(ts) FROM price_tick").fetchone()[0]
        out["tradeable_total"] = cur.execute(
            "SELECT COUNT(*) FROM ammo WHERE tradeable=1 AND enabled=1").fetchone()[0]
        return out
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="查看历史回补进度")
    ap.add_argument("--watch", type=int, default=0, help="观察多少秒")
    ap.add_argument("--interval", type=int, default=15, help="观察时每次间隔秒数")
    args = ap.parse_args()

    db = get_settings().db_path
    print(f"数据库：{db}")
    if not db.exists():
        print("[FAIL] 数据库文件不存在，服务可能还没启动过。")
        return 1

    s = snapshot(db)
    print(f"价格点总数      {s['ticks']}")
    print(f"其中判异        {s['suspect']}")
    print(f"有数据的物品    {s['ammo_with_ticks']} / {s['tradeable_total']} 个可交易物品")
    print(f"回补来源的点    {s['history_origin']}")
    print(f"小时聚合桶      {s['agg_1h']}")
    if s["min_ts"] and s["max_ts"]:
        span = (s["max_ts"] - s["min_ts"]) / 86400
        print(f"数据时间跨度    {span:.2f} 天")
        print(f"  最早 {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['min_ts']))}")
        print(f"  最新 {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['max_ts']))}")

    pct = s["ammo_with_ticks"] / s["tradeable_total"] * 100 if s["tradeable_total"] else 0
    print(f"\n回补进度        {pct:.1f}%")
    if pct >= 100:
        print("=> 回补已完成。")
        return 0
    print("=> 回补仍在进行中，或已中断。")

    if args.watch > 0:
        print(f"\n观察 {args.watch} 秒…")
        deadline = time.time() + args.watch
        prev = s["ammo_with_ticks"]
        while time.time() < deadline:
            time.sleep(args.interval)
            cur = snapshot(db)
            delta = cur["ammo_with_ticks"] - prev
            print(f"  [{time.strftime('%H:%M:%S')}] 物品 {cur['ammo_with_ticks']}"
                  f"（+{delta}）  价格点 {cur['ticks']}")
            if delta == 0 and cur["ammo_with_ticks"] > 0:
                print("  => 进度停滞，回补可能已中断（检查服务日志）。")
                break
            prev = cur["ammo_with_ticks"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
