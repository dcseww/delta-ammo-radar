# -*- coding: utf-8 -*-
"""从 SPA 的入口 bundle 里找出它的懒加载分包（chunk）与 axios 基地址。

背景
----
用 tools/inspect_bundle.py 分析入口 bundle 时，常常只能看到登录/注册这类
通用接口 —— 因为业务页面（子弹排行、交易行价格）是**懒加载分包**，
入口 bundle 里只留一个 `import("./xxx-hash.js")` 引用。

本脚本就是为这一步准备的：把入口 bundle 里所有分包文件名和它们的来源
（动态 import、__vitePreload 数组、assets 目录引用）挖出来并落盘成一个清单，
然后逐个下载、逐个搜关键词，最终定位真正返回价格数据的那个接口。

用法
----
    python tools/find_chunks.py --url "https://example.com/assets/index-xxxx.js"
    python tools/find_chunks.py --url "..." --keyword 子弹 --keyword price
    python tools/find_chunks.py --url "..." --download      # 顺带把分包全下下来
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

# 分包文件名（Vite / Rollup 常见命名：index-XXXXXXXX.js、page-Ab12Cd.js 等）
CHUNK_PATTERNS = [
    r"""["'`](\.?/?assets/[A-Za-z0-9._\-]+\.js)["'`]""",
    r"""["'`]([A-Za-z0-9._\-]*-[A-Za-z0-9_]{6,10}\.js)["'`]""",
    r"""import\(\s*["'`]([^"'`]+\.js)["'`]\s*\)""",
    r"""__vitePreload\(\s*\(\)\s*=>\s*import\(\s*["'`]([^"'`]+\.js)["'`]""",
]

# axios 实例的 baseURL / 前缀
BASE_PATTERNS = [
    r"""(?:baseURL|baseUrl|API_BASE|apiBase|VITE_[A-Z_]*URL)\s*[:=]\s*["'`]([^"'`\s]{2,160})["'`]""",
    r"""create\(\s*\{[^}]{0,400}?baseURL\s*:\s*["'`]([^"'`\s]{2,160})["'`]""",
]

# 与子弹价格相关的关键词，用于判断哪个分包最值得看
DEFAULT_KEYWORDS = ["ammo", "bullet", "子弹", "弹药", "price", "价格", "item", "物品", "交易", "排行"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="找出 SPA 的懒加载分包并可选下载。")
    ap.add_argument("--url", required=True, help="入口 bundle 地址")
    ap.add_argument("--out", default="data/fixtures/chunks", help="分包落盘目录")
    ap.add_argument("--download", action="store_true", help="下载所有分包并统计关键词命中")
    ap.add_argument("--keyword", action="append", default=[], help="自定义关键词，可重复")
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args(argv)

    keywords = args.keyword or DEFAULT_KEYWORDS
    headers = {"User-Agent": DEFAULT_UA}
    origin = f"{urlparse(args.url).scheme}://{urlparse(args.url).netloc}"

    print(f"下载入口 bundle: {args.url}")
    try:
        r = httpx.get(args.url, headers=headers, timeout=args.timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        print(f"[FAIL] {type(e).__name__}: {e}")
        return 1
    if r.status_code != 200:
        print(f"[FAIL] 状态码 {r.status_code}")
        return 1
    text = r.text
    print(f"OK：{len(text)} 字符\n")

    # ── 1. 分包 ─────────────────────────────────────────────────────────
    chunks: list[str] = []
    for pat in CHUNK_PATTERNS:
        for v in re.findall(pat, text):
            v = v.strip()
            if v.endswith(".js") and v not in chunks:
                chunks.append(v)
    print("=" * 78)
    print(f"发现 {len(chunks)} 个候选分包")
    print("=" * 78)
    for c in chunks:
        print("   ", c)

    # ── 2. axios 基地址 ─────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("axios / fetch 基地址线索")
    print("=" * 78)
    bases: list[str] = []
    for pat in BASE_PATTERNS:
        for v in re.findall(pat, text):
            if v not in bases:
                bases.append(v)
    if bases:
        for b in bases:
            print("   ", b)
        print("\n提示：baseURL + 分包里出现的路径片段 = 真正的接口地址。")
    else:
        print("    没有直接找到 baseURL，可能是变量拼接或同源相对路径。")
        # 退而求其次：把入口 bundle 里所有以 / 开头的短路径列出来
        paths = sorted({p for p in re.findall(r"""["'`](/[a-zA-Z0-9_\-/]{3,60})["'`]""", text)})
        if paths:
            print("\n    入口 bundle 中以 / 开头的路径（可能是接口前缀）：")
            for p in paths[:40]:
                print("   ", p)

    if not args.download or not chunks:
        if chunks:
            print(f"\n下一步：加 --download 把 {len(chunks)} 个分包下载下来并搜关键词。")
        return 0

    # ── 3. 下载分包并搜关键词 ───────────────────────────────────────────
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print("\n" + "=" * 78)
    print("下载分包并扫描关键词")
    print("=" * 78)

    ranked: list[tuple[int, str, list[str], list[str]]] = []
    for c in chunks:
        full = urljoin(origin + "/", c.lstrip("./"))
        try:
            cr = httpx.get(full, headers=headers, timeout=args.timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            print(f"  [SKIP] {c}: {type(e).__name__}")
            continue
        if cr.status_code != 200:
            print(f"  [SKIP] {c}: HTTP {cr.status_code}")
            continue

        body = cr.text
        name = Path(urlparse(full).path).name or "chunk.js"
        (out_dir / name).write_text(body, encoding="utf-8")

        hit_kw = [k for k in keywords if k in body]
        # 顺便把分包里的接口路径抽出来
        api_paths = sorted(set(re.findall(r"""["'`](/(?:api|v\d)?/?[a-zA-Z0-9_\-/]{3,80})["'`]""", body)))
        api_paths = [p for p in api_paths if not p.endswith((".js", ".css", ".png", ".svg", ".ico", ".jpg", ".webp"))]

        if hit_kw or api_paths:
            ranked.append((len(hit_kw), name, hit_kw, api_paths[:25]))
            print(f"  {name:36s} {len(body):>7d}B  关键词命中={hit_kw}")

    ranked.sort(reverse=True)
    print("\n" + "=" * 78)
    print("按相关性排序（关键词命中越多越可能是我们要的分包）")
    print("=" * 78)
    for score, name, kws, paths in ranked[:12]:
        print(f"\n[{score}] {name}   命中={kws}")
        for p in paths:
            print(f"       {p}")

    print(f"\n所有分包已落盘到 {out_dir}")
    print("下一步：对上面每个分包里的接口路径跑 scripts/probe_source.py 逐个验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
