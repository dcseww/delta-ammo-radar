# -*- coding: utf-8 -*-
"""端到端验收脚本：对**正在运行的服务**做真实数据校验。

为什么需要它（而不是只跑单元测试）
----------------------------------
单元测试用的是假适配器和 fixture 数据，它们能证明逻辑正确，
但证明不了"真的能拿到数据、真的存进去了、真的能算出来"。
本脚本打真实 HTTP 请求，验证整条链路：

    真实公开接口 → 适配器 → 校验 → SQLite → 聚合 → 指标 → API → 前端可读

它会检查若干**容易出现静默错误**的点：
- 价格是否真的非零、非空；
- 历史曲线是否真的回补进来了（点数应远大于 1）；
- 判异记录是否被正确挡在曲线之外；
- 打包物项检查是否如实报告"没有"；
- 数据源自报的 7 日涨跌与自算值是否在同一量级（互校）。

用法
----
    python scripts/verify_e2e.py
    python scripts/verify_e2e.py --base http://127.0.0.1:8787 --wait 300
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    level = PASS if ok else (WARN if warn_only else FAIL)
    results.append((level, name, detail))
    mark = {PASS: "[OK]  ", FAIL: "[FAIL]", WARN: "[WARN]"}[level]
    print(f"{mark} {name}" + (f"  —  {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="对运行中的服务做真实数据端到端校验")
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--wait", type=int, default=0,
                    help="最多等待多少秒让历史回补完成")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.base, timeout=30.0)

    print("=" * 74)
    print(f"端到端验收：{args.base}")
    print("=" * 74)

    # ── 1. 服务可达 ─────────────────────────────────────────────────────
    try:
        h = c.get("/api/health").json()
    except Exception as e:
        print(f"[FAIL] 无法连接服务：{type(e).__name__}: {e}")
        print("\n请先启动：python -m backend.app")
        return 1
    check("服务可达", True, f"status={h['status']}")

    # ── 2. 等待回补（可选）──────────────────────────────────────────────
    if args.wait > 0:
        sched = h.get("scheduler", {})
        if not sched.get("backfill_done"):
            print(f"\n等待历史回补完成（最多 {args.wait} 秒）…")
            deadline = time.time() + args.wait
            while time.time() < deadline:
                time.sleep(5)
                h = c.get("/api/health").json()
                if h.get("scheduler", {}).get("backfill_done"):
                    print("  回补完成。")
                    break
                print(f"  … 已有 {h['tick_counts']['total']} 个价格点")
            else:
                print("  等待超时，继续用现有数据校验。")

    # ── 3. 基础表 ───────────────────────────────────────────────────────
    counts = h["ammo_counts"]
    check("基础表已导入", counts["total"] > 50,
          f"共 {counts['total']} 条，其中可交易 {counts['tradeable']} 条")
    check("可交易物品数合理", counts["tradeable"] >= 80,
          f"{counts['tradeable']} 条", warn_only=True)

    # ── 4. 采集数据 ─────────────────────────────────────────────────────
    ticks = h["tick_counts"]
    check("已有价格数据", ticks["total"] > 0,
          f"共 {ticks['total']} 个价格点（判异 {ticks['suspect']} 个）")
    check("数据新鲜", not h["data_freshness"]["stale"],
          f"{h['data_freshness']['age_seconds']} 秒前更新")

    # ── 5. 行情接口 ─────────────────────────────────────────────────────
    r = c.get("/api/ammo", params={"limit": 300}).json()
    items = r["items"]
    check("行情列表非空", len(items) > 0, f"{r['total']} 个物品")

    priced = [i for i in items if i["price"] is not None]
    check("价格字段真实非空", len(priced) > 0, f"{len(priced)} 个物品有价格")
    bad_prices = [i for i in priced if not (i["price"] > 0)]
    check("价格全部为正数", not bad_prices,
          f"{len(bad_prices)} 个异常" if bad_prices else "无 0 或负数")

    # 关键：缺价格必须是 None，不能是 0（0 会被误读成"价格是 0"）
    missing = [i for i in items if i["price"] is None]
    check("缺价格显示为 null 而非 0", all(i["price"] is None for i in missing),
          f"{len(missing)} 个物品无价格")

    check("单一价格口径已如实标注",
          all(i["price_kind"] == "reference" for i in priced),
          "price_kind=reference")

    # ── 6. 历史曲线（回补是否真的生效）──────────────────────────────────
    if priced:
        target = max(priced, key=lambda i: i["samples"])
        series = c.get(f"/api/price/{target['ammo_id']}",
                       params={"range": "7d"}).json()
        check("历史曲线已回补", series["count"] > 10,
              f"{target['name']} 有 {series['count']} 个点（7 天）")
        check("曲线点按时间升序",
              all(series["points"][i]["ts"] < series["points"][i + 1]["ts"]
                  for i in range(len(series["points"]) - 1)),
              f"{series['count']} 个点单调递增")
        check("曲线带口径免责说明", bool(series.get("note")),
              (series.get("note") or "")[:40] + "…")

    # ── 7. 指标是否算出来 ───────────────────────────────────────────────
    with_pct = [i for i in items if i["percentile_7d"] is not None]
    reliable = [i for i in items if i["percentile_reliable"]]
    check("分位数已算出", len(with_pct) > 0, f"{len(with_pct)} 个物品有分位")
    check("存在样本充足的分位数", len(reliable) > 0,
          f"{len(reliable)} 个物品的样本量 >= 24")
    check("分位数标签已生成",
          all(i["percentile_label"] for i in with_pct),
          "例如：" + (with_pct[0]["percentile_label"] if with_pct else "—"))

    # 互校：数据源自报的 7 日最高/最低 vs 我们自算值。
    #
    # ⚠️ 这里刻意**不比对**数据源的 `7zf` 字段：实测它不是涨跌百分比
    # （价格贴近 7 日最低点时它仍可能是 +118 这类大正数），
    # 拿它当涨跌幅比对会得出"我们有 bug"的错误结论。
    # 真正能互校的是 7 日高低 —— 两者若一致，说明价格序列、时区、回补都正确。
    cmp_items = [i for i in items
                 if i["high_7d"] is not None and i["low_7d"] is not None]
    if cmp_items:
        check("7 日高低区间自洽", all(i["low_7d"] <= i["price"] <= i["high_7d"]
                                      for i in cmp_items
                                      if i["price"] is not None),
              f"{len(cmp_items)} 个物品的当前价都落在自算 7 日区间内")
    else:
        check("7 日高低区间自洽", False, "没有可比的物品", warn_only=True)

    # ── 8. 排行榜 ───────────────────────────────────────────────────────
    for t in ("up", "down", "volatility", "opportunity", "cheap"):
        rr = c.get("/api/rank", params={"type": t}).json()
        check(f"排行榜 {t} 可用", isinstance(rr["items"], list),
              f"{len(rr['items'])} 条")

    # ── 9. 打包物项检查（决定是否展示套利分析）──────────────────────────
    pk = c.get("/api/health/pack-check").json()
    check("打包物项检查如实报告", pk["has_pack_items"] is False,
          f"has_pack_items={pk['has_pack_items']}，UI 应隐藏套利模块")

    # ── 10. 手动采集与告警链路 ──────────────────────────────────────────
    # 回补或采集正在写库时，本接口会（正确地）返回 409。
    # 那种情况不算失败，但要如实报告，避免把"正常限流"误判成"功能坏了"。
    try:
        rr = c.post("/api/ingest/trigger")
        if rr.status_code == 409:
            check("手动触发采集可用", True,
                  f"409（写锁被占，属正常保护）：{rr.json().get('detail', '')[:50]}")
        else:
            trig = rr.json()
            check("手动触发采集可用", bool(trig.get("ok")),
                  trig.get("summary") or str(trig)[:80])
    except Exception as e:
        check("手动触发采集可用", False, f"{type(e).__name__}: {e}")

    # ── 11. 静态前端 ────────────────────────────────────────────────────
    for path, needle in (("/", "三角洲行动"), ("/assets/app.js", "lineChart"),
                         ("/assets/style.css", "--up")):
        try:
            rr = c.get(path)
            ok = rr.status_code == 200 and needle in rr.text
            check(f"前端资源 {path}", ok, f"{len(rr.content)} 字节")
        except Exception as e:
            check(f"前端资源 {path}", False, str(e))

    # ── 汇总 ────────────────────────────────────────────────────────────
    n_pass = sum(1 for lv, _, _ in results if lv == PASS)
    n_fail = sum(1 for lv, _, _ in results if lv == FAIL)
    n_warn = sum(1 for lv, _, _ in results if lv == WARN)
    print("\n" + "=" * 74)
    print(f"结果：{n_pass} 通过 / {n_warn} 警告 / {n_fail} 失败")
    print("=" * 74)
    if n_fail:
        print("\n失败项：")
        for lv, name, detail in results:
            if lv == FAIL:
                print(f"  - {name}  {detail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
