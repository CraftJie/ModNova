"""纯像素渲染：圆角位图、PNG 打包、窗口图标。

Tk 画布**没有抗锯齿**：不管是「矩形 + 椭圆」拼，还是采样成一条多边形，
1px 描边的圆角都只能落在整像素上，放大看就是一格格台阶。所以这里自己算：
按 4 倍超采样逐行填色，再缩回 1 倍，边缘就成了平滑过渡，跟浏览器里
border-radius 的观感一致。

不依赖任何第三方库（标准库 zlib 打包 PNG，base64 交给 Tk 的 PhotoImage）。
单独成模块，是因为它和 Modbus 一点关系都没有 —— gui.py 该管的是
「哪个控件放哪儿」，不是「某个像素的 alpha 是多少」。
"""

from __future__ import annotations

import base64
import math
import struct
import tkinter as tk
import zlib

from .ui_theme import ACCENT_PRESS


# ---------------------------------------------------------------------------
# 圆角位图渲染
#
# Tk 画布**没有抗锯齿**：不管是「矩形 + 椭圆」拼，还是采样成一条多边形，
# 1px 描边的圆角都只能落在整像素上，放大看就是一格格台阶。
# 所以这里自己算：按 4 倍超采样逐行填色，再缩回 1 倍，边缘就成了平滑过渡，
# 跟浏览器里 border-radius 的观感一致。
# ---------------------------------------------------------------------------

def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """两色按 t 线性插值，t=0 取 a。"""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = hex_rgb(a)
    rb, gb, bb = hex_rgb(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t),
                              round(ga + (gb - ga) * t),
                              round(ba + (bb - ba) * t))


def rounded_span(y: float, x1, y1, x2, y2, r):
    """某一条水平线与圆角矩形的交集区间；落在上下圆角里就按圆方程缩进。"""
    if y < y1 or y >= y2:
        return None
    if r <= 0:
        return x1, x2
    if y < y1 + r:
        dy = (y1 + r) - y
    elif y > y2 - r:
        dy = y - (y2 - r)
    else:
        return x1, x2
    dx = math.sqrt(max(0.0, r * r - dy * dy))
    return x1 + r - dx, x2 - r + dx


def fill_rows(buf, ss: int, width: int, x1, y1, x2, y2, r, color: str) -> None:
    cols = width * ss
    rgb = hex_rgb(color)
    for row_index in range(len(buf)):
        found = rounded_span((row_index + 0.5) / ss, x1, y1, x2, y2, r)
        if found is None:
            continue
        start = max(0, int(round(found[0] * ss)))
        end = min(cols, int(round(found[1] * ss)))
        if end > start:
            buf[row_index][start * 4:end * 4] = bytes((*rgb, 255)) * (end - start)


def _downsample(buf: list[bytearray], ss: int, w: int, h: int) -> bytes:
    """ss×ss 超采样缓冲缩回 1×：逐点对 ss² 个采样求平均得到抗锯齿结果。

    render_rounded 和 icon_png 用同一套「超采样 → 缩小平均」的流水线，
    这段循环原来抄了两份 —— 收进来之后两边的缩放算法才不会各自漂移。
    """
    out = bytearray(w * h * 4)
    n = ss * ss
    for y in range(h):
        for x in range(w):
            r_ = g_ = b_ = a_ = 0
            for dy in range(ss):
                row = buf[y * ss + dy]
                base_i = (x * ss) * 4
                for dx in range(ss):
                    i = base_i + dx * 4
                    r_ += row[i]
                    g_ += row[i + 1]
                    b_ += row[i + 2]
                    a_ += row[i + 3]
            o = (y * w + x) * 4
            out[o:o + 4] = bytes((r_ // n, g_ // n, b_ // n, a_ // n))
    return bytes(out)


def render_rounded(w: int, h: int, r: int, fill: str, edge: str | None = None,
                    shadow: str | None = None, base: str = "#ffffff",
                    ss: int = 4) -> bytes:
    """渲染一张带 alpha 的圆角块，返回 RGBA 字节。"""
    big_w, big_h = w * ss, h * ss
    # 透明处先铺底色（alpha=0），缩小平均时才不会把边缘拉黑
    clear = bytes((*hex_rgb(base), 0)) * big_w
    buf = [bytearray(clear) for _ in range(big_h)]

    if shadow:
        fill_rows(buf, ss, w, 0, 1, w, h + 1, r, shadow)
    if edge:
        fill_rows(buf, ss, w, 0, 0, w, h, r, edge)
        fill_rows(buf, ss, w, 1, 1, w - 1, h - 1, max(r - 1, 1), fill)
    else:
        fill_rows(buf, ss, w, 0, 0, w, h, r, fill)

    return _downsample(buf, ss, w, h)


def png_rgba(w: int, h: int, rgba: bytes) -> bytes:
    """把 RGBA 原始数据包成 PNG（真彩色 + alpha）。"""
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def photo(master, png: bytes) -> tk.PhotoImage:
    return tk.PhotoImage(master=master, data=base64.b64encode(png).decode("ascii"))


def icon_png(size: int = 32) -> bytes:
    """窗口/任务栏图标：品牌蓝圆角方块 + 白色 M。

    与头部品牌方块同一套 4 倍超采样渲染；M 是手工定义的 12 点矢量轮廓
    （坐标归一化到 0..1，y 向下），逐超采样点做射线法填充，
    缩小平均后边缘自然抗锯齿，不依赖任何第三方库。
    """
    ss = 4
    big = size * ss
    buf = [bytearray(bytes((*hex_rgb("#ffffff"), 0)) * big) for _ in range(big)]
    fill_rows(buf, ss, size, 0, 0, size, size, round(size * 0.30), ACCENT_PRESS)

    glyph = ((0.00, 0.00), (0.28, 0.00), (0.50, 0.62), (0.72, 0.00), (1.00, 0.00),
             (1.00, 1.00), (0.75, 1.00), (0.75, 0.30), (0.50, 0.88), (0.25, 0.30),
             (0.25, 1.00), (0.00, 1.00))
    bx0, by0 = size * 0.20, size * 0.22       # 字面盒：左右各留 20%、上留 22%
    bw, bh = size * 0.60, size * 0.60
    poly = [(bx0 + px * bw, by0 + py * bh) for px, py in glyph]
    n = len(poly)
    white = bytes((255, 255, 255, 255))
    for by in range(big):
        py = (by + 0.5) / ss
        row = buf[by]
        for bx in range(big):
            px = (bx + 0.5) / ss
            inside = False
            j = n - 1
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[j]
                if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                    inside = not inside
                j = i
            if inside:
                o = bx * 4
                row[o:o + 4] = white

    return png_rgba(size, size, _downsample(buf, ss, size, size))
