# -*- coding: utf-8 -*-
"""用一个参数矩阵多次调用同一个只读接口，摸清它的分页/过滤边界与全量取数方式。

为什么需要它
------------
公开接口常常有**硬上限**（例如一次最多返回 100 条），但没有显式分页参数。
本项目的目标物品（弹药）数量可能超过该上限，所以必须搞清：
「能不能用 type/grade 之类的过滤把全量切片取回来」。

本脚本就是做这件事：把同一组参数矩阵依次请求、统计每条返回多少条、
落盘每一份响应，最后给出结论（上限是多少、哪种切片方式能覆盖全量）。

设计原则：只读、串行、带延时，绝不高频打公开接口。

用法
----
    python tools/probe_matrix.py --url https://example.com/api/Items/search \
        --payload '{}' --payload '{"grade":["6"]}' --payload '{"type":["弹药"]}'
    python tools/probe_matrix.py --url ... --matrix-file payloads.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 httpx。请先安装：pip install httpx")

DEFAULT_UA = "df-ammo-radar-probe/0.1 (personal read-only research; contact: local user)"


def find_records(obj, path: str | None):
    """定位记录数组：优先用指定路径，否则自动探测常见字段名。"""
    if path:
        cur = obj
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return None
        return cur if isinstance(cur, list) else None
    for cand in ("data", "items", "list", "result", "rows"):
        v = obj.get(cand) if isinstance(obj, dict) else None
        if isinstance(v, list):
            return v
    return obj if isinstance(obj, list) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="参数矩阵探测器：摸清只读接口的上限与切片方式。")
    ap.add_argument("--url", required=True)
    ap.add_argument("--payload", action="append", default=[], help="JSON 字符串，可重复")
    ap.add_argument("--matrix-file", default=None, help="从 JSON 文件读取 payload 列表")
    ap.add_argument("--list-path", default="data", help="记录数组路径（默认 data）")
    ap.add_argument("--out", default="data/fixtures/matrix", help="落盘目录")
    ap.add_argument("--delay", type=float, default=1.2, help="每次请求间隔秒数（默认 1.2，请保持礼貌）")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--field", default="name", help="去重用的记录标识字段（默认 name）")
    args = ap.parse_args(argv)

    payloads: list[dict] = []
    for p in args.payload:
        payloads.append(json.loads(p))
    if args.matrix_file:
        payloads.extend(json.loads(Path(args.matrix_file).read_text(encoding="utf-8")))
    if not payloads:
        payloads = [{}]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    host = urlparse(args.url).netloc or "unknown"
    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}

    print("=" * 78)
    print(f"目标：{args.url}")
    print(f"共 {len(payloads)} 组参数，间隔 {args.delay}s")
    print("=" * 78)

    results: list[tuple[str, int, int, str]] = []
    seen_ids: set[str] = set()
    per_call_ids: list[set[str]] = []

    for idx, payload in enumerate(payloads):
        label = json.dumps(payload, ensure_ascii=False)
        time.sleep(args.delay)
        try:
            r = httpx.post(args.url, json=payload, headers=headers, timeout=args.timeout,
                           follow_redirects=True)
        except httpx.HTTPError as e:
            print(f"[{idx + 1}/{len(payloads)}] {label}\n    [FAIL] {type(e).__name__}: {e}")
            results.append((label, -1, 0, "请求失败"))
            continue

        if r.status_code != 200:
            print(f"[{idx + 1}/{len(payloads)}] {label}\n    [FAIL] HTTP {r.status_code}")
            results.append((label, r.status_code, 0, "非 200"))
            continue

        try:
            obj = r.json()
        except json.JSONDecodeError:
            print(f"[{idx + 1}/{len(payloads)}] {label}\n    [FAIL] 响应不是 JSON")
            results.append((label, 200, 0, "非 JSON"))
            continue

        recs = find_records(obj, args.list_path)
        n = len(recs) if isinstance(recs, list) else -1
        ids = set()
        if isinstance(recs, list):
            for rec in recs:
                if isinstance(rec, dict):
                    v = rec.get(args.field)
                    if v is not None:
                        ids.add(str(v))
            seen_ids |= ids
        per_call_ids.append(ids)

        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        (out_dir / f"{host}-{idx + 1:02d}-{safe}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"[{idx + 1}/{len(payloads)}] {label}")
        print(f"    条数={n}  唯一{args.field}={len(ids)}  updateTime={obj.get('updateTime', '-')}")
        results.append((label, 200, n, f"唯一 {len(ids)}"))

    # ── 汇总 ────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"{'参数':<46} {'条数':>6}  备注")
    for label, status, n, note in results:
        print(f"{label[:46]:<46} {n:>6}  {note}")

    counts = [n for _, s, n, _ in results if s == 200 and n >= 0]
    if counts:
        print(f"\n最大条数 = {max(counts)}；出现该条数的调用次数 = {counts.count(max(counts))}")
        if counts.count(max(counts)) > 1 and max(counts) > 1:
            print("=> 多次调用都卡在同一数字，几乎可以确定这是**服务端硬上限**。")
            print("   应对：用过滤条件把数据切片取回，再在本地合并去重。")
        elif len(results) == 1:
            print("=> 只探测了一次，无法判断上限。建议再用不同过滤条件探测几次。")

    print(f"\n所有响应已落盘：{out_dir}")
    print(f"多次调用累计唯一 {args.field} 数 = {len(seen_ids)}")

    union = set()
    for s in per_call_ids:
        union |= s
    if union:
        print(f"参数矩阵合并后覆盖 {len(union)} 个不同 {args.field}。")
        print("=> 若该数字明显大于单次上限，说明**切片取全是可行的**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
