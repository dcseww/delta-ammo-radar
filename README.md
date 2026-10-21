# 三角洲行动 · 子弹物价实时监控系统

> 单机自用 · 本地浏览器看板 · **只做监控、分析与告警**
>
> 不做自动下单、不做抢购、不注入游戏客户端、不读取游戏内存、不对外提供数据服务。

---

## 现在就能用

### 最快：双击桌面图标

桌面上的 **「子弹物价监控」** 快捷方式，双击即启动：后台拉起前端 + 后端 → 自动打开看板。
全程没有黑框闪现；服务已经在运行时不会重复启动，只会把看板再打开一次；启动失败会弹窗提示。

> 换了电脑 / 快捷方式丢了就重建：`.\install_shortcut.ps1`
> 想放开始菜单：`.\install_shortcut.ps1 -StartMenu`；想开机自启：`.\install_shortcut.ps1 -Startup`
> 想删掉快捷方式：`.\install_shortcut.ps1 -Remove`

### 命令行

```powershell
# 一键启动（后台运行；日志写 logs\backend.log 与 logs\backend.log.err）
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1

# 前后端分离：后端 8787 + 前端静态服务器 8788（改前端不用重启后端）
.\start.ps1 -Dev

.\start.ps1 -Status    # 查看前后端状态
.\start.ps1 -Stop      # 停止

# 然后打开
#   看板   http://127.0.0.1:8787
#   接口文档 http://127.0.0.1:8787/docs
```

| 启动相关文件 | 作用 |
| --- | --- |
| [start.ps1](start.ps1) | 主力启动器：默认一体模式（后端同时托管前端），`-Dev` 分离模式，另有 `-Status` / `-Stop` / `-Foreground` |
| [install_shortcut.ps1](install_shortcut.ps1) | 生成桌面快捷方式（带图标），`-StartMenu` / `-Startup` / `-Remove` / `-Visible` |
| [启动子弹监控.vbs](启动子弹监控.vbs) | 静默启动入口（桌面快捷方式指向它）：无黑框，失败弹窗 |
| [启动子弹监控.bat](启动子弹监控.bat) | 控制台回退入口：能看到启动全过程，.vbs 被策略拦截时用它 |
| [scripts/serve_frontend.py](scripts/serve_frontend.py) | 分离模式的前端静态服务器，把 `/api` 反代给后端（同源、无跨域） |
| [tools/make_icon.py](tools/make_icon.py) | 生成 `frontend/assets/app.ico`（快捷方式与浏览器标签页共用） |

> `run.ps1` 是早期启动脚本，仍然可用；新用法建议走 `start.ps1`（多了前后端分离、按端口探测进程、日志 UTF-8 且不缓冲）。

首次启动会自动**回补 7 天逐小时历史**（85 个可交易弹药，约 2 分钟），
所以打开页面就能看到曲线、分位数和排行榜，不需要等几天攒数据。

---

## 当前进度

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| 项目开发文档 | ✅ | 16 章 + 2 附录，含架构图 |
| 数据源路线 | ✅ | 公开只读接口，无需登录、无需 Token |
| **M0 环境就绪** | ✅ | 依赖齐备，工作区权限已修 |
| **M1 骨架** | ✅ | FastAPI + SQLAlchemy + SQLite(WAL) |
| **M2 数据闭环** | ✅ | 真实采集入库，**15,000+ 个价格点**，判异 0；调度器已自主运行多轮 |
| **历史回补** | ✅ | **85/85 物品 × 168 点（7 天逐小时）** |
| **M3 看板** | ✅ | 四视图 + 手写 SVG 图表，零 CDN 依赖 |
| **M4 告警** | 🟡 | 规则 CRUD / 判定引擎 / 触发历史 / 浏览器内提醒已实现；桌面弹窗与手机推送留到第二版 |
| **M5 加固** | 🟡 | 健康检查、降级、保留策略已实现；CSV 导出未做 |
| **自动化测试** | ✅ | **124 个测试，全部离线可跑** |

---

## 界面

| 视图 | 内容 |
| --- | --- |
| **行情总览** | 价格表（搜索 / 品质 / 口径筛选、多列排序、分位条、机会分）+ 排行榜（涨幅 / 跌幅 / 波动 / 机会 / 低位） |
| **自选** | 固定关注的弹药 |
| **告警** | 规则管理（价格阈值 / 涨跌幅 / 分位 / 异动）、触发历史 |
| **数据健康** | 新鲜度、数据源状态、采集历史、打包物项检查 |

点任意一行打开详情抽屉：价格曲线（1h / 6h / 24h / 7d / 30d）、周内高低线标注、14 项指标。

配色沿用国内习惯：**红涨绿跌**。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [项目开发文档](docs/三角洲子弹物价监控系统-开发文档.md) | 交付状态、需求与非目标、架构、数据源方案、SQLite 表结构、指标算法、API 契约、看板与告警设计、排期、风险登记册、合规声明 |
| [数据源实测报告](docs/数据源实测报告-dergame.md) | **必读**。接口规格、字段可信度分级、参数陷阱、命名坑、使用礼仪、采集策略 |
| [架构图](docs/arch.svg) | 分层泳道图（SVG，源文件 `tools/make_arch_diagram.py`） |

---

## 数据来源

| 项目 | 实测结果 |
| --- | --- |
| 主接口 | `POST https://dergame.cn/api/Items/search`，body `{"type":["弹药"]}` |
| 历史曲线 | `POST https://dergame.cn/api/Items/chartData`，body `{"itemId":"...","chartType":7}` |
| 是否需要登录 / Token | **不需要**（无 Cookie、无 Token、无 UA 校验） |
| 数据来源站 | DerGame（小Der数据报），一个免费的公益数据工具 |
| 弹药规模 | 113 条记录 / 111 个物品名，其中 85 条可交易 |
| 历史深度 | 7 天逐小时（168 点/物品） |
| 数据缺口 | **无打包弹药** → 「倒子弹」套利分析不可用（UI 已如实隐藏） |

### ⚠️ 最重要的一条：`name` 不能当唯一键

数据源里 **`.300BLK` 是三种不同品质的弹药**（grade 3/4/5），共用同一个名字，
但 ID 与价格完全不同：

| 名字 | ID | 品质 | 价格 |
| --- | --- | --- | --- |
| `.300BLK` | `37280500001` | 金 (5) | 4871 |
| `.300BLK` | `37280400001` | 紫 (4) | 1604 |
| `.300BLK` | `37280300001` | 蓝 (3) | 444 |

如果把这三条合并成一个物品，它的"价格"就会在 444~4871 之间漂移 ——
实测产生 4871 → 1604（**−67%**）的假跳变。

因此本项目的模型是：**一个（名称 + 数据源 ID）组合 = 一条独立价格序列**。
基础表里同名条目会加品质后缀（`300blk_g5` / `300blk_g4` / `300blk_g3`）区分。

> 同理，`slugify` 必须保留中文字符 —— 否则 `碳纤维刺骨箭矢` 与 `碳纤维穿甲箭矢`
> 都会被压成 `unknown` 而撞主键。

### 三个必须避开的坑（实测踩出来的）

1. **不要传 `grade` 参数** —— 接口会返回空。品质一律本地筛。
2. **不要用完整口径字符串搜索** —— 游戏内写法不统一：`.300BLK` 属于 `7.62x35mm` 口径但名字里
   没有该串；`5.56*45mm` 用星号；`45-70 Govt` 丢了前导点。用**短数字前缀**做超集匹配。
3. **警惕 100 条上限** —— 宽泛关键词会被截断并**静默丢数据**（`name:"9"` 漏掉部分 9mm 弹药，
   `name:"9mm"` 才捞全）。

### 一个重要的口径教训

数据源字段 `7zf` 最初被当作"7 日涨跌百分比"，端到端互校证明**它不是**：
价格贴近 7 日最低点时它仍可能是 +118 这种大正数。现已改名为 `source_index_7zf`，
标注为"定义未知"，**不参与任何计算与告警**。

真正可靠的是数据源自报的 `7top` / `7down` —— 实测与自算 7 日高低**完全一致**。

---

## 开发

### 常用命令

```powershell
$py = "C:\Users\cth\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

& $py -m pytest -q                                     # 124 个测试，离线
& $py scripts\verify_e2e.py                            # 端到端验收（打真实服务）
& $py scripts\check_backfill.py --watch 60             # 回补进度
& $py scripts\seed_catalog.py                          # 导入子弹基础表
& $py scripts\reset_db.py                              # 重置数据库（预览；加 --yes 执行，自动备份）
& $py -m backend.app                                   # 前台启动
```

> ⚠️ **PowerShell 用户注意**：本机执行策略默认禁止运行脚本，直接 `.\run.ps1` 会报
> "禁止运行脚本"。用一次性绕过即可：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
> ```
>
> 另外 `run.ps1` 已保存为 **UTF-8 带 BOM**。若你把它重存成无 BOM 的 UTF-8，
> PowerShell 5.1 会按 GBK 解码中文注释并导致语法错误 —— 请保持 BOM。

### 目录结构

```
子弹/
├─ run.ps1                          # 一键启动 / 停止 / 状态
├─ requirements.txt
├─ README.md
├─ docs/
│  ├─ 三角洲子弹物价监控系统-开发文档.md
│  ├─ 数据源实测报告-dergame.md
│  └─ arch.svg
├─ backend/
│  ├─ app.py                        # FastAPI 入口（lifespan 自动建表 + 导入基础表 + 启动调度）
│  ├─ config.py                     # 全部配置（pydantic-settings 读 .env）
│  ├─ db.py / models.py             # SQLite(WAL) + 8 张表
│  ├─ repository.py                 # 数据访问（幂等写入、聚合、保留策略）
│  ├─ schemas.py                    # API 出参
│  ├─ catalog/
│  │  ├─ ammo_catalog.yaml          # ★ 子弹基础表（113 条，唯一需人工核对）
│  │  └─ seed.py                    # 导入基础表（不覆盖 enabled）
│  ├─ sources/
│  │  ├─ base.py                    # SourceAdapter 协议 + 注册表
│  │  └─ dergame.py                 # 公开只读接口适配器
│  ├─ pipeline/
│  │  ├─ ingest.py                  # 采集编排 + 历史回补
│  │  ├─ validate.py                # 校验（纯函数）
│  │  ├─ metrics.py                 # 指标（纯函数）
│  │  ├─ aggregate.py               # 聚合入口
│  │  └─ scheduler.py               # APScheduler + 写锁 + 退避
│  ├─ alerts/
│  │  ├─ rules.py                   # 规则判定（纯函数，防轰炸四件套）
│  │  └─ notify.py                  # 通知通道（可扩展）
│  └─ api/                          # routes_ammo / routes_alerts / routes_health
├─ frontend/
│  ├─ index.html
│  └─ assets/{app.js, style.css}
├─ scripts/                         # seed_catalog / verify_e2e / check_backfill / reset_db
├─ tools/                           # 9 个只读探测与发现工具
├─ tests/                           # 124 个测试
└─ data/                            # 运行时数据（不入库）
```

### 测试覆盖

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_validate.py` | 负价、未来时间戳、跳变判异（含"7 天 +79% 不应判异"回归测试） |
| `test_metrics.py` | 均线、分位数边界、波动率、机会分、时区日切 |
| `test_rules.py` | 规则触发、冷却、跨夜静默、持续确认、同物品合并、样本量保护 |
| `test_adapter.py` | 字段解析、类型摇摆（int/float/字符串）、脏数据跳过、时区解析 |
| `test_ingest.py` | 幂等、判异隔离、ID 归属、健康记录、回补断点续跑、源未更新不重写、实时行不被回补覆盖 |
| `test_catalog_build.py` | **主键生成规则**：同名不同品质必须拆分、中文字符不得丢失、主键全局唯一 |
| `test_api.py` | 全接口集成、规则入口校验（含 percentile 阈值必须是 0~1）、告警队列、**物品详情与列表筛选解耦**（详情抽屉取数回归测试） |
| `test_frontend_css.py` | **前端样式表级联**：`hidden` 属性必须真的生效（曾因 `.drawer { display: flex }` 压过 `[hidden]` 导致"点关闭关不掉抽屉"） |

---

## 合规与自我约束

1. 本项目**仅用于个人学习与自用信息聚合**，不对外提供数据服务、不转售数据。
2. **不实现任何自动化游戏操作**（自动购买、自动挂单、抢购脚本、模拟点击、内存读取、注入）。
3. 不使用任何账号凭证；抓包路线已明确排除。
4. 游戏数据版权归腾讯及游戏官方所有；本项目不主张任何数据所有权。
5. 使用第三方公开服务时保持礼貌频率、错峰请求、本地缓存，并在展示时标注数据来源。
6. 演示数据来源：DerGame（小Der数据报），一个免费的公益数据工具。
