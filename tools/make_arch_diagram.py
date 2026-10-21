# -*- coding: utf-8 -*-
"""生成《三角洲行动子弹物价监控系统》架构图（SVG，无第三方依赖）。

为什么用 SVG 而不是 PNG
-----------------------
1. 零依赖：不需要 Pillow 之类的图像库，标准库即可生成；
2. 无限缩放：在任何屏幕上文字都清晰，不会被位图放大糊掉；
3. 可读可改：出问题直接改代码里的坐标，不需要重装绘图环境；
4. 体积小：典型 10KB 量级，而同等清晰度的 PNG 要几百 KB。

用法
----
    python tools/make_arch_diagram.py
    python tools/make_arch_diagram.py --out docs/arch.svg
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

W, H = 1560, 1080

# ── 配色 ────────────────────────────────────────────────────────────────
C_BG = "#f6f8fc"
C_CARD = "#ffffff"
C_BORDER = "#c6d2e6"
C_NAVY = "#182e54"
C_TEXT = "#33415c"
C_MUTED = "#68748b"
C_AMBER_BG, C_AMBER_BD = "#fff9ec", "#e9b054"
C_BLUE_BG, C_BLUE_BD = "#eef7ff", "#5a9ce0"
C_GREEN_BG, C_GREEN_BD = "#f0fdf4", "#6ebe8c"
C_PURPLE_BG, C_PURPLE_BD = "#faf5ff", "#9e82dc"
C_RED_BG, C_RED_BD = "#fff4f4", "#e68c8c"
C_LINE = "#96a5be"
C_HAIRLINE = "#d2dae8"


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


class Svg:
    """极简 SVG 构建器。只实现本项目需要的几个图元。"""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.parts: list[str] = []

    def rect(self, x, y, w, h, fill, stroke=None, rx=12, sw=1.6, dash=None):
        s = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"')
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{sw}"'
        if dash:
            s += f' stroke-dasharray="{dash}"'
        self.parts.append(s + "/>")

    def text(self, x, y, s, size=14, fill=C_TEXT, weight="normal", anchor="start"):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="Microsoft YaHei, Segoe UI, PingFang SC, sans-serif">'
            f"{esc(s)}</text>"
        )

    def line(self, x1, y1, x2, y2, stroke=C_LINE, sw=1.6, marker=True, dash=None):
        s = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
             f'stroke-width="{sw}"')
        if dash:
            s += f' stroke-dasharray="{dash}"'
        if marker:
            s += ' marker-end="url(#arrow)"'
        self.parts.append(s + "/>")

    def render(self) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}">\n'
            "<defs>\n"
            '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto-start-reverse">\n'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{C_LINE}"/>\n'
            "</marker>\n"
            "</defs>\n"
            f'<rect width="{self.w}" height="{self.h}" fill="{C_BG}"/>\n'
            + "\n".join(self.parts)
            + "\n</svg>\n"
        )


def card(svg: Svg, x, y, w, h, title, lines, bg, bd, title_size=16,
         body_size=13, line_h=21):
    svg.rect(x, y, w, h, bg, bd)
    svg.text(x + 16, y + 24, title, size=title_size, fill=C_NAVY, weight="bold")
    ty = y + 24 + line_h + 4
    for ln in lines:
        svg.text(x + 16, ty, ln, size=body_size, fill=C_TEXT)
        ty += line_h


def main() -> int:
    ap = argparse.ArgumentParser(description="生成架构图 SVG")
    ap.add_argument("--out", default="docs/arch.svg")
    args = ap.parse_args()

    s = Svg(W, H)

    # ── 标题 ────────────────────────────────────────────────────────────
    s.text(40, 56, "三角洲行动 · 子弹物价实时监控系统", size=32, fill=C_NAVY, weight="bold")
    s.text(40, 86, "单机自用 · 本地网页看板 · 只做监控与告警，不触碰游戏客户端",
           size=15, fill=C_MUTED)
    s.line(40, 106, W - 40, 106, stroke=C_HAIRLINE, sw=1.4, marker=False)

    s.text(40, 140, "分层视图（L1 → L5 单向依赖）", size=17, fill=C_NAVY, weight="bold")
    s.text(W - 40, 140, "数据源可替换", size=17, fill="#be5a28", weight="bold", anchor="end")

    # ── L1 数据源 ───────────────────────────────────────────────────────
    LX, LY, LW = 40, 160, 900
    s.rect(LX, LY, LW, 168, C_CARD, C_BORDER, rx=14)
    s.text(LX + 16, LY + 28, "L1  数据源层（可插拔适配器）", size=16, fill=C_NAVY, weight="bold")

    card(s, LX + 16, LY + 44, 420, 108,
         "A  公开只读接口（已实测采用）",
         ["· POST /api/Items/search",
          "· 无需登录 / 无需 Token / 无需执行 JS",
          "· 弹药 113 条，83 条可交易；单一参考价"],
         C_GREEN_BG, C_GREEN_BD, title_size=14, body_size=12, line_h=20)

    card(s, LX + 452, LY + 44, 432, 108,
         "B  付费聚合服务（备选，未采用）",
         ["· 稳定但按资源包计费",
          "· 唯一能提供『子弹自选包收益』的来源",
          "· 换源只改适配器，不动上层"],
         C_BLUE_BG, C_BLUE_BD, title_size=14, body_size=12, line_h=20)

    # ── L2 采集 ─────────────────────────────────────────────────────────
    LY2 = 356
    s.rect(LX, LY2, LW, 150, C_CARD, C_BORDER, rx=14)
    s.text(LX + 16, LY2 + 28, "L2  采集与归一化（Collector / ETL）",
           size=16, fill=C_NAVY, weight="bold")

    boxes = [
        ("调度器", ["APScheduler", "10 分钟 + 抖动", "指数退避 1/4/16s"]),
        ("适配器", ["DerGameAdapter", "外部 ID → ammo_id", "字段归一为 ItemQuote"]),
        ("清洗校验", ["负价/未来时间戳", "15 分钟内跳变 > 60%", "判异入库但不进曲线"]),
        ("幂等落库", ["唯一索引去重", "重复采集自动覆盖", "suspect 标记留证"]),
    ]
    bx = LX + 16
    for i, (t, ls) in enumerate(boxes):
        card(s, bx, LY2 + 44, 205, 92, t, ls, C_GREEN_BG if i % 2 == 0 else C_BLUE_BG,
             C_GREEN_BD if i % 2 == 0 else C_BLUE_BD,
             title_size=14, body_size=11, line_h=19)
        if i < 3:
            s.line(bx + 207, LY2 + 90, bx + 219, LY2 + 90, stroke="#96c6a6", sw=1.6)
        bx += 221

    s.line(LX + 250, LY + 168, LX + 250, LY2)
    s.line(LX + 650, LY + 168, LX + 650, LY2)

    # ── L3 存储与分析 ───────────────────────────────────────────────────
    LY3 = 536
    s.rect(LX, LY3, LW, 182, C_CARD, C_BORDER, rx=14)
    s.text(LX + 16, LY3 + 28, "L3  存储与分析（Storage & Analytics）",
           size=16, fill=C_NAVY, weight="bold")

    boxes3 = [
        ("SQLite 时序库", ["price_tick 明细 30 天", "1h / 1d 聚合", "WAL + 幂等写入"]),
        ("指标计算（纯函数）", ["涨跌幅 / MA / EMA", "分位数（带样本量）", "σ 与 z-score"]),
        ("规则引擎（纯函数）", ["阈值 / 涨跌幅 / 分位", "冷却 + 静默 + 持续确认", "同物品合并防轰炸"]),
        ("历史回补", ["7 天逐小时（约 168 点）", "首次启动即回补", "分位数立即可用"]),
    ]
    bx = LX + 16
    for t, ls in boxes3:
        card(s, bx, LY3 + 44, 205, 124, t, ls, C_PURPLE_BG, C_PURPLE_BD,
             title_size=13, body_size=11, line_h=19)
        bx += 221

    s.line(LX + 250, LY2 + 150, LX + 250, LY3)
    s.line(LX + 650, LY2 + 150, LX + 650, LY3)

    # ── L4 服务 ─────────────────────────────────────────────────────────
    LY4 = 748
    s.rect(LX, LY4, LW, 132, C_CARD, C_BORDER, rx=14)
    s.text(LX + 16, LY4 + 28, "L4  服务层（FastAPI，仅绑 127.0.0.1）",
           size=16, fill=C_NAVY, weight="bold")

    apis = ["/api/ammo", "/api/price/{id}", "/api/rank", "/api/alerts/*",
            "/api/health", "/api/ingest/trigger"]
    bx = LX + 16
    for a in apis:
        w = 22 + len(a) * 7.6
        s.rect(bx, LY4 + 48, w, 32, "#ecfdf5", "#50b482", rx=8, sw=1.4)
        s.text(bx + 11, LY4 + 69, a, size=12, fill="#1c3c30")
        bx += w + 10

    s.text(LX + 16, LY4 + 108,
           "无多用户体系 · 无鉴权 · 不暴露公网 · 单进程 + 单 SQLite 文件",
           size=12, fill=C_MUTED)

    s.line(LX + 250, LY3 + 182, LX + 250, LY4)
    s.line(LX + 650, LY3 + 182, LX + 650, LY4)

    # ── L5 展示与告警 ───────────────────────────────────────────────────
    RX, RY, RW = 972, 160, 548
    s.rect(RX, RY, RW, 420, C_CARD, C_BORDER, rx=14)
    s.text(RX + 16, RY + 28, "L5  展示层（浏览器看板，纯离线）",
           size=16, fill=C_NAVY, weight="bold")

    ui = [
        ("行情总览", "搜索 / 品质 / 口径筛选 · 多列排序 · 红涨绿跌"),
        ("价格曲线", "1h / 6h / 24h / 7d 切换 · 周内高低线标注"),
        ("排行榜", "涨幅 / 跌幅 / 波动 / 机会分 / 低位榜"),
        ("自选与告警", "规则 CRUD · 触发历史 · 浏览器内提醒"),
        ("数据健康", "新鲜度横幅 · 数据源状态 · 手动触发采集"),
    ]
    yy = RY + 44
    for t, d in ui:
        s.rect(RX + 16, yy, RW - 32, 66, C_AMBER_BG, C_AMBER_BD, rx=10, sw=1.4)
        s.text(RX + 32, yy + 26, t, size=14, fill=C_NAVY, weight="bold")
        s.text(RX + 32, yy + 48, d, size=12, fill=C_TEXT)
        yy += 76

    # 服务层 → 展示层
    s.line(LX + LW, LY4 + 60, RX - 6, RY + 240, stroke="#78a0d2", sw=1.8)

    # ── 通知通道 ────────────────────────────────────────────────────────
    NY = 600
    s.rect(RX, NY, RW, 112, C_RED_BG, C_RED_BD, rx=14)
    s.text(RX + 16, NY + 28, "通知通道", size=16, fill=C_NAVY, weight="bold")
    s.text(RX + 16, NY + 54, "第一版：浏览器内提醒 + 触发历史（已实现）", size=12, fill=C_TEXT)
    s.text(RX + 16, NY + 76, "预留：系统托盘弹窗 / Server酱 / Bark / 邮件",
           size=12, fill=C_TEXT)
    s.text(RX + 16, NY + 96, "扩展方式：register_channel() 加一个函数，规则逻辑不动",
           size=11, fill=C_MUTED)
    s.line(RX + 274, NY, RX + 274, RY + 420, stroke=C_RED_BD, sw=1.6, dash="5 4")

    # ── 技术栈 ──────────────────────────────────────────────────────────
    TY = 736
    s.rect(RX, TY, RW, 144, C_CARD, C_BORDER, rx=14)
    s.text(RX + 16, TY + 28, "技术栈与设计取舍", size=16, fill=C_NAVY, weight="bold")
    for i, line in enumerate([
        "后端  Python 3.11 · FastAPI · SQLAlchemy 2 · APScheduler · httpx",
        "前端  原生 HTML + JS + 手写 SVG 图表（零构建、零外部 CDN）",
        "存储  SQLite（WAL）单文件；明细 30 天，聚合永久",
        "测试  82 个单元测试，全部离线可跑（fixture + 假适配器）",
        "取舍  不用 React / 不上云 / 不引入迁移工具 / 不做自动交易",
    ]):
        s.text(RX + 16, TY + 56 + i * 21, line, size=12, fill=C_TEXT)

    # ── 数据流 ──────────────────────────────────────────────────────────
    s.rect(40, 900, W - 80, 130, C_CARD, C_BORDER, rx=14)
    s.text(56, 928, "数据流", size=16, fill=C_NAVY, weight="bold")
    s.text(56, 962,
           "公开接口  →  适配器归一化  →  校验（判异标记）  →  幂等落库  →  聚合与指标  →  规则判定  →  浏览器提醒",
           size=13, fill=C_TEXT)
    s.text(56, 990,
           "历史曲线回补 ── 首次启动，7 天逐小时 ── 直接进入落库，使分位数立即可用",
           size=12, fill=C_MUTED)
    s.text(56, 1016,
           "说明：本系统不含任何游戏客户端注入、内存读取、自动下单或抢购模块。数据版权归腾讯及游戏官方所有。",
           size=12, fill="#be5a28")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(s.render(), encoding="utf-8")
    print(f"saved: {out}  ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
