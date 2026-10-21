# -*- coding: utf-8 -*-
"""可复用的数据源实验台。

为什么需要它
------------
探测阶段最大的效率瓶颈是"一次运行只验证一个假设"，而且命令行工具的输出
会被截断（只保留尾部），导致一次跑太多实验就看不到全过程。

本脚本把实验结果**结构化写入 JSON 报告 + 逐个落盘原始响应**，终端只打印
简短结论。这样一次可以跑很多轮实验，结论永远完整可查。

它做什么
--------
对一个只读接口依次发送多组参数（实验轮次），每轮记录：
- 请求体、HTTP 状态、返回条数
- 关键字段的分布（primary / grade / second）
- 与之前轮次的 name 集合做交集/并集，用于判断数据切片是否重叠

用法
----
    python tools/experiment.py --spec tools/experiments/dergame_filters.json
    python tools/experiment.py --spec ... --report data/fixtures/exp-report.json
"""

from __future__ import annotations

import argparse
import json
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

DEFAULT_UA = "df-ammo-radar-probe/0.1 (personal read-only research; contact: local user)"


def load_spec(path: str) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if "url" not in spec or "rounds" not in spec:
        raise SystemExit("spec 文件必须包含 'url' 与 'rounds' 两个字段。")
    return spec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="结构化数据源实验台。")
    ap.add_argument("--spec", required=True, help="实验定义 JSON 文件")
    ap.add_argument("--report", default="data/fixtures/experiment-report.json")
    ap.add_argument("--raw-dir", default="data/fixtures/experiments")
    ap.add_argument("--delay", type=float, default=None, help="覆盖 spec 中的请求间隔")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)
    delay = args.delay if args.delay is not None else spec.get("delay_seconds", 1.5)
    list_path = spec.get("list_path", "data")
    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}

    raw_dir = Path(args.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"实验台：{spec.get('name', '(未命名)')}")
    print(f"目标  ：{spec['url']}")
    print(f"轮次  ：{len(spec['rounds'])}  间隔：{delay}s")
    print("=" * 78)

    report: list[dict] = []
    seen: set[str] = set()
    per_round: list[tuple[str, set[str]]] = []

    for i, rnd in enumerate(spec["rounds"], 1):
        label = rnd.get("label", f"round-{i}")
        payload = rnd.get("payload", {})
        headers_r = dict(headers)
        headers_r.update(rnd.get("headers", {}))

        time.sleep(delay)
        entry: dict = {"round": i, "label": label, "payload": payload}
        try:
            r = httpx.post(spec["url"], json=payload, headers=headers_r,
                           timeout=spec.get("timeout_seconds", 25), follow_redirects=True)
        except httpx.HTTPError as e:
            entry.update({"status": None, "error": f"{type(e).__name__}: {e}", "count": -1})
            report.append(entry)
            print(f"[{i:2d}] {label[:52]:52s} 请求失败")
            continue

        entry["status"] = r.status_code
        if r.status_code != 200:
            entry["count"] = -1
            report.append(entry)
            print(f"[{i:2d}] {label[:52]:52s} HTTP {r.status_code}")
            continue

        try:
            obj = r.json()
        except json.JSONDecodeError:
            entry["count"] = -1
            entry["error"] = "响应不是 JSON"
            report.append(entry)
            print(f"[{i:2d}] {label[:52]:52s} 非 JSON")
            continue

        cur = obj
        for part in list_path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        recs = cur if isinstance(cur, list) else []

        names = {str(x.get("name")) for x in recs if isinstance(x, dict) and x.get("name")}
        entry["count"] = len(recs)
        entry["unique_names"] = len(names)
        entry["update_time"] = obj.get("updateTime") if isinstance(obj, dict) else None
        entry["primary_dist"] = dict(Counter(str(x.get("primary")) for x in recs if isinstance(x, dict)).most_common(12))
        entry["grade_dist"] = dict(sorted(Counter(str(x.get("grade")) for x in recs if isinstance(x, dict)).items()))
        entry["overlap_with_previous"] = {p[0]: len(names & p[1]) for p in per_round}
        entry["new_names"] = len(names - seen)

        seen |= names
        per_round.append((label, names))

        (raw_dir / f"{i:02d}-{label.replace('/', '_')[:50]}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        report.append(entry)

        print(f"[{i:2d}] {label[:52]:52s} 条数={len(recs):4d} 新增={entry['new_names']:4d} "
              f"类型={list(entry['primary_dist'].keys())[:3]}")

    print("\n" + "=" * 78)
    print(f"累计唯一 name = {len(seen)}")
    print("=" * 78)

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"spec_name": spec.get("name"), "url": spec["url"], "total_unique_names": len(seen),
         "rounds": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化报告已写入：{out}")
    print(f"原始响应已写入：{raw_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
