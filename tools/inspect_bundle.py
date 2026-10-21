# -*- coding: utf-8 -*-
"""从 SPA 的 JS bundle 里挖出它真正调用的数据接口。

为什么需要它
------------
很多公开的三角洲数据站是单页应用（SPA）：直接抓 HTML 只有一个空壳
（`<div id="app"></div>`），价格数据是浏览器执行 JS 之后再由 XHR/fetch 拉回来的。
所以"公开页面只读解析"这条路的真正入口不是 HTML，而是 **bundle 里的接口地址**。

本脚本做两件事：
1. 下载指定 JS bundle，原始内容落盘（便于反复分析、也便于站点改版后对比）；
2. 用一组正则把候选接口地址、API 前缀、fetch/axios 调用点全部列出来。

注意：本脚本只做**静态分析**，不执行页面 JS、不注入、不碰任何账号凭证。

用法
----
    python tools/inspect_bundle.py --url "https://example.com/assets/index-xxxx.js"
    python tools/inspect_bundle.py --url "..." --filter api
    python tools/inspect_bundle.py --url "..." --show-context 120
"""

from __future__ import annotations

import argparse
import re
import sys
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

# 候选接口地址的提取规则。刻意放宽，宁可多列出来让人筛，也不要漏掉真正的入口。
PATTERNS: list[tuple[str, str]] = [
    ("绝对URL", r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{6,140}"),
    ("/api/ 路径", r"""["'`](/api/[^"'`\s]{2,100})["'`]"""),
    ("fetch/axios 调用", r"""(?:fetch|axios\.\w+|\.(?:get|post|request))\s*\(\s*["'`]([^"'`\s]{4,120})"""),
    ("模板字符串URL", r"`((?:https?://|/)[^`\s]{4,140})`"),
    ("baseURL 配置", r"""(?:baseURL|baseUrl|API_BASE|apiBase|VITE_[A-Z_]*URL)\s*[:=]\s*["'`]([^"'`\s]{2,140})"""),
    ("疑似接口片段", r"""["'`](/(?:v\d/)?[a-z0-9_\-]*(?:api|item|price|ammo|list|data|rank|goods)[a-z0-9_\-/]{0,60})["'`]"""),
]

# 与"价格数据"相关的关键词，用于判断哪个接口最可能是我们要的。
HOT_WORDS = ["ammo", "子弹", "price", "价格", "item", "物品", "list", "排行", "rank", "trade", "交易"]


def classify(endpoint: str) -> str:
    low = endpoint.lower()
    score = sum(1 for w in HOT_WORDS if w in low)
    if score >= 2:
        return "HOT "
    if score == 1:
        return "warm"
    return "    "


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="静态分析 JS bundle，提取数据接口地址。")
    ap.add_argument("--url", required=True, help="JS bundle 地址")
    ap.add_argument("--out", default="data/fixtures", help="落盘目录（默认 data/fixtures）")
    ap.add_argument("--save", action="store_true", default=True, help="保存原始 bundle（默认开）")
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--filter", default=None, help="只显示包含该子串的结果")
    ap.add_argument("--show-context", type=int, default=0, help="显示每个命中的上下文字符数")
    ap.add_argument("--max-per-pattern", type=int, default=80, help="每种规则最多显示多少条")
    args = ap.parse_args(argv)

    headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    print(f"下载 bundle: {args.url}")
    try:
        r = httpx.get(args.url, headers=headers, timeout=args.timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        print(f"[FAIL] 请求失败：{type(e).__name__}: {e}")
        return 1

    print(f"状态码 {r.status_code} | {len(r.content)} 字节 | {r.headers.get('content-type')}")
    if r.status_code != 200:
        print("[FAIL] 未取到 bundle，无法继续分析。")
        return 1

    text = r.text
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9._-]", "_", urlparse(args.url).netloc or "unknown")
    bundle_path = out_dir / f"{host}-bundle.js"
    bundle_path.write_text(text, encoding="utf-8")
    print(f"已落盘原始 bundle: {bundle_path}（{len(text)} 字符）")

    print("\n" + "=" * 78)
    print("候选接口地址")
    print("=" * 78)
    all_hits: set[str] = set()
    for name, pat in PATTERNS:
        found: list[str] = []
        for m in re.finditer(pat, text):
            val = m.group(1) if m.groups() else m.group(0)
            val = val.strip()
            if len(val) < 2 or val in found:
                continue
            if args.filter and args.filter.lower() not in val.lower():
                continue
            found.append(val)
        if not found:
            continue
        all_hits.update(found)
        print(f"\n--- 规则「{name}」命中 {len(found)} 条 ---")
        for v in found[: args.max_per_pattern]:
            tag = classify(v)
            line = f"  [{tag}] {v}"
            if args.show_context:
                idx = text.find(v)
                if idx >= 0:
                    ctx = text[max(0, idx - args.show_context): idx + len(v) + args.show_context]
                    ctx = ctx.replace("\n", " ")
                    line += f"\n         ...{ctx}..."
            print(line)
        if len(found) > args.max_per_pattern:
            print(f"  ...（还有 {len(found) - args.max_per_pattern} 条，见落盘 bundle）")

    print("\n" + "=" * 78)
    hot = sorted(v for v in all_hits if classify(v).strip() == "HOT")
    print(f"最可能相关的接口（HOT，共 {len(hot)} 条）：")
    if hot:
        for v in hot:
            print("   *", v)
        print("\n下一步：对上面每条 HOT 地址单独跑 scripts/probe_source.py，")
        print("      看哪一条真的返回 JSON 价格数据，然后才写解析器。")
    else:
        print("   没有命中明显相关的接口。")
        print("   可能原因：接口地址是运行时拼接的、被压缩混淆，或数据走 WebSocket。")
        print("   建议改用手工方式：浏览器 F12 -> Network -> 筛选 XHR/Fetch -> 刷新页面。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
