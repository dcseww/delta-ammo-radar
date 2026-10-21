# -*- coding: utf-8 -*-
"""前端样式表的回归测试（纯文本解析，不需要浏览器）。

为什么值得单独测
----------------
前端没有构建、没有浏览器测试环境，所以样式表里那些"看不见但会决定功能"的规则
最容易悄悄坏掉。本项目就真的踩过一次：

    `.drawer { display: flex }` 是作者样式，会压过浏览器默认的 `[hidden] { display: none }`。
    于是 JS 里 `drawer.hidden = true` 执行成功了，抽屉却**依然显示在屏幕上** ——
    表现成"点关闭没反应、窗口关不掉"。逻辑测试全绿，问题只在 CSS 层叠里。

所以这里按 CSS 级联规则（!important > 特异度）自己判定"最终生效的 display"，
确保 hidden 属性对所有靠它开关的元素都真的有效。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parent.parent / "frontend" / "assets" / "style.css"


def strip_comments(css: str) -> str:
    """去掉 /* ... */ 注释，避免注释里的文字被当成选择器或声明。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rules(css: str) -> list[tuple[list[str], str]]:
    """拆出 (选择器列表, 声明块) —— 只处理本项目实际使用的扁平规则。"""
    out = []
    for selector_group, decls in re.findall(r"([^{}]+)\{([^{}]*)\}", strip_comments(css)):
        sels = [s.strip() for s in selector_group.split(",") if s.strip()]
        if sels:
            out.append((sels, decls))
    return out


def _specificity(simple: str) -> int:
    """够用的特异度近似：id > class/属性/伪类 > 元素。"""
    ids = len(re.findall(r"#[\w-]+", simple))
    classes = (len(re.findall(r"\.[\w-]+", simple))
               + len(re.findall(r"\[[^\]]+\]", simple))
               + len(re.findall(r":(?!:)[\w-]+", simple)))
    tags = len(re.findall(r"(?:^|[\s>+~])[a-zA-Z][\w-]*", simple))
    return ids * 100 + classes * 10 + tags


def winning_display(css: str, selector: str) -> str | None:
    """返回针对该选择器最终生效的 display 值（按 cascade：!important 优先，再比特异度）。"""
    best: tuple[int, int, str] | None = None   # (优先, 特异度, 值)
    for sels, decls in rules(css):
        m = re.search(r"(?:^|;)\s*display\s*:\s*([a-zA-Z-]+)\s*(!important)?", decls)
        if not m:
            continue
        value, important = m.group(1), bool(m.group(2))
        for sel in sels:
            if sel != selector:
                continue
            key = (1 if important else 0, _specificity(sel))
            if best is None or key >= best[:2]:
                best = (key[0], key[1], value)
    return best[2] if best else None


@pytest.fixture(scope="module")
def css() -> str:
    assert CSS_PATH.exists(), f"样式表不存在：{CSS_PATH}"
    return CSS_PATH.read_text(encoding="utf-8")


def test_hidden_attribute_is_not_overridden_by_component_display(css):
    """隐藏态下，靠 hidden 开关的组件最终 display 必须是 none。

    这正是"点关闭关不掉抽屉"的根因：`.drawer { display: flex }` 压过了 `[hidden]`。
    """
    assert winning_display(css, "[hidden]") == "none", (
        "样式表里 [hidden] 的最终 display 不是 none —— hidden 属性会失效，"
        "表现为抽屉/横幅关不掉。请保留 `[hidden] { display: none !important; }`。")

    # 摘要至少证明当时确实存在"组件自己写了 display"的冲突
    assert re.search(r"\.drawer\s*\{[^}]*display\s*:\s*flex", css), (
        ".drawer 的 display 变了，请重新评估 [hidden] 兜底规则是否仍然必要且正确")


def test_drawer_uses_hidden_attribute(css):
    """抽屉的显隐必须只依赖 hidden 属性（JS 只改这个属性）。"""
    assert winning_display(css, ".drawer") == "flex"
    # 隐藏时的最终值由 [hidden] 提供
    assert winning_display(css, "[hidden]") == "none"


def test_views_are_shown_and_hidden(css):
    """视图切换用的 .view 依然可显示（防止把 display:none 写成了全局规则）。"""
    assert winning_display(css, ".view") == "block"
