# -*- coding: utf-8 -*-
"""关注度快照趋势线（M3-4）：纯 SVG 渲染 + 趋势摘要。

`attention.py` 的一次快照只回答"现在有多热"；本模块把**同档案的快照序列**画成
一条趋势线并给出方向摘要，回答"热往哪个方向走"。供 `dossier.service` 的热度
章节（`_trend_block`）按钉死契约 try-import：

    render_trend_svg(snapshots: list[dict]) -> str
    trend_summary(snapshots: list[dict]) -> dict

输入 `snapshots` 是档案库 `store.snapshots(id)` 的行（每行含 taken_at / e_score /
x / y / z / profile / proxy_flags 等，按时间升序），本模块**只读 taken_at 与
e_score 两个键**，其余字段忽略；输入顺序即绘制顺序（store 已按 taken_at, id
升序返回，本模块不做重排——重排会把"最新"悄悄换掉，方向就谎报了）。

设计约束（与 attention 包既有约定一致）：
   - **纯函数零依赖**：只用标准库 `math`，不联网、不碰 LLM、不读文件、不改状态；
     同一输入必然复现同一输出，可被单元测试精确断言。
   - **永不抛异常**：坏行跳过（非 dict / 缺 taken_at 或 e_score 键 / e_score 不是
     有限实数），全部行无效时与空输入同样给"暂无快照"占位；内部再兜一层
     try/except，趋势是装饰性输出，绝不能拖垮章节读取。
   - **口径诚实**：e_score 是确定性公式输出但 y/z 多为代理值，方向只描述本序列
     首末两点之差，不外推、不预测。
   - **可直接内嵌 HTML**：输出是带 xmlns 的完整 `<svg>...</svg>` 字符串，全部
     属性内联（无外部 CSS/JS）；渲染内容只含常量与数字（数值经 :g/:.2f 格式化），
     用户字符串一律不进 SVG，因此无需转义也不存在注入面。

尺寸与配色常量集中在模块顶部，改这里即可整体调整样式。
"""

from __future__ import annotations

import math
from typing import Any, List, Tuple

__all__ = ["render_trend_svg", "trend_summary"]

# ---- 尺寸常量 ----
SVG_WIDTH: int = 420
SVG_HEIGHT: int = 170
PLOT_LEFT: float = 36.0    # 左侧留白：容纳 0/50/100 刻度文字
PLOT_RIGHT: float = 14.0
PLOT_TOP: float = 16.0     # 顶部留白：容纳首点数值标签
PLOT_BOTTOM: float = 10.0

# ---- 配色与字号常量 ----
GRID_COLOR: str = "#d0d7de"        # 0-100 刻度线
TICK_TEXT_COLOR: str = "#57606a"   # 刻度数字
LINE_COLOR: str = "#1f6feb"        # 折线
POINT_COLOR: str = "#1f6feb"       # 快照点
LABEL_COLOR: str = "#57606a"       # 首末 e_score 数值标签
PLACEHOLDER_COLOR: str = "#8b949e" # "暂无快照"占位文字
FONT_FAMILY: str = "sans-serif"
FONT_SIZE_TICK: int = 10
FONT_SIZE_LABEL: int = 11
FONT_SIZE_PLACEHOLDER: int = 12

# ---- 形状常量 ----
POINT_RADIUS: float = 3.0          # 快照点圆半径
LINE_WIDTH: float = 2.0            # 折线宽
LABEL_LIFT: float = 6.0            # 数值标签相对点位的上移量
LABEL_MIN_Y: float = 11.0          # 标签基线最低不小于此（防顶部裁切）

#: e_score 绘图范围：公式保证 E 严格落在 (0, 100)，越界的脏值绘图时夹回 [0, 100]
#: （只影响画笔坐标，trend_summary 的数值仍如实回显原值）
E_RANGE: Tuple[float, float] = (0.0, 100.0)

#: 0-100 刻度线取值（横向网格线 + 左侧刻度数字）
GRID_VALUES: Tuple[int, ...] = (0, 50, 100)

#: 方向判定阈值：|last - first| <= DIRECTION_EPSILON 视为 flat（常量化，防魔法数）。
#: E 存库是 double，1e-9 以内的差在展示精度（:g 六位有效数字）下不可见，判平更诚实。
DIRECTION_EPSILON: float = 1e-9


# ---------------------------------------------------------------------------
# 输入清洗：坏行跳过（契约钉死：缺 taken_at / e_score 键的行不算数）
# ---------------------------------------------------------------------------

def _valid_points(snapshots: Any) -> List[Tuple[float, str]]:
    """从 snapshots 抽出有效点 [(e_score, taken_at), ...]，**保持输入顺序**。

    坏行跳过：非 dict、缺 taken_at 或 e_score 键、e_score 非 int/float（bool 不算）、
    非有限（nan/inf）的行一律丢弃；snapshots 本身不是 list/tuple 时返回空列表。
    """
    if not isinstance(snapshots, (list, tuple)):
        return []
    points: List[Tuple[float, str]] = []
    for row in snapshots:
        if not isinstance(row, dict):
            continue
        if "taken_at" not in row or "e_score" not in row:
            continue
        value = row["e_score"]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        value = float(value)
        if not math.isfinite(value):
            continue
        points.append((value, str(row.get("taken_at") or "")))
    return points


def _clamp_e(value: float) -> float:
    """把 e_score 夹进绘图范围 [0, 100]（公式输出本就在开区间内，纯防御）。"""
    low, high = E_RANGE
    if value < low:
        return low
    if value > high:
        return high
    return value


# ---------------------------------------------------------------------------
# 趋势摘要
# ---------------------------------------------------------------------------

def trend_summary(snapshots: list[dict]) -> dict:
    """快照序列的趋势摘要。**纯函数、永不抛异常。**

    返回（键固定，便于单测与前端逐项断言）：
        count     有效快照数（坏行跳过后的数量）
        first     首个有效 e_score（无有效快照时为 None）
        last      最后一个有效 e_score（同上）
        min/max   序列内最小/最大 e_score（同上）
        direction "up"（last 明显高于 first）/ "down"（明显低于）/ "flat"
                  —— 有效快照不足 2 条，或首末差不超过 DIRECTION_EPSILON 时为 "flat"

    direction 只比较**首末两点**，不拟合、不外推；输入顺序即时间顺序
    （store.snapshots 按 taken_at 升序返回）。
    """
    try:
        points = _valid_points(snapshots)
    except Exception:  # noqa: BLE001  摘要与渲染一样不许抛
        points = []
    values = [value for value, _ in points]
    if not values:
        return {"count": 0, "first": None, "last": None,
                "min": None, "max": None, "direction": "flat"}
    first, last = values[0], values[-1]
    if len(values) < 2 or abs(last - first) <= DIRECTION_EPSILON:
        direction = "flat"
    elif last > first:
        direction = "up"
    else:
        direction = "down"
    return {"count": len(values), "first": first, "last": last,
            "min": min(values), "max": max(values), "direction": direction}


# ---------------------------------------------------------------------------
# 纯 SVG 渲染
# ---------------------------------------------------------------------------

def render_trend_svg(snapshots: list[dict]) -> str:
    """把快照序列渲染成可直接内嵌 HTML 的 `<svg>` 字符串。**永不抛异常。**

    形态：
        0 个有效点  —— "暂无快照"占位 svg（全部行坏 / 输入为空同理）
        1 个有效点  —— 单点标记 + 该点 e_score 数值标签 + 0-100 刻度线
        ≥2 个有效点 —— polyline 折线 + 每点圆标 + 首末 e_score 数值标签 + 0-100 刻度线

    渲染内容只含常量与格式化数字，不含任何用户字符串（taken_at 不上画面），
    无转义需求、无注入面。
    """
    try:
        return _render(_valid_points(snapshots))
    except Exception:  # noqa: BLE001  趋势是装饰性输出，任何内部异常都退占位
        return _placeholder_svg()


def _render(points: List[Tuple[float, str]]) -> str:
    if not points:
        return _placeholder_svg()

    plot_w = SVG_WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_h = SVG_HEIGHT - PLOT_TOP - PLOT_BOTTOM
    parts: List[str] = [
        _svg_open(f"关注度快照趋势线（{len(points)} 个快照，E 为确定性公式输出）")
    ]
    parts.extend(_gridlines(plot_h))

    if len(points) == 1:
        # 单点：居中放置，避免"只有一个点还假装有趋势"的误导
        value = _clamp_e(points[0][0])
        cx = PLOT_LEFT + plot_w / 2.0
        cy = _e_to_y(value, plot_h)
        parts.append(_circle(cx, cy))
        parts.append(_value_label(cx, cy, points[0][0], anchor="middle"))
    else:
        coords: List[Tuple[float, float]] = []
        count = len(points)
        for index, (value, _) in enumerate(points):
            x = PLOT_LEFT + plot_w * index / (count - 1)
            coords.append((x, _e_to_y(_clamp_e(value), plot_h)))
        polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
        parts.append(f'<polyline points="{polyline}" fill="none" '
                     f'stroke="{LINE_COLOR}" stroke-width="{LINE_WIDTH}"/>')
        for x, y in coords:
            parts.append(_circle(x, y))
        parts.append(_value_label(coords[0][0], coords[0][1], points[0][0],
                                  anchor="start"))
        parts.append(_value_label(coords[-1][0], coords[-1][1], points[-1][0],
                                  anchor="end"))

    parts.append("</svg>")
    return "".join(parts)


def _svg_open(aria_label: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" '
            f'height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
            f'role="img" aria-label="{aria_label}">')


def _e_to_y(value: float, plot_h: float) -> float:
    """E 值（0-100）→ 画布 y 坐标：E=100 顶对齐 PLOT_TOP，E=0 底对齐。"""
    return PLOT_TOP + plot_h * (1.0 - _clamp_e(value) / 100.0)


def _gridlines(plot_h: float) -> List[str]:
    """0-100 横向刻度线 + 左侧刻度数字。"""
    out: List[str] = []
    right = SVG_WIDTH - PLOT_RIGHT
    for value in GRID_VALUES:
        y = _e_to_y(float(value), plot_h)
        out.append(f'<line x1="{PLOT_LEFT:.2f}" y1="{y:.2f}" x2="{right:.2f}" '
                   f'y2="{y:.2f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        out.append(f'<text x="{PLOT_LEFT - 6:.2f}" y="{y + 3.5:.2f}" '
                   f'text-anchor="end" font-family="{FONT_FAMILY}" '
                   f'font-size="{FONT_SIZE_TICK}" fill="{TICK_TEXT_COLOR}">'
                   f'{value}</text>')
    return out


def _circle(x: float, y: float) -> str:
    return (f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{POINT_RADIUS}" '
            f'fill="{POINT_COLOR}"/>')


def _value_label(x: float, y: float, value: float, anchor: str) -> str:
    """首末（或单点）e_score 数值标签：点位上方悬浮，顶部防裁切。

    数值用 :g（六位有效数字）——与 dossier/service._fmt_g 同口径，不四舍五入
    到固定位数也不谎报精度。
    """
    label_y = max(y - LABEL_LIFT, LABEL_MIN_Y)
    return (f'<text x="{x:.2f}" y="{label_y:.2f}" text-anchor="{anchor}" '
            f'font-family="{FONT_FAMILY}" font-size="{FONT_SIZE_LABEL}" '
            f'fill="{LABEL_COLOR}">{value:g}</text>')


def _placeholder_svg() -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" '
            f'height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" '
            f'role="img" aria-label="暂无快照">'
            f'<text x="{SVG_WIDTH / 2:.2f}" y="{SVG_HEIGHT / 2:.2f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="{FONT_FAMILY}" font-size="{FONT_SIZE_PLACEHOLDER}" '
            f'fill="{PLACEHOLDER_COLOR}">暂无快照</text></svg>')


