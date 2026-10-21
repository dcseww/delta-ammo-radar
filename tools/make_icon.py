#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成应用图标（.ico + 预览 png）。

产出
----
    frontend/assets/app.ico        多尺寸图标（16/24/32/48/64/128/256），给 Windows 快捷方式用
    frontend/assets/app-icon.png   只用于预览/检查的大图

设计
----
深色圆角底 + 铜色「弹」字 + 右上角红色告警点。
要点是 16px 下也要认得出：所以只保留"一个字 + 一个点"，细碎装饰全部去掉。

用法
----
    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "assets"
ICO_PATH = ASSETS_DIR / "app.ico"
PNG_PATH = ASSETS_DIR / "app-icon.png"

# 超采样倍数：先画大图再缩小，边缘才干净
SUPERSAMPLE = 1024
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

BG_TOP = (24, 31, 41)
BG_BOTTOM = (10, 13, 17)
BORDER = (43, 53, 66)
GLYPH_TOP = (243, 200, 112)      # 铜色高光
GLYPH_BOTTOM = (198, 134, 40)    # 铜色暗部
ALERT = (229, 72, 77)            # 告警红（与看板红涨绿跌的"红"同色系）

GLYPH = "弹"
GLYPH_FONTS = [
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """生成竖直线性渐变（用 1px 宽的图拉伸，比逐像素快）。"""
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        column.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return column.resize((size, size), Image.BILINEAR)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def load_glyph_font(size: int) -> ImageFont.FreeTypeFont:
    for path in GLYPH_FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError("找不到可用的中文字体（试过：" + "、".join(GLYPH_FONTS) + "）")


def build_icon() -> Image.Image:
    s = SUPERSAMPLE
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # 1) 深色圆角底
    bg = vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA")
    icon.paste(bg, (0, 0), rounded_mask(s, radius=int(s * 0.225)))

    # 2) 一圈内描边，让边缘在浅色任务栏上也分得清
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(
        [int(s * 0.006), int(s * 0.006), s - 1 - int(s * 0.006), s - 1 - int(s * 0.006)],
        radius=int(s * 0.22),
        outline=BORDER + (255,),
        width=max(1, int(s * 0.008)),
    )

    # 3) 「弹」字：先取字形蒙版，再用铜色渐变填充，最后贴到底板上
    # 字号刻意留白：给右上角的红点让位，否则小尺寸下红点会糊进字形里
    font = load_glyph_font(int(s * 0.60))
    text_mask = Image.new("L", (s, s), 0)
    text_draw = ImageDraw.Draw(text_mask)
    left, top, right, bottom = text_draw.textbbox((0, 0), GLYPH, font=font)
    text_draw.text(
        (
            (s - (right - left)) / 2 - left,
            (s - (bottom - top)) / 2 - top + int(s * 0.025),
        ),
        GLYPH,
        font=font,
        fill=255,
    )
    glyph = vertical_gradient(s, GLYPH_TOP, GLYPH_BOTTOM).convert("RGBA")
    icon.paste(glyph, (0, 0), text_mask)

    # 4) 右上角告警红点（带一圈底色描边，和小尺寸下不糊边）
    r = int(s * 0.105)
    cx, cy = int(s * 0.785), int(s * 0.2)
    ring = int(s * 0.022)
    draw.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=BG_BOTTOM + (255,))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ALERT + (255,))

    return icon


def main() -> int:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    icon = build_icon()

    icon.save(ICO_PATH, format="ICO", sizes=[(n, n) for n in ICO_SIZES])
    icon.resize((256, 256), Image.LANCZOS).save(PNG_PATH, format="PNG")

    print(f"[OK] 已生成 {ICO_PATH}（尺寸：{', '.join(str(n) for n in ICO_SIZES)}）")
    print(f"[OK] 已生成 {PNG_PATH}（256×256 预览）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
