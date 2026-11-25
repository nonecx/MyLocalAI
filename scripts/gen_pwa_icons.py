# -*- coding: utf-8 -*-
"""生成 PWA「添加到桌面」所需图标（零第三方依赖：纯标准库写 PNG）。

产物（写入 src/mylocalai/web/static/icons/）：
    icon-192.png           192×192  普通图标（Chrome/Edge/Android 主屏）
    icon-512.png           512×512  普通图标（安装弹窗/应用列表大图）
    icon-512-maskable.png  512×512  maskable 图标（Android 自适应裁切，图形收在安全区内）
    apple-touch-icon.png   180×180  iOS「添加到主屏幕」图标（满铺方图，圆角由系统加）

为什么自己画而不是引依赖：本项目前端零构建、后端只要 FastAPI，
为 4 张小图引入 Pillow 不划算；用距离场（SDF）逐像素算覆盖率即可得到
带抗锯齿的边缘，几十行标准库代码足够。

用法（项目根目录）：
    .venv/Scripts/python.exe -X utf8 scripts/gen_pwa_icons.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "mylocalai" / "web" / "static" / "icons"

# 与 style.css 的 --primary / --panel 对齐，保证图标和页面同一套配色
BG = (0x35, 0x61, 0x8F)
FG = (0xFF, 0xFF, 0xFF)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _sd_round_rect(px: float, py: float, half: float, radius: float) -> float:
    """圆角矩形有符号距离（负数在内部）。px/py 相对中心。"""
    qx = abs(px) - (half - radius)
    qy = abs(py) - (half - radius)
    ax, ay = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ax, ay) + min(max(qx, qy), 0.0) - radius


def _sd_circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(px - cx, py - cy) - r


def _sd_segment(px: float, py: float, ax: float, ay: float,
                bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = _clamp01((wx * vx + wy * vy) / (vx * vx + vy * vy))
    return math.hypot(wx - t * vx, wy - t * vy)


def _render(size: int, *, maskable: bool, full_bleed: bool) -> bytes:
    """画一张 size×size 的 RGBA 图，返回 PNG 字节流。

    maskable=True：Android 会把图标裁成圆形/圆角方块，图形需收在直径 80%
      的安全区内，因此背景满铺、放大镜缩到约 52%。
    full_bleed=True：iOS 主屏图标由系统加圆角，图本身必须满铺（不能自带留白，
      否则桌面上会出现「圆角里套圆角」的双层边框）。
    """
    half = size / 2.0
    radius = 0.0 if full_bleed else size * 0.22
    # 放大镜中心与半径：maskable 收到安全区内，普通图标可以撑得更大。
    # 注意像素坐标以图心为原点（px/py 已减 half），所以这里给的是相对偏移。
    scale = 0.52 if maskable else 0.68
    cx = -size * scale * 0.07
    cy = -size * scale * 0.09
    ring_r = size * scale * 0.5 * 0.62
    ring_t = max(2.0, size * scale * 0.075)   # 镜圈线宽
    # 手柄沿 45° 向右下伸出，起点压在镜圈内侧避免出现接缝
    handle_a = (cx + ring_r * 0.66, cy + ring_r * 0.66)
    handle_b = (cx + ring_r * 1.02, cy + ring_r * 1.02)
    handle_t = ring_t * 0.92

    rows = []
    for y in range(size):
        row = bytearray()
        py = y + 0.5 - half
        for x in range(size):
            px = x + 0.5 - half
            # 底色覆盖率：满铺图恒为 1，否则圆角矩形向外淡出
            bg_cov = 1.0 if full_bleed else _clamp01(
                0.5 - _sd_round_rect(px, py, half, radius))
            # 前景覆盖率：镜圈（环）与手柄（线段）取并集
            ring_d = abs(_sd_circle(px, py, cx, cy, ring_r)) - ring_t / 2.0
            handle_d = _sd_segment(px, py, *handle_a, *handle_b) - handle_t / 2.0
            fg_cov = _clamp01(0.5 - min(ring_d, handle_d))
            if bg_cov <= 0.0 and fg_cov <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            a = bg_cov if bg_cov > 0.0 else 0.0
            r, g, b = BG
            if fg_cov > 0.0:
                # 前景按覆盖率与白混色，同时保证圆角外的像素 alpha 正确
                r = round(BG[0] + (FG[0] - BG[0]) * fg_cov)
                g = round(BG[1] + (FG[1] - BG[1]) * fg_cov)
                b = round(BG[2] + (FG[2] - BG[2]) * fg_cov)
                a = max(a, fg_cov)
            row += bytes((r, g, b, round(a * 255)))
        rows.append(bytes(row))
    return _encode_png(size, size, rows)


def _encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + r for r in rows)      # 每行滤波类型 0（None）
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = (
        ("icon-192.png", 192, dict(maskable=False, full_bleed=False)),
        ("icon-512.png", 512, dict(maskable=False, full_bleed=False)),
        ("icon-512-maskable.png", 512, dict(maskable=True, full_bleed=True)),
        ("apple-touch-icon.png", 180, dict(maskable=False, full_bleed=True)),
    )
    for name, size, opts in targets:
        path = OUT_DIR / name
        path.write_bytes(_render(size, **opts))
        print(f"wrote {path.relative_to(ROOT)} ({size}x{size}, {path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
