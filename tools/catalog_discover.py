# -*- coding: utf-8 -*-
"""把一个只读接口的"弹药"数据完整取回，生成子弹基础表（ammo_catalog.yaml）。

要解决的问题
------------
dergame 的 /api/Items/search 一次最多返回 100 条（服务端硬上限，无分页参数），
其中 primary=弹药 的只有 85 条 —— 也就是说**弹药数据被截断了**，直接抓一次
会漏掉物品。

解决思路：**按口径切片**。
弹药名称有稳定的命名规律：`<口径> <型号>`（例如 `5.56x45mm M855`、
`.338 Lap Mag AP`）。接口的 `name` 参数是**子串匹配**，所以：
    对每个已知口径发一次 {"type":["弹药"],"name":"5.56x45mm"}
每次返回都在 100 条以内，合并去重后即可覆盖全量。
口径列表来自上一轮的探测结果（tools/experiments/dergame_filters.json），
并支持 --extra-caliber 手工补充。

产出
----
- ammo_catalog.yaml : 子弹基础表（项目唯一需要人工核对的文件）
- discovery-report.json : 每个口径切片的返回条数与新增条数，便于判断是否取全

用法
----
    python tools/catalog_discover.py --url https://dergame.cn/api/Items/search
    python tools/catalog_discover.py --url ... --extra-caliber "7.62x25mm"
    python tools/catalog_discover.py --url ... --out backend/catalog/ammo_catalog.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 httpx。请先安装：pip install httpx")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_UA = "df-ammo-radar-probe/0.1 (personal read-only research; contact: local user)"

# 已通过实测观测到的口径（来自 /api/Items/search 的 second 字段分布）。
# 这些是"切片键"，用于突破 100 条上限。
DEFAULT_CALIBERS = [
    "12 Gauge", ".45 ACP", "5.45x39mm", "5.56x45mm", "5.7x28mm", "7.62x39mm",
    "4.6x30mm", "5.8x42mm", "7.62x51mm", ".357 Magnum", "12.7x55mm", "6.8x51mm",
    "7.62x35mm", ".50 AE", "7.62x54mm", "9x19mm", ".45-70mm", ".50 BMG",
    "9x39mm", ".338 Lap Mag", "弹匣",
]

TIER_NAMES = {"1": "白", "2": "绿", "3": "蓝", "4": "紫", "5": "金", "6": "红"}


def slugify(text: str) -> str:
    """把物品名变成稳定的内部 ID，只保留 ASCII 字母数字与下划线。"""
    s = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    return re.sub(r"_{2,}", "_", s) or "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按口径切片取全弹药数据，生成 ammo_catalog.yaml。")
    ap.add_argument("--url", required=True, help="只读接口地址")
    ap.add_argument("--out", default="backend/catalog/ammo_catalog.yaml")
    ap.add_argument("--report", default="data/fixtures/discovery-report.json")
    ap.add_argument("--raw-dir", default="data/fixtures/discovery")
    ap.add_argument("--extra-caliber", action="append", default=[], help="补充口径，可重复")
    ap.add_argument("--type-value", default="弹药", help="type 字段的取值（默认 弹药）")
    ap.add_argument("--delay", type=float, default=1.5, help="请求间隔（默认 1.5s，请保持礼貌）")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--min-caliber", type=int, default=3, help="口径字符串最短长度，过滤噪声")
    args = ap.parse_args(argv)

    calibers = []
    for c in DEFAULT_CALIBERS + args.extra_caliber:
        c = c.strip()
        if c and len(c) >= args.min_caliber and c not in calibers:
            calibers.append(c)

    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"按口径切片取全弹药：{args.url}")
    print(f"切片数：{len(calibers)}   间隔：{args.delay}s")
    print("=" * 78)

    items: dict[str, dict] = {}   # name -> 合并后的记录
    rounds: list[dict] = []

    def absorb(recs: list[dict], source_label: str) -> int:
        added = 0
        for r in recs:
            name = r.get("name")
            if not name:
                continue
            if name not in items:
                added += 1
                items[name] = {
                    "name": name,
                    "source_ids": [],
                    "primary": r.get("primary"),
                    "caliber": r.get("second"),
                    "grade": str(r.get("grade") or ""),
                    "tier": TIER_NAMES.get(str(r.get("grade")), "?"),
                    "length": r.get("length"),
                    "width": r.get("width"),
                    "pic": r.get("pic"),
                    "last_value": r.get("value") or {},
                    "found_by": [],
                }
            if source_label not in items[name]["found_by"]:
                items[name]["found_by"].append(source_label)
            sid = r.get("id")
            if sid and sid not in items[name]["source_ids"]:
                items[name]["source_ids"].append(sid)
        return added

    def call(payload: dict, label: str) -> tuple[int, int]:
        time.sleep(args.delay)
        try:
            r = httpx.post(args.url, json=payload, headers=headers,
                           timeout=args.timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
            return -1, 0
        if r.status_code != 200:
            print(f"  [FAIL] {label}: HTTP {r.status_code}")
            return -1, 0
        try:
            obj = r.json()
        except json.JSONDecodeError:
            print(f"  [FAIL] {label}: 响应不是 JSON")
            return -1, 0

        recs = obj.get("data") or []
        # 严格只收弹药：接口在 type 未命中时会退回返回混合数据
        ammo = [x for x in recs if isinstance(x, dict) and x.get("primary") == args.type_value]
        added = absorb(ammo, label)

        (raw_dir / f"{label.replace('/', '_').replace(' ', '_')[:60]}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(ammo), added

    # ── 先取一次全量作为基线 ────────────────────────────────────────────
    print("\n[基线] type=弹药（会被 100 条上限截断，仅作对照）")
    n, added = call({"type": [args.type_value]}, "baseline-type-ammo")
    print(f"  返回弹药 {n} 条，新增 {added} 条")
    rounds.append({"label": "baseline-type-ammo", "returned": n, "added": added})

    # ── 按口径逐个切片 ─────────────────────────────────────────────────
    print("\n[切片] 逐口径请求")
    for c in calibers:
        n, added = call({"type": [args.type_value], "name": c}, f"caliber-{c}")
        flag = "  <-- 达上限，可能仍有遗漏" if n >= 100 else ""
        print(f"  {c:16s} 返回 {n:4d}  新增 {added:4d}{flag}")
        rounds.append({"label": f"caliber-{c}", "returned": n, "added": added})

    # ── 汇总 ────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print(f"共发现 {len(items)} 种弹药")
    print("=" * 78)

    tier_dist = Counter(v["tier"] for v in items.values())
    cal_dist = Counter(v["caliber"] for v in items.values())
    priced = sum(1 for v in items.values() if (v["last_value"] or {}).get("new") is not None)

    print(f"品质分布：{dict(sorted(tier_dist.items()))}")
    print(f"口径数量：{len(cal_dist)}  覆盖最多的：{cal_dist.most_common(5)}")
    print(f"有价格的：{priced}/{len(items)} ({priced / len(items) * 100:.1f}%)")

    truncated = [r for r in rounds if r["returned"] >= 100 and r["label"] != "baseline-type-ammo"]
    if truncated:
        print(f"\n[注意] 有 {len(truncated)} 个切片返回条数达上限，可能仍有遗漏：")
        for r in truncated:
            print(f"   {r['label']}  returned={r['returned']}")
        print("   建议：对这些口径再加更细的关键词（如型号数字）继续切片。")

    # ── 写出 ammo_catalog.yaml ─────────────────────────────────────────
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    catalog = []
    for name in sorted(items, key=lambda s: (items[s]["caliber"] or "", s)):
        v = items[name]
        catalog.append({
            "ammo_id": slugify(name),
            "name": name,
            "caliber": v["caliber"],
            "tier": v["tier"],
            "grade": v["grade"],
            "is_pack": 1 if (v["caliber"] == "弹匣" or "包" in name) else 0,
            "pack_size": 1,
            "length": v["length"],
            "width": v["width"],
            "external_ids": {"dergame": v["source_ids"]},
            "enabled": 1,
        })

    header = (
        "# 三角洲行动 · 子弹基础表（ammo_catalog.yaml）\n"
        "#\n"
        "# 本文件由 tools/catalog_discover.py 从公开只读接口自动生成，\n"
        "# 是整个项目**唯一需要人工核对**的数据文件。\n"
        "#\n"
        "# 生成时间：{}\n"
        "# 数据来源：{}\n"
        "# 条目数：{}\n"
        "#\n"
        "# 人工核对要点：\n"
        "#   1. pack_size —— 打包出售的物品要填真实发数，否则单发价折算会错。\n"
        "#   2. is_pack   —— 子弹自选包 / 弹匣要置 1。\n"
        "#   3. enabled   —— 不关心的条目置 0，采集时跳过，省请求也省数据库。\n"
        "#   4. caliber   —— 用于前端筛选；空值请补全。\n"
    ).format(time.strftime("%Y-%m-%d %H:%M:%S"), args.url, len(catalog))

    if yaml is None:
        print("\n[FAIL] 未安装 PyYAML，无法写出 YAML。请先 pip install pyyaml")
        print("       作为兜底，已写出 JSON 版本。")
        out.with_suffix(".json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out.write_text(header + yaml.safe_dump(
            catalog, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
        print(f"\n已写出基础表：{out}")

    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": args.url,
        "total_items": len(items),
        "priced_items": priced,
        "tier_distribution": dict(sorted(tier_dist.items())),
        "caliber_distribution": dict(cal_dist.most_common()),
        "rounds": rounds,
        "items": catalog,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"发现报告：{rep}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
