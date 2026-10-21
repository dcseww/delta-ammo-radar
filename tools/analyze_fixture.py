# -*- coding: utf-8 -*-
"""分析已落盘的接口响应 fixture，摸清字段结构与数据覆盖率。

为什么需要它
------------
"公开页面只读解析"这条路线的成败，取决于三个问题：
1. 接口到底返回哪些字段？（决定数据模型怎么建）
2. 有多少条目标数据是**有值**的？（决定这个源值不值得用）
3. 数据里的异常/null 长什么样？（决定校验规则怎么写）

本脚本对一份已落盘的 JSON fixture 做统计，把答案算出来，而不是靠眼看。

用法
----
    python tools/analyze_fixture.py --file data/fixtures/xxx.raw
    python tools/analyze_fixture.py --file xxx.raw --filter-field primary --filter-value 弹药
    python tools/analyze_fixture.py --file xxx.raw --numeric value.new
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def walk_types(obj, prefix: str = "", depth: int = 0, max_depth: int = 4, acc=None):
    """递归收集所有字段路径及其出现过的类型。"""
    if acc is None:
        acc = {}
    if depth > max_depth:
        return acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            acc.setdefault(path, set())
            acc[path].add(type(v).__name__)
            if isinstance(v, (dict, list)):
                walk_types(v, path, depth + 1, max_depth, acc)
    elif isinstance(obj, list):
        acc.setdefault(prefix + "[]", set()).add(f"list[{len(obj)}]")
        for item in obj[:200]:
            walk_types(item, prefix, depth + 1, max_depth, acc)
    return acc


def dig(obj, path: str):
    """按 'a.b.c' 取值，任一层缺失返回 None。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="统计已落盘 fixture 的字段结构与数据覆盖率。")
    ap.add_argument("--file", required=True, help="fixture 文件（.raw 或 .json）")
    ap.add_argument("--list-path", default=None, help="数据数组所在路径，如 data；默认自动探测")
    ap.add_argument("--filter-field", default=None, help="只统计某字段等于某值的记录")
    ap.add_argument("--filter-value", default=None)
    ap.add_argument("--categorical", action="append", default=[], help="要统计分布的字段路径，可重复")
    ap.add_argument("--numeric", action="append", default=[], help="要统计数值分布的字段路径，可重复")
    ap.add_argument("--show", type=int, default=3, help="每种分布展示前 N 项（默认 3）")
    args = ap.parse_args(argv)

    raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[FAIL] 不是合法 JSON：{e}")
        return 1

    print("=" * 78)
    print(f"文件：{args.file}")
    print(f"顶层类型：{type(obj).__name__}")
    if isinstance(obj, dict):
        print(f"顶层字段：{list(obj.keys())}")
    print("=" * 78)

    # ── 定位记录数组 ────────────────────────────────────────────────────
    records = None
    where = ""
    if args.list_path:
        records = dig(obj, args.list_path)
        where = args.list_path
    else:
        for cand in ("data", "items", "list", "result", "rows"):
            v = dig(obj, cand) if isinstance(obj, dict) else None
            if isinstance(v, list) and v:
                records = v
                where = cand
                break
        if records is None and isinstance(obj, list):
            records, where = obj, "(顶层数组)"

    if not isinstance(records, list):
        print("没能定位到记录数组。请用 --list-path 指定，例如 --list-path data.items")
        print("\n字段结构总览：")
        for path, types in sorted(walk_types(obj).items()):
            print(f"   {path:52s} {sorted(types)}")
        return 0

    print(f"记录数组位置：{where}    共 {len(records)} 条\n")

    # ── 字段结构 ────────────────────────────────────────────────────────
    print("--- 字段结构（前 200 条采样）---")
    types_map = walk_types(records[:200])
    for path, types in sorted(types_map.items()):
        print(f"   {path:52s} {sorted(types)}")

    # ── 过滤 ────────────────────────────────────────────────────────────
    if args.filter_field:
        before = len(records)
        records = [r for r in records if str(dig(r, args.filter_field)) == str(args.filter_value)]
        print(f"\n--- 过滤 {args.filter_field} == {args.filter_value}：{before} -> {len(records)} 条 ---")

    # ── 分类分布 ────────────────────────────────────────────────────────
    for field in args.categorical:
        c: Counter = Counter()
        missing = 0
        for r in records:
            v = dig(r, field)
            if v is None or v == "":
                missing += 1
            else:
                c[str(v)] += 1
        print(f"\n--- 分布：{field}（缺失 {missing} 条，共 {len(records)}）---")
        for val, n in c.most_common(args.show):
            pct = n / len(records) * 100 if records else 0
            print(f"   {val:28s} {n:5d} 条  ({pct:.1f}%)")
        if len(c) > args.show:
            print(f"   ... 另有 {len(c) - args.show} 个不同取值")

    # ── 数值覆盖率（最关键：决定这个源有没有用）──────────────────────────
    for field in args.numeric:
        total = len(records)
        present, nulls = 0, 0
        vals: list[float] = []
        for r in records:
            v = dig(r, field)
            if v is None:
                nulls += 1
                continue
            present += 1
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        print(f"\n--- 数值覆盖：{field} ---")
        if total:
            print(f"   有效值 {present}/{total} ({present / total * 100:.1f}%)   空值 {nulls}")
        if vals:
            vals.sort()
            n = len(vals)
            print(f"   min={vals[0]:.2f}  p25={vals[n // 4]:.2f}  中位={vals[n // 2]:.2f}  "
                  f"p75={vals[n * 3 // 4]:.2f}  max={vals[-1]:.2f}")
            print(f"   负值 {sum(1 for v in vals if v < 0)} 条 / 零值 {sum(1 for v in vals if v == 0)} 条")
        else:
            print("   没有任何有效数值 —— 这个字段不可用。")

    # ── 结论提示 ────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("判读提示")
    print("=" * 78)
    print(" 1. 若关键价格字段的『有效值比例』接近 100%，说明这个源可以作为主数据源。")
    print(" 2. 若大量为空，说明该源只覆盖部分物品，需要保留 static_fixture 兜底。")
    print(" 3. grade / primary / second 这类低基数字段适合做前端筛选维度，应进 ammo 表。")
    print(" 4. length × width 可用于计算单格价值，是筛『值不值得捡』的关键指标。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
