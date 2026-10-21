# -*- coding: utf-8 -*-
"""数据库引擎、会话与建表。

设计要点
--------
1. **SQLite + WAL**：自用场景单文件足够；WAL 允许采集写入与看板读取并发，
   避免"采集时页面卡住"。
2. **单写者**：SQLite 只允许一个写事务。所有写入都经过同一进程的会话，
   不引入多进程写入，从而不需要重试或分布式锁。
3. **幂等写入**：价格明细有唯一索引 (ammo_id, price_kind, source, ts)，
   重复采集同一时刻的数据用 INSERT OR REPLACE 覆盖，不会产生重复点。
4. **建表即时可用**：用 SQLAlchemy 的 create_all，不引入 Alembic 迁移工具。
   自用单机项目追加迁移工具的复杂度大于收益；表结构变更直接删库重建
   （见 scripts/reset_db.py）。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_settings
from backend.models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    """连接级 PRAGMA。WAL 必须在每个连接上开启（不是数据库级持久设置）。"""
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")      # 读写并发
        cur.execute("PRAGMA synchronous=NORMAL")    # WAL 下的安全/性能平衡点
        cur.execute("PRAGMA foreign_keys=ON")       # 启用外键约束
        cur.execute("PRAGMA busy_timeout=5000")     # 写锁冲突时等 5 秒而不是立刻报错
    finally:
        cur.close()


def get_engine() -> Engine:
    """获取（或惰性创建）全局引擎。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.ensure_dirs()
        _engine = create_engine(
            settings.sqlalchemy_url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False},  # 允许调度线程与请求线程共用引擎
        )
        event.listen(_engine, "connect", _configure_sqlite)
        log.debug("SQLite 引擎已创建：%s", settings.db_path)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文：正常提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """建表（幂等）。已存在的表不会被改动。"""
    Base.metadata.create_all(bind=get_engine())
    log.info("数据库表结构已就绪")


def reset_engine() -> None:
    """清空缓存的引擎与会话工厂。主要供测试在切换数据库路径后调用。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
