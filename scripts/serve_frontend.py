#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端静态服务器（开发用）—— 托管 frontend/ 并把 /api 反向代理到后端。

为什么需要它
------------
本项目的「前端」是纯静态页面（HTML/CSS/JS，无 npm、无构建、无 CDN 依赖）。
正式运行时由 FastAPI 直接托管（backend/app.py 把 StaticFiles 挂在 "/"），
一个进程就够了。

但要改前端时，你不会想每改一行 CSS 就重启一次后端。于是可以单独起这个
静态服务器：它把 /api、/docs、/openapi.json 反向代理给后端，页面与接口
仍然同源 —— app.js 里写死的相对路径 `/api/...` 一行都不用改，也不存在
CORS 问题，改完刷新浏览器就能看到效果。

用法
----
    python scripts/serve_frontend.py                       # 8788 → 代理 127.0.0.1:8787
    python scripts/serve_frontend.py --port 9000
    python scripts/serve_frontend.py --backend http://127.0.0.1:8790
    python scripts/serve_frontend.py --no-proxy            # 纯静态，不代理

只监听 127.0.0.1：单机自用，不对外暴露。仅依赖标准库。
"""

from __future__ import annotations

import argparse
import mimetypes
import posixpath
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIR = PROJECT_ROOT / "frontend"

# 这些路径交给后端，其余一律当作静态文件
PROXY_EXACT = {"/api", "/docs", "/redoc", "/openapi.json"}
PROXY_PREFIX = ("/api/", "/docs/", "/redoc/")

# 后端可能被反代打挂时给出的兜底文案
BACKEND_HINT = "后端没起来？先运行 .\\start.ps1（或 .\\start.ps1 -Dev）"


class Handler(BaseHTTPRequestHandler):
    """静态文件 + /api 反向代理。

    可配置项由 main() 注入到类属性上（每个服务器实例一份配置）。
    """

    server_version = "df-ammo-frontend/0.1"
    protocol_version = "HTTP/1.1"

    frontend_dir: Path = DEFAULT_FRONTEND_DIR
    backend: str | None = None
    proxy_enabled: bool = True
    timeout: float = 60.0

    # ── 内部工具 ────────────────────────────────────────────────────────
    def send_response(self, code, message=None):  # noqa: D102 - 记录状态码用于日志
        self._status = code
        super().send_response(code, message)

    def log_message(self, *args):  # noqa: D102 - 关掉默认逐行日志，用我们自己的格式
        pass

    def _note(self, method: str, status: int) -> None:
        ms = (time.perf_counter() - self._started) * 1000
        kind = "PROXY " if self._proxied else "STATIC"
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {kind} {method:4} {self.path} -> {status} ({ms:.0f} ms)", flush=True)

    def _send_text(self, status: int, title: str, detail: str) -> None:
        payload = f"{title}\n\n{detail}\n".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _is_proxy_path(self) -> bool:
        if not (self.proxy_enabled and self.backend):
            return False
        path = urllib.parse.urlsplit(self.path).path
        return path in PROXY_EXACT or path.startswith(PROXY_PREFIX)

    def _static_target(self) -> Path | None:
        """把 URL 映射到 frontend/ 下的真实文件；越界或不存在返回 None。"""
        raw = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        rel = posixpath.normpath(raw).lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        root = self.frontend_dir.resolve()
        try:
            target = (root / rel).resolve()
            target.relative_to(root)  # 目录穿越保护
        except (ValueError, OSError):
            return None
        if target.is_dir():
            target = target / "index.html"
        return target if target.is_file() else None

    # ── 静态 ────────────────────────────────────────────────────────────
    def _serve_static(self, head_only: bool) -> None:
        target = self._static_target()
        if target is None:
            self._send_text(
                404,
                "404 Not Found",
                f"前端目录下没有这个文件：{self.path}\n"
                f"前端根目录：{self.frontend_dir}\n"
                f"（分离模式下 /api、/docs、/openapi.json 会被转发到后端：{self.backend}）",
            )
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # 开发模式：改完刷新即生效
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    # ── 反代 ────────────────────────────────────────────────────────────
    def _proxy(self, method: str, head_only: bool) -> None:
        url = self.backend.rstrip("/") + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=method)
        for name in ("Content-Type", "Accept", "Accept-Language", "Authorization"):
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)
        client = self.client_address[0] if self.client_address else "127.0.0.1"
        req.add_header("X-Forwarded-For", client)
        req.add_header("X-Forwarded-Proto", "http")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, headers, payload = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:            # 后端的 4xx/5xx 原样透传
            status, headers, payload = exc.code, exc.headers, exc.read()
        except Exception as exc:                          # 连不上/超时 → 502
            self._send_text(
                502,
                "502 Bad Gateway（后端不可达）",
                f"{method} {self.path}\n{type(exc).__name__}: {exc}\n"
                f"后端地址：{self.backend}\n{BACKEND_HINT}",
            )
            return

        self.send_response(status)
        ctype = headers.get("Content-Type")
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    # ── 入口 ────────────────────────────────────────────────────────────
    def _handle(self, method: str, head_only: bool = False) -> None:
        self._started = time.perf_counter()
        self._status = 500
        self._proxied = self._is_proxy_path()
        try:
            if self._proxied:
                self._proxy(method, head_only)
            elif method in ("GET", "HEAD"):
                self._serve_static(head_only)
            else:
                self._send_text(
                    405,
                    "405 Method Not Allowed",
                    f"静态前端不接受 {method} 请求：{self.path}\n",
                )
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器提前断开，不是错误
        finally:
            self._note(method, self._status)

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_HEAD(self):  # noqa: N802
        self._handle("HEAD", head_only=True)

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self):  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self):  # noqa: N802
        self._handle("OPTIONS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="前端静态服务器（含 /api 反向代理；仅标准库）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（默认 8788）")
    parser.add_argument("--backend", default="http://127.0.0.1:8787", help="后端地址（默认 127.0.0.1:8787）")
    parser.add_argument("--dir", default=str(DEFAULT_FRONTEND_DIR), help="前端目录（默认 <项目>/frontend）")
    parser.add_argument("--no-proxy", action="store_true", help="关闭反向代理，只做静态服务")
    parser.add_argument("--timeout", type=float, default=60.0, help="反代请求超时秒数（默认 60）")
    args = parser.parse_args(argv)

    root = Path(args.dir).resolve()
    if not (root / "index.html").is_file():
        print(f"[FAIL] 前端目录里找不到 index.html：{root}", file=sys.stderr, flush=True)
        return 2

    Handler.frontend_dir = root
    Handler.backend = None if args.no_proxy else args.backend
    Handler.proxy_enabled = not args.no_proxy
    Handler.timeout = args.timeout

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[FAIL] 无法监听 {args.host}:{args.port} —— {exc}", file=sys.stderr, flush=True)
        print("       端口被占用？换个端口，或先执行 .\\start.ps1 -Stop", file=sys.stderr, flush=True)
        return 1

    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"[OK] 前端静态服务器已启动：{url}", flush=True)
    print(f"     前端目录：{root}", flush=True)
    if args.no_proxy:
        print("     反向代理：已关闭（纯静态，页面里的 /api 请求会 404）", flush=True)
    else:
        print(f"     反向代理：/api、/docs、/openapi.json → {args.backend}", flush=True)
    print("     Ctrl+C 停止", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[--] 收到 Ctrl+C，前端服务器退出", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
