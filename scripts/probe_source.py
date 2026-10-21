# -*- coding: utf-8 -*-
"""数据源探测工具（只读，不做任何解析假设）。

用途
----
在写任何适配器/解析器之前，先用它真实地打一次目标地址，回答三个问题：

1. 需不需要登录 / 有没有反爬（状态码、跳转、验证码页面）；
2. 返回的是 HTML 还是 JSON（决定用 lxml 还是 json 解析）；
3. 真实字段长什么样（把原始响应落盘成 fixture，供离线开发与单测使用）。

设计原则
--------
- **不猜字段**。本脚本只做"请求 → 报告 → 落盘"，字段识别是人的工作。
- **礼貌访问**。默认串行、带 UA 与延时、默认不重试，避免给公开站点添麻烦。
- **可复现**。原始响应按时间戳落盘，之后所有解析测试都基于这份 fixture，
  即使站点改版也能回放历史数据继续开发（对应开发文档的风险 R1）。

用法
----
    python scripts/probe_source.py --url "https://example.com/prices"
    python scripts/probe_source.py --url "..." --out data/fixtures --save
    python scripts/probe_source.py --url "..." --post --data '{"page":1}'
    python scripts/probe_source.py --url "..." --json-api --data '{"isZb":0}'

输出
----
- 终端：状态码、Content-Type、字节数、耗时、重定向链，以及响应片段预览
- 文件：<out>/<host>-<ts>.raw（原始字节）与同名 .meta.json（请求与响应元数据）

注意：本工具不发送任何账号凭证。若某源必须登录才能访问，请**放弃该源**，
不要在此脚本中硬编码 token —— 开发文档第 15 章对此有明确约束。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 httpx。请先安装：pip install httpx")

# Windows 控制台默认 GBK，中文与符号会直接抛 UnicodeEncodeError 中断探测。
# 统一把三个标准流切到 UTF-8，并对无法编码的字符降级替换而不是崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

# 第三方公开页面的默认 UA：如实表明身份，便于站点管理员识别与联系。
DEFAULT_UA = "df-ammo-radar-probe/0.1 (personal read-only research; contact: local user)"

# 响应中可能出现的"需要登录/被拦截"信号，用于快速判读而不是靠人眼找。
LOGIN_HINTS = [
    "请登录", "登录后查看", "立即登录", "扫码登录", "未登录",
    "sign in", "log in", "login required", "unauthorized",
]
BLOCK_HINTS = [
    "验证码", "人机验证", "访问过于频繁", "请求过于频繁", "滑动验证",
    "captcha", "cloudflare", "access denied", "403 forbidden", "安全验证",
]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_host(url: str) -> str:
    host = urlparse(url).netloc or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def preview(body: bytes, content_type: str, limit: int = 1200) -> str:
    """把响应体渲染成可读预览：JSON 美化，其余按文本截断。"""
    text = body.decode("utf-8", errors="replace")
    if "json" in content_type.lower() or text.lstrip()[:1] in "[{":
        try:
            obj = json.loads(text)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
            return pretty[:limit] + ("\n... (预览已截断)" if len(pretty) > limit else "")
        except json.JSONDecodeError:
            pass
    return text[:limit] + ("\n... (预览已截断)" if len(text) > limit else "")


def judge(status: int, body: bytes, content_type: str) -> list[str]:
    """给出人类可读的判读结论，而不是让使用者自己盯着一堆字节。

    刻意只用 ASCII 标记（[OK]/[WARN]/[FAIL]）：Windows 控制台默认 GBK，
    带 Emoji 的输出会在打印时抛 UnicodeEncodeError 把探测打断。
    """
    verdicts: list[str] = []
    low = body.decode("utf-8", errors="replace").lower()

    if status == 200:
        verdicts.append("[OK]   HTTP 200，可访问")
    elif status in (301, 302, 303, 307, 308):
        verdicts.append(f"[WARN] HTTP {status} 重定向，注意最终落点是否需要登录")
    elif status in (401, 403):
        verdicts.append(f"[FAIL] HTTP {status}，被拒绝 —— 该源大概率需要登录或存在反爬，建议放弃")
    elif status == 404:
        verdicts.append("[FAIL] HTTP 404，地址不存在或已改版")
    elif status >= 500:
        verdicts.append(f"[FAIL] HTTP {status}，服务端错误，可能临时故障也可能是反爬")
    else:
        verdicts.append(f"[WARN] HTTP {status}，需人工判断")

    if "json" in content_type.lower():
        verdicts.append("[OK]   返回 JSON —— 可直接用 json 解析，最理想")
    elif "html" in content_type.lower():
        verdicts.append("[WARN] 返回 HTML —— 若数据在 JS 里渲染，HTML 里可能根本没有价格，需进一步确认")
    else:
        verdicts.append(f"[WARN] Content-Type = {content_type or '未知'}")

    for h in LOGIN_HINTS:
        if h in low:
            verdicts.append(f"[FAIL] 响应中出现登录提示「{h}」—— 该源不可用（本项目不使用账号凭证）")
            break
    for h in BLOCK_HINTS:
        if h in low:
            verdicts.append(f"[FAIL] 响应中出现拦截/验证码提示「{h}」—— 该源不可用")
            break

    if len(body) < 800 and ("html" in content_type.lower()):
        verdicts.append("[WARN] HTML 体积异常小（<800 字节），可能是跳转页或拦截页")

    return verdicts


def key_hits(body: bytes) -> list[str]:
    """扫描响应体，报告是否出现"价格类"字段名，用于快速判断有没有我们要的数据。"""
    text = body.decode("utf-8", errors="replace")
    patterns = {
        "price": r'"?price"?\s*[:=]',
        "价格": r"价格",
        "子弹": r"子弹",
        "弹药": r"弹药",
        "auction/trade": r'"?(\w*auction\w*|\w*trade\w*)"?\s*[:=]',
        "item/ammo": r'"?(\w*item\w*|\w*ammo\w*)"?\s*[:=]',
        "涨跌": r"(涨跌|change|pct)",
    }
    hits = [name for name, pat in patterns.items() if re.search(pat, text, re.IGNORECASE)]
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="只读探测一个候选数据源：报告可访问性与响应形态，并落盘原始响应。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", required=True, help="目标地址")
    ap.add_argument("--out", default="data/fixtures", help="落盘目录（默认 data/fixtures）")
    ap.add_argument("--save", action="store_true", help="把原始响应与元数据写入 --out")
    ap.add_argument("--post", action="store_true", help="改用 POST（默认 GET）")
    ap.add_argument("--json-api", action="store_true", help="POST 时以 JSON 发送 body（默认表单）")
    ap.add_argument("--data", default=None, help="请求体，JSON 字符串或 k=v&k2=v2")
    ap.add_argument("--header", action="append", default=[], help="附加请求头，形如 'K: V'，可重复")
    ap.add_argument("--timeout", type=float, default=20.0, help="超时秒数（默认 20）")
    ap.add_argument("--delay", type=float, default=1.0, help="请求前延时秒数，礼貌限速（默认 1.0）")
    ap.add_argument("--preview", type=int, default=1200, help="预览字符数（默认 1200）")
    args = ap.parse_args(argv)

    headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    for raw in args.header:
        if ":" not in raw:
            print(f"忽略无法解析的请求头：{raw}", file=sys.stderr)
            continue
        k, v = raw.split(":", 1)
        headers[k.strip()] = v.strip()

    body = None
    if args.data is not None:
        if args.json_api:
            try:
                json.loads(args.data)  # 提前校验，避免把坏 JSON 发出去
            except json.JSONDecodeError as e:
                print(f"--json-api 的 --data 不是合法 JSON：{e}", file=sys.stderr)
                return 2
            body = args.data.encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            body = args.data.encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    print("=" * 78)
    print(f"探测目标 : {args.url}")
    print(f"方法     : {'POST' if args.post else 'GET'}")
    print(f"时间     : {now_stamp()}")
    print(f"UA       : {headers['User-Agent']}")
    print("=" * 78)

    if args.delay:
        time.sleep(args.delay)

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=True, headers=headers) as client:
            resp = client.request("POST" if args.post else "GET", args.url, content=body)
    except httpx.HTTPError as e:
        print(f"\n[FAIL] 请求失败：{type(e).__name__}: {e}")
        print("\n判读：网络层就没通。可能是域名不存在、需要代理、或被本机网络策略拦截。")
        return 1

    elapsed_ms = (time.perf_counter() - started) * 1000
    raw = resp.content
    ctype = resp.headers.get("content-type", "")

    print(f"\n状态码      : {resp.status_code}")
    print(f"Content-Type: {ctype or '(无)'}")
    print(f"响应大小    : {len(raw)} 字节")
    print(f"耗时        : {elapsed_ms:.0f} ms")
    if resp.history:
        chain = " -> ".join(f"{r.status_code} {r.url}" for r in resp.history)
        print(f"重定向链    : {chain} -> {resp.status_code} {resp.url}")
    print(f"最终地址    : {resp.url}")
    print(f"内容摘要    : sha256={hashlib.sha256(raw).hexdigest()[:16]}")

    print("\n--- 判读 ---")
    for v in judge(resp.status_code, raw, ctype):
        print(f"  {v}")

    hits = key_hits(raw)
    print("\n--- 关键字段扫描 ---")
    if hits:
        print(f"  命中：{', '.join(hits)}")
        print("  → 响应里出现了价格/物品类字段名，值得深入看字段结构。")
    else:
        print("  未命中任何价格/物品类字段名。")
        print("  → 若返回 HTML，数据很可能是 JS 动态加载的，静态抓取拿不到。")

    print(f"\n--- 响应预览（前 {args.preview} 字符）---")
    print(preview(raw, ctype, args.preview))

    if args.save:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_host(args.url)}-{now_stamp()}"
        raw_path = out / f"{stem}.raw"
        meta_path = out / f"{stem}.meta.json"
        raw_path.write_bytes(raw)
        meta = {
            "probed_at": now_stamp(),
            "request": {
                "url": args.url,
                "method": "POST" if args.post else "GET",
                "headers": {k: v for k, v in headers.items() if k.lower() != "cookie"},
                "body_sha256": hashlib.sha256(body).hexdigest() if body else None,
            },
            "response": {
                "status": resp.status_code,
                "final_url": str(resp.url),
                "content_type": ctype,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "elapsed_ms": round(elapsed_ms),
            },
            "judgement": judge(resp.status_code, raw, ctype),
            "key_field_hits": hits,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已落盘：\n  {raw_path}\n  {meta_path}")
        print("  → 之后所有解析测试都基于这份 fixture 离线回放，站点改版也不影响开发。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
