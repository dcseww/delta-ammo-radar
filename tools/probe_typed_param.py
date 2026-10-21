# -*- coding: utf-8 -*-
"""验证某接口接受的参数类型（字符串 ID vs 数字 ID），并确认历史价格曲线可用。

要解决的问题（对应数据源实测报告的待验证项 V1/V2）
--------------------------------------------------
站点前端代码里 `itemId` 标注为 `Number`，但接口返回的物品 `id` 是**字符串**
（如 `"37220000001"`，12 位，超过 32 位整数范围）。这两种表示不能混用：

- 传数字：可能精度丢失或被服务端拒绝；
- 传字符串：可能触发类型校验失败。

本脚本对**同一个 id** 分别以字符串、数字两种形式请求，对比响应，
从而确定正确用法。这是"接口契约靠实测确定、不靠猜"的体现。

用法
----
    python tools/probe_typed_param.py --url https://example.com/api/x \
        --param itemId --value 37220000001 --extra '{"chartType":7}'
"""

from __future__ import annotations

import argparse
import json
import sys
import time

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


def describe(obj, limit: int = 600) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)[:limit]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="探测接口参数类型：字符串 vs 数字。")
    ap.add_argument("--url", required=True)
    ap.add_argument("--param", required=True, help="要探测的参数名，如 itemId")
    ap.add_argument("--value", required=True, help="参数值；脚本会自动尝试 str 与 int 两种类型")
    ap.add_argument("--extra", default="{}", help="其它固定参数的 JSON，如 '{\"chartType\":7}'")
    ap.add_argument("--delay", type=float, default=1.4)
    ap.add_argument("--timeout", type=float, default=25.0)
    args = ap.parse_args(argv)

    base = json.loads(args.extra)
    headers = {"User-Agent": DEFAULT_UA, "Content-Type": "application/json"}

    # 值可能超出 int64，提前告警
    note = ""
    try:
        as_int = int(args.value)
        if as_int > 2**53:
            note = "（注意：该值超过 IEEE754 安全整数范围 2^53，转数字可能丢精度）"
    except ValueError:
        as_int = None
        note = "（该值无法转为整数，只能传字符串）"

    trials: list[tuple[str, object]] = [("字符串", str(args.value))]
    if as_int is not None:
        trials.append(("数字", as_int))

    print("=" * 78)
    print(f"探测参数类型：{args.url}")
    print(f"参数：{args.param} = {args.value} {note}")
    print(f"其它参数：{json.dumps(base, ensure_ascii=False)}")
    print("=" * 78)

    results = []
    for label, val in trials:
        payload = dict(base)
        payload[args.param] = val
        time.sleep(args.delay)
        body = json.dumps(payload, ensure_ascii=False)
        print(f"\n--- 尝试 {label}：{body} ---")
        try:
            r = httpx.post(args.url, content=body.encode("utf-8"), headers=headers,
                           timeout=args.timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            print(f"  [FAIL] {type(e).__name__}: {e}")
            results.append((label, None, None))
            continue

        print(f"  HTTP {r.status_code}  {len(r.content)} 字节")
        if r.status_code != 200:
            print(f"  响应片段：{r.text[:300]}")
            results.append((label, r.status_code, None))
            continue

        try:
            obj = r.json()
        except json.JSONDecodeError:
            print(f"  [FAIL] 非 JSON：{r.text[:200]}")
            results.append((label, 200, None))
            continue

        # 结构化判读：有 code 字段就看 code，有数据就统计规模
        code = obj.get("code") if isinstance(obj, dict) else None
        data = obj.get("data") if isinstance(obj, dict) else None
        n = len(data) if isinstance(data, list) else ("对象" if isinstance(data, dict) else "无")
        print(f"  code={code}  msg={obj.get('msg') if isinstance(obj, dict) else '-'}  data={n}")
        print(f"  预览：{describe(obj, 500)}")
        results.append((label, 200, code))

    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    ok = [(lab, c) for lab, st, c in results if st == 200 and (c in (0, None))]
    if not ok:
        print("  两种类型都失败。请检查 id 是否有效、接口路径是否变化。")
    elif len(ok) == len(results):
        print("  两种类型都成功 —— 接口对该参数类型不敏感，用字符串更安全（避免精度问题）。")
    else:
        print(f"  只有 {ok[0][0]} 形式成功 —— 接入时必须使用该类型。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
