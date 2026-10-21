# -*- coding: utf-8 -*-
"""配置读取与校验。

设计要点
--------
1. **配置与代码分离**：所有可调项集中在 .env，不写死在代码里。
2. **默认值必须安全**：即使没有 .env 也能启动（用内置默认值），
   这样第一次 clone 下来就能跑，降低上手成本。
3. **只绑本机**：默认 HOST=127.0.0.1，避免无意中把数据暴露到局域网。
4. **数据源参数集中**：接口地址、请求间隔、超时、退避都在这里，
   换数据源时只动配置不动逻辑（对应开发文档的风险 R1）。

用法
----
    from backend.config import get_settings
    settings = get_settings()
    print(settings.db_path)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：本文件在 backend/ 下，往上一级即项目根。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """全部运行期配置。字段名即 .env 中的键名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",          # 允许 .env 里有本项目暂时不用的键
        case_sensitive=False,
    )

    # ── 服务 ────────────────────────────────────────────────────────────
    host: str = "127.0.0.1"          # 只绑本机：自用场景不暴露到局域网
    port: int = 8787
    timezone: str = "Asia/Shanghai"

    # ── 数据库 ──────────────────────────────────────────────────────────
    db_path: Path = PROJECT_ROOT / "data" / "ammo.sqlite3"
    tick_retention_days: int = 30     # 明细保留天数（过期前先聚合）
    agg_1h_retention_days: int = 365

    # ── 数据源 ──────────────────────────────────────────────────────────
    # 实测可用的公开只读接口（见 docs/数据源实测报告-dergame.md）
    source_search_url: str = "https://dergame.cn/api/Items/search"
    source_chart_url: str = "https://dergame.cn/api/Items/chartData"
    source_type_value: str = "弹药"   # 只采弹药，其余类型一概忽略
    source_user_agent: str = "df-ammo-radar/0.1 (personal read-only research)"

    poll_interval_seconds: int = 600   # 全量快照 10 分钟一次（实测 updateTime 为分钟级）
    poll_jitter_seconds: int = 30      # 抖动，避免整点集中请求
    # 历史回补时两次请求之间的间隔。
    # 注意：这个值直接决定回补总耗时 —— 83 个可交易物品 × 间隔。
    # 初版误设为 90 秒，导致回补要跑 2 小时（设计缺陷，已修正）。
    # 1.5 秒足以对公开服务保持礼貌，整批约 2 分钟。
    backfill_interval_seconds: float = 1.5
    backfill_on_startup: bool = True     # 首次启动回补 7 天历史
    backfill_max_items: int = 0          # 0 = 不限制；>0 时每次只回补前 N 个（分片用）
    backfill_progress_every: int = 10    # 每处理多少个物品打一条进度日志
    http_timeout_seconds: float = 20.0
    max_retries: int = 3               # 失败重试次数（指数退避 1s/4s/16s）
    chart_type: int = 7                # 7=七日曲线，30=月度曲线

    # ── 数据质量 ────────────────────────────────────────────────────────
    # 注意：跳变阈值只用于「相邻两次采集之间」的判异，不用于 7 日累计涨幅。
    # 实测存在 7 日涨幅 +79% 的真实行情，拿累计涨幅判异会把真实暴涨误杀。
    suspect_jump_pct: float = 60.0
    stale_after_minutes: int = 30      # 数据超过该时长未更新则标记为过期

    # ── 告警 ────────────────────────────────────────────────────────────
    alert_enabled: bool = True
    alert_quiet_hours: str = "23:30-07:00"   # 静默时段，普通规则跳过
    alert_cooldown_minutes: int = 30         # 同一规则触发冷却

    # ── 文件路径 ────────────────────────────────────────────────────────
    catalog_path: Path = PROJECT_ROOT / "backend" / "catalog" / "ammo_catalog.yaml"
    fixtures_dir: Path = PROJECT_ROOT / "data" / "fixtures"

    @field_validator("db_path", "catalog_path", "fixtures_dir", mode="before")
    @classmethod
    def _resolve_relative(cls, v):
        """把配置里的相对路径解析到项目根目录下，避免受启动目录影响。"""
        if v is None:
            return v
        p = Path(v)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    def ensure_dirs(self) -> None:
        """确保运行期需要的目录存在。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置单例：整个进程只读一次 .env。"""
    s = Settings()
    s.ensure_dirs()
    return s
