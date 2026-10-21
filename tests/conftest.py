# -*- coding: utf-8 -*-
"""pytest 公共夹具。

关键约束：**导入 backend 之前**必须先设好环境变量。
因为 `backend.config.get_settings()` 带 lru_cache，且模块导入时会读 .env，
所以数据库路径必须在第一个 backend 模块被导入前就确定下来。

另外所有测试都走**离线路径**：适配器的网络方法在测试中被替换为固定 fixture，
因此跑测试不需要联网，也不会给公开接口添麻烦。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── 1. 在任何 backend 导入之前确定测试用数据库 ──────────────────────────
_TMP_DIR = Path(tempfile.mkdtemp(prefix="df-ammo-test-"))
os.environ["DB_PATH"] = str(_TMP_DIR / "test.sqlite3")
os.environ["BACKFILL_ON_STARTUP"] = "false"
os.environ["POLL_INTERVAL_SECONDS"] = "3600"      # 测试里不让调度器乱跑
os.environ["ALERT_ENABLED"] = "true"

# 项目根加入 sys.path，便于 `import backend`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db as db_mod  # noqa: E402
from backend.config import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture()
def session():
    """每个测试一个干净的数据库：**前后都清库**。

    只在前面清是不够的 —— 一个测试写下的数据会泄漏给后面的测试，
    导致失败出现在"无辜"的测试里，排查成本极高（本项目就踩过这个坑）。
    """
    from backend.models import Base

    def wipe(s) -> None:
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
        s.commit()

    db_mod.reset_engine()
    db_mod.init_db()
    with db_mod.session_scope() as s:
        wipe(s)                      # 前清：避免上一个测试的残留
        yield s
        wipe(s)                      # 后清：不把残留留给下一个测试


@pytest.fixture()
def sample_search_payload() -> dict:
    """一份结构真实（但数值虚构）的 /api/Items/search 响应。

    刻意包含三类容易出问题的记录：
    1. 正常有价的弹药；
    2. **无价弹药**（value.new = null）—— 必须被跳过而不是当成 0；
    3. **非目标类型**（primary = 装备）—— 必须被过滤掉，因为服务端的 type
       过滤并不可靠（实测 type=["护甲"] 会返回 primary="装备"）。
    """
    return {
        "code": 0,
        "updateTime": "2026-10-14 18:30:01",
        "msg": "获取成功",
        "data": [
            {
                "id": "37220000001",
                "name": ".357 Magnum 9号霰射鼠弹",
                "grade": "3",
                "primary": "弹药",
                "second": ".357 Magnum",
                "pic": "https://example.invalid/a.png",
                "length": "1",
                "width": "1",
                "value": {"new": 237, "7zf": 32.91, "7df": -32.4, "7top": 315, "7down": 179},
            },
            {
                "id": "37220200001",
                "name": ".357 Magnum HP",
                "grade": "2",
                "primary": "弹药",
                "second": ".357 Magnum",
                "length": "1",
                "width": "1",
                # 7zf 是 int，不是 float —— 实测类型会摇摆，必须能处理
                "value": {"new": 150, "7zf": 6, "7df": -6.38, "7top": 159, "7down": 141},
            },
            {
                # 无价格：不可交易物品，必须跳过
                "id": "37240700001",
                "name": ".338 Lap Mag AP",
                "grade": "6",
                "primary": "弹药",
                "second": ".338 Lap Mag",
                "length": "1",
                "width": "1",
                "value": {"new": None, "7zf": None, "7df": None, "7top": None, "7down": None},
            },
            {
                # 非弹药：服务端过滤不可靠时可能混进来，必须按 primary 再过滤一次
                "id": "99000000001",
                "name": "某件护甲",
                "grade": "4",
                "primary": "装备",
                "second": "护甲",
                "length": "2",
                "width": "2",
                "value": {"new": 1234, "7zf": 1.0, "7df": 12.0, "7top": 1300, "7down": 1200},
            },
        ],
    }


@pytest.fixture()
def sample_chart_payload() -> dict:
    """一份结构真实的 /api/Items/chartData 响应。

    注意 `avg_price` 是**字符串**（实测如此），解析时必须显式转换。
    """
    return {
        "name": ".357 Magnum 9号霰射鼠弹",
        "values": [
            {"time": "2026-10-07 19:00:00", "avg_price": "284"},
            {"time": "2026-10-07 20:00:00", "avg_price": "284"},
            {"time": "2026-10-07 21:00:00", "avg_price": "281"},
            # 脏数据：空价格，必须被跳过
            {"time": "2026-10-07 22:00:00", "avg_price": ""},
            # 脏数据：无法解析的时间，必须被跳过
            {"time": "not-a-time", "avg_price": "300"},
            {"time": "2026-10-07 23:00:00", "avg_price": "284"},
        ],
    }
