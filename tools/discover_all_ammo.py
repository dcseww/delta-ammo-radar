# -*- coding: utf-8 -*-
"""完整发现某公开接口里的全部弹药，并生成 ammo_catalog.yaml。

踩过的坑（这就是本脚本存在的理由）
----------------------------------
第一版按"完整口径字符串"切片，结果漏了 30+ 条弹药，原因是游戏内的命名
**不统一**：

    口径字段 second      实际物品名 name              问题
    ------------------  --------------------------  --------------------------
    7.62x35mm           .300BLK                     名字里根本没有这个口径串
    .45-70mm            45-70 Govt FMJ              名字丢了前导点
    5.56x45mm           5.56*45mm M855 APC          用 * 而不是 x
    4.6x30mm            4.6 x 30mm AP ST            加了空格
    5.7x28mm            5.7 x 28mm HT+              加了空格
    复合弓箭矢           玻纤柳叶箭矢                  完全不同的命名体系

而接口的 name 参数是**大小写不敏感的子串匹配**（不是分词、不是模糊搜索）。
所以正确的做法是：用**短关键词**（数字前缀，如 "5.56"、"7.62"、"45-70"）
做超集匹配，再在本地按 primary == 弹药 过滤。

流程
----
1. 广度采样：用一批数字/字母关键词撒网，建立弹药候选池；
2. 派生切片键：从候选池里的物品名提取数字前缀（去重），作为第二轮的精确切片键；
3. 二次切片：对每个切片键单独请求，把结果并入候选池；
4. 只保留 primary == 弹药，写出 ammo_catalog.yaml + 发现报告。

每次请求都落盘原始响应，并记录返回条数——**返回达到 100 说明该切片仍被截断**，
工具会明确警告，而不是假装取全了。

用法
----
    python tools/discover_all_ammo.py --url https://dergame.cn/api/Items/search
    python tools/discover_all_ammo.py --url ... --seed-file tools/seed_keys.json
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
CAP = 100  # 服务端硬上限，实测得到

TIER_NAMES = {"1": "白", "2": "绿", "3": "蓝", "4": "紫", "5": "金", "6": "红"}

# 第一轮撒网用的种子关键词。
#
# 设计原则：**宁可窄，不要宽**。
# 实测教训：种子用 "9" 的时候返回 100 条（打满上限），里面混着装备/配件，
# 结果把一部分 9mm 弹药挤了出去；换成 "9mm" 反而多捞出 15 条弹药。
# 所以种子应当是"够窄以避开上限、够宽以覆盖命名变体"的关键词。
SEED_KEYS = [
    # 霰弹与大口径
    "Gauge", "12.7", ".50", ".338", ".357", "45-70", ".45",
    # 小口径步枪弹（含 * 与 x 两种写法，靠数字前缀覆盖）
    "4.6", "5.45", "5.56", "5.7", "5.8", "6.8", "7.62", "9x", "9mm",
    # 特殊命名
    "300BLK", ".300", "9x39", "箭矢",
    # 英文型号后缀（用于兜底覆盖）
    "ACP", "BMG", "Lap Mag", "Magnum", "AP", "SUB", "CT",
]

# 从物品名里提取"数字口径前缀"的正则（如 5.56*45mm -> 5.56；9*39mm -> 9；300BLK -> 300）
NUM_PREFIX = re.compile(r"(\.?\d+(?:\.\d+)?)")
# 带字母前缀的口径（如 300BLK、5.56x），用于派生更精确的切片键
ALNUM_PREFIX = re.compile(r"(\.?\d+(?:\.\d+)?\s?[A-Za-z]{1,4})")


def slugify(text: str) -> str:
    """把物品名变成稳定的内部 ID。

    ⚠️ 必须**保留中文字符**。初版只保留 ASCII 字母数字，于是
    `碳纤维刺骨箭矢` 与 `碳纤维穿甲箭矢` 都被压成 `unknown` 而撞 ID ——
    两个不同的物品共用一个 ammo_id，价格会互相覆盖。
    """
    s = text.strip().lower()
    # 保留：字母、数字、CJK 统一表意文字；其余替换为下划线
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", s).strip("_")
    s = re.sub(r"_{2,}", "_", s)
    return s or "unknown"


def build_catalog(by_name: dict[str, list[dict]]) -> list[dict]:
    """把"按名称分组"的原始记录转成基础表条目。

    核心规则：**一个 (名称, 数据源ID) 组合 = 一个独立条目**。
    同名不同品质必须拆开，否则"同一个物品"的价格会在轮询之间漂移。

    这个函数被抽出来是为了可单测 —— 它产出的 ammo_id 是整个项目的主键，
    出错不会报错，只会静默污染价格序列（实测踩过两次，见 tests/test_catalog_build.py）。
    """
    catalog: list[dict] = []
    seen_ids: set[str] = set()
    for name in sorted(by_name, key=lambda s: (str(by_name[s][0].get("second") or ""), s)):
        recs = by_name[name]
        # 每个数据源 ID 单独成条目；按价格从高到低排（高品质通常更贵）
        recs_sorted = sorted(recs, key=lambda r: -((r.get("value") or {}).get("new") or 0))
        for rep in recs_sorted:
            grade = str(rep.get("grade") or "")
            aid = slugify(name)
            # 同名多条时必须加品质后缀，否则会互相覆盖
            if len(recs_sorted) > 1:
                aid = f"{aid}_g{grade}" if grade else f"{aid}_{str(rep.get('id'))[-4:]}"
            if aid in seen_ids:
                # 兜底：slug 天然相同但确实是不同物品时，用数据源 ID 后缀保证唯一
                aid = f"{aid}_{str(rep.get('id'))[-4:]}"
            seen_ids.add(aid)
            catalog.append({
                "ammo_id": aid,
                "name": name,
                "caliber": rep.get("second"),
                "tier": TIER_NAMES.get(grade, "?"),
                "grade": grade,
                "is_pack": 0,
                "pack_size": 1,
                "length": rep.get("length"),
                "width": rep.get("width"),
                "tradeable": 1 if (rep.get("value") or {}).get("new") is not None else 0,
                "external_ids": {"dergame": [str(rep.get("id"))]},
                "enabled": 1,
            })
    return catalog


def derive_slice_keys(names: list[str]) -> list[str]:
    """从已观测到的物品名里派生切片键。

    派生两类键：
    1. 纯数字前缀（5.56 / 7.62 / 45-70），以及去掉前导点的变体；
    2. 数字+字母前缀（9mm / 300BLK / 5.56x），用于收窄过宽的纯数字键。

    刻意跳过单个数字：实测 "9" 会返回 100 条并打满上限，反而丢数据。
    """
    keys: list[str] = []
    for name in names:
        cands = [m.group(1) for m in NUM_PREFIX.finditer(name)]
        cands += [m.group(1) for m in ALNUM_PREFIX.finditer(name)]
        for tok in cands:
            tok = tok.strip()
            if len(tok) < 2:      # 单个数字太宽泛，会瞬间打满 100 条上限
                continue
            for variant in (tok, tok.lstrip(".")):
                if variant and len(variant) >= 2 and variant not in keys:
                    keys.append(variant)
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="完整发现全部弹药并生成 ammo_catalog.yaml。")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="backend/catalog/ammo_catalog.yaml")
    ap.add_argument("--report", default="data/fixtures/discovery-full-report.json")
    ap.add_argument("--raw-dir", default="data/fixtures/discovery-full")
    ap.add_argument("--type-value", default="弹药")
    ap.add_argument("--seed-file", default=None, help="额外的种子关键词 JSON 数组")
    ap.add_argument("--delay", type=float, default=1.4)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--max-keys", type=int, default=60, help="切片键上限，防止请求过多")
    args = ap.parse_args(argv)

    seeds = list(SEED_KEYS)
    if args.seed_file:
        seeds += json.loads(Path(args.seed_file).read_text(encoding="utf-8"))
    seeds = list(dict.fromkeys(s for s in seeds if s))

    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}
    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    pool: dict[tuple, dict] = {}     # (name, id) -> 原始记录
    rounds: list[dict] = []

    def call(keyword: str | None, label: str) -> list[dict]:
        payload: dict = {"type": [args.type_value]}
        if keyword:
            payload["name"] = keyword
        time.sleep(args.delay)
        try:
            r = httpx.post(args.url, json=payload, headers=headers,
                           timeout=args.timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
            rounds.append({"label": label, "keyword": keyword, "returned": -1, "error": str(e)})
            return []
        if r.status_code != 200:
            print(f"  [FAIL] {label}: HTTP {r.status_code}")
            rounds.append({"label": label, "keyword": keyword, "returned": -1,
                           "error": f"HTTP {r.status_code}"})
            return []
        try:
            obj = r.json()
        except json.JSONDecodeError:
            print(f"  [FAIL] {label}: 非 JSON")
            rounds.append({"label": label, "keyword": keyword, "returned": -1, "error": "非 JSON"})
            return []

        recs = obj.get("data") or []
        ammo = [x for x in recs if isinstance(x, dict) and x.get("primary") == args.type_value]
        new = 0
        for a in ammo:
            k = (a.get("name"), a.get("id"))
            if k not in pool:
                new += 1
            pool[k] = a

        (raw_dir / f"{label.replace('/', '_').replace(' ', '_')[:60]}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        rounds.append({"label": label, "keyword": keyword, "payload": payload,
                       "returned_total": len(recs), "returned_ammo": len(ammo),
                       "new_ammo": new, "capped": len(recs) >= CAP})
        return ammo

    # ── 第一轮：种子撒网 ────────────────────────────────────────────────
    print("=" * 78)
    print(f"第一轮：种子关键词撒网（{len(seeds)} 个）")
    print("=" * 78)
    call(None, "seed-all")
    for k in seeds:
        ammo = call(k, f"seed-{k}")
        mark = "  <== 达上限" if len(ammo) >= CAP else ""
        print(f"  {k:12s} 弹药 {len(ammo):4d}  池内累计 {len(pool):4d}{mark}")

    # ── 第二轮：从物品名派生的精确切片键 ────────────────────────────────
    names = [n for (n, _) in pool if n]
    keys = derive_slice_keys(names)[: args.max_keys]
    keys = [k for k in keys if k not in seeds]

    print("\n" + "=" * 78)
    print(f"第二轮：派生切片键 {len(keys)} 个（去重后）")
    print("=" * 78)
    for k in keys:
        before = len(pool)
        ammo = call(k, f"slice-{k}")
        added = len(pool) - before
        mark = "  <== 达上限" if len(ammo) >= CAP else ""
        if added or len(ammo) >= CAP:
            print(f"  {k:12s} 弹药 {len(ammo):4d}  新增 {added:4d}{mark}")

    # ── 第三轮：口径覆盖率自检 ──────────────────────────────────────────
    # 用"口径 token"再搜一遍。口径（second）往往与物品名写法不同，
    # 这一轮能查出"某口径只发现了部分型号"的情况 —— 这是宽泛关键词被
    # 100 条上限截断后最容易出现的漏数据形态。
    cal_tokens: list[str] = []
    for c in set(str(r.get("second")) for r in pool.values()):
        if not c or c in ("None", "弹匣"):
            continue
        t = c.split()[0].strip()
        if len(t) >= 3 and t not in cal_tokens:
            cal_tokens.append(t)
        for m in NUM_PREFIX.finditer(c):
            v = m.group(1)
            if len(v) >= 3 and v not in cal_tokens:
                cal_tokens.append(v)

    cal_tokens = [t for t in cal_tokens if t not in seeds and t not in keys][:25]
    print("\n" + "=" * 78)
    print(f"第三轮：口径覆盖率自检（{len(cal_tokens)} 个口径 token）")
    print("=" * 78)
    gaps: list[tuple[str, int]] = []
    for t in cal_tokens:
        before = len(pool)
        ammo = call(t, f"verify-{t}")
        added = len(pool) - before
        if added:
            gaps.append((t, added))
            print(f"  {t:14s} 弹药 {len(ammo):4d}  新增 {added:4d}  <== 之前遗漏")
    if gaps:
        total_gap = sum(a for _, a in gaps)
        print(f"\n[自检] 又补回 {total_gap} 条此前遗漏的弹药，涉及 {len(gaps)} 个口径。")
        print("       说明前面的切片确实被上限截断了；已并入最终结果。")
    else:
        print("  没有发现遗漏 —— 各口径的型号已取全。")

    # ── 汇总 ────────────────────────────────────────────────────────────
    items = list(pool.values())
    print("\n" + "=" * 78)
    print(f"最终发现弹药条目：{len(items)}")
    print("=" * 78)

    by_name: dict[str, list[dict]] = {}
    for r in items:
        by_name.setdefault(str(r.get("name")), []).append(r)

    priced = sum(1 for r in items if (r.get("value") or {}).get("new") is not None)
    tier_dist = Counter(TIER_NAMES.get(str(r.get("grade")), "?") for r in items)
    cal_dist = Counter(str(r.get("second")) for r in items)
    capped = [r for r in rounds if r.get("capped")]

    print(f"唯一物品名：{len(by_name)}   条目数（含同名不同品质）：{len(items)}")
    print(f"有价格：{priced}/{len(items)} ({priced / len(items) * 100:.1f}%)")
    print(f"品质分布：{dict(sorted(tier_dist.items()))}")
    print(f"口径数：{len(cal_dist)}")

    dupes = {n: len(v) for n, v in by_name.items() if len(v) > 1}
    if dupes:
        extra = sum(c - 1 for c in dupes.values())
        print(f"\n[注意] 有 {len(dupes)} 个名字对应多条记录（同名不同品质/型号）：")
        for n, c in sorted(dupes.items(), key=lambda x: -x[1])[:10]:
            print(f"   {n!r} x {c}")
        print(f"   已按 (名称, 数据源ID) 拆成独立条目（共多出 {extra} 条）。")
        print("   原因：这些是同名但不同品质的弹药，价格差异极大；")
        print("         若共用一个 ammo_id，价格来源会在轮询间漂移并产生假跳变。")

    if capped:
        print(f"\n[警告] {len(capped)} 个切片返回达 {CAP} 条上限，可能仍有遗漏：")
        for r in capped[:15]:
            print(f"   {r['label']}  returned={r['returned_total']}")
        print("   应对：给这些关键词再加数字细分（如 '5.56' -> '5.56*'、'5.56x'）。")
    else:
        print("\n[OK] 没有任何切片触及 100 条上限，覆盖较为完整。")

    # ── 写出基础表 ─────────────────────────────────────────────────────
    # 规则与原因见 build_catalog 的 docstring（同名不同品质必须拆开）。
    catalog = build_catalog(by_name)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # ★ 唯一性自检：ammo_id 是主键，撞了就会让两个物品互相覆盖价格。
    # 必须在写文件之前拦住，否则问题会以"价格莫名跳变"的形式在很久以后才暴露。
    ids = [c["ammo_id"] for c in catalog]
    dup_ids = [i for i, n in Counter(ids).items() if n > 1]
    if dup_ids:
        raise SystemExit(
            f"生成失败：ammo_id 出现重复 {dup_ids}。\n"
            f"这会让不同物品共用一条价格序列并互相覆盖。\n"
            f"请检查 slugify 是否丢字符，或为冲突项追加数据源 ID 后缀。")

    header = (
        "# 三角洲行动 · 子弹基础表（ammo_catalog.yaml）\n"
        "#\n"
        "# 由 tools/discover_all_ammo.py 从公开只读接口自动生成。\n"
        "# 这是整个项目**唯一需要人工核对**的数据文件。\n"
        "#\n"
        f"# 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# 数据来源：{args.url}\n"
        f"# 物品名数：{len(catalog)}   含价格：{priced}\n"
        "#\n"
        "# 人工核对要点：\n"
        "#   1. is_pack / pack_size —— 打包出售（子弹自选包、弹匣）必须填对，\n"
        "#      否则单发价折算错误，所有收益分析都会跟着错。\n"
        "#   2. tradeable —— 不可交易的物品保留但置 0，采集时跳过。\n"
        "#   3. enabled   —— 不关心的置 0，减少请求与存储。\n"
        "#   4. 同名多条（variants）—— 表示游戏内同名的不同品质/型号，\n"
        "#      核对时确认是否应合并或拆成不同 ammo_id。\n"
    )
    if yaml is None:
        print("\n[FAIL] 未安装 PyYAML，改写 JSON 兜底。")
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
        "total_records": len(items),
        "unique_names": len(by_name),
        "priced": priced,
        "tier_distribution": dict(sorted(tier_dist.items())),
        "caliber_distribution": dict(cal_dist.most_common()),
        "duplicate_names": dupes,
        "capped_rounds": capped,
        "rounds": rounds,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"发现报告：{rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
