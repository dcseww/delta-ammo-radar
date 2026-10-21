# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

启动时会做三件事（幂等，重复启动安全）
--------------------------------------
1. 建表（`init_db`）；
2. 如果 `ammo` 表为空，自动导入 `backend/catalog/ammo_catalog.yaml`
   —— 让"clone 下来直接跑"成为可能，不需要记得先跑 seed 脚本；
3. 启动调度器：每 N 分钟采集一次 + 首次启动回补 7 天历史。

安全默认值
----------
只绑 `127.0.0.1`（配置里的 default），不暴露到局域网。
本项目不做多用户、不做鉴权 —— 因为它本来就不该被外部访问到。

启动
----
    python -m backend.app
或
    uvicorn backend.app:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from backend.api import routes_alerts, routes_ammo, routes_health
from backend.config import get_settings
from backend.db import init_db, session_scope
from backend.models import Ammo
from backend.pipeline.scheduler import get_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("df-ammo-radar")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def ensure_catalog_seeded() -> bool:
    """ammo 表为空时自动导入基础表。返回是否执行了导入。"""
    with session_scope() as session:
        n = session.scalar(select(func.count()).select_from(Ammo)) or 0
        if n > 0:
            log.info("基础表已有 %d 条，跳过导入", n)
            return False

    from backend.catalog.seed import seed

    try:
        with session_scope() as session:
            stats = seed(session)
        log.info("基础表自动导入完成：%s", stats)
        return True
    except FileNotFoundError as e:
        log.error("自动导入基础表失败：%s", e)
        return False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时准备数据与调度，关闭时优雅停止。"""
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    ensure_catalog_seeded()

    scheduler = get_scheduler()
    if not getattr(_app.state, "testing", False):
        scheduler.start()
    else:
        log.info("测试模式：不启动调度器")

    yield

    scheduler.shutdown()


app = FastAPI(
    title="三角洲行动 · 子弹物价实时监控",
    description=(
        "单机自用的弹药价格监控与告警服务。\n\n"
        "**数据来源**：公开只读接口（无需登录、无凭证）。\n\n"
        "**明确不做**：不自动下单、不抢购、不注入游戏客户端、不对外提供数据服务。"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(routes_ammo.router)
app.include_router(routes_alerts.router)
app.include_router(routes_health.router)


@app.get("/api", include_in_schema=False)
def api_root() -> JSONResponse:
    return JSONResponse({
        "service": "df-ammo-radar",
        "version": "0.1.0",
        "docs": "/docs",
        "endpoints": [
            "/api/health", "/api/ammo", "/api/ammo/{ammo_id}", "/api/price/{ammo_id}",
            "/api/rank", "/api/alerts/rules", "/api/alerts/log", "/api/alerts/pending",
            "/api/watchlist", "/api/ingest/trigger", "/api/health/pack-check",
        ],
    })


# 静态前端挂载在最后：API 路由优先匹配，不会被静态目录抢占。
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:  # pragma: no cover
    @app.get("/", include_in_schema=False)
    def no_frontend() -> JSONResponse:
        return JSONResponse({"error": "frontend 目录不存在"}, status_code=500)


def main() -> None:
    """命令行启动入口。"""
    import uvicorn

    settings = get_settings()
    log.info("启动服务：http://%s:%d", settings.host, settings.port)
    log.info("数据源：%s", settings.source_search_url)
    log.info("数据库：%s", settings.db_path)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
