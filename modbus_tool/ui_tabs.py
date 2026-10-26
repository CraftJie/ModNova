"""两段式切换条（分段控件）。

为什么不用 ttk.Notebook：clam 主题的页签是一层层带斜面的方框，配色怎么调都
留有 90 年代的立体感；而且切换时整块页签瞬间位移，视觉上就是「啪」地跳一下。
这里自己画：凹槽里放一个白色药丸，切换时药丸缓出滑到目标段，文字颜色同时
逐帧插值，内容立刻换掉 —— 眼睛跟着动的东西走，就不会觉得突兀。

独立成模块：这是一个自包含的控件，和 Modbus 业务零耦合。
"""

from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont

from .ui_render import mix, photo, png_rgba, render_rounded
from .ui_theme import (
    ACCENT,
    BG,
    CARD,
    OK,
    PILL_EDGE,
    PILL_SHADOW,
    TAB_HOVER,
    TAB_IDLE,
    TEXT,
    TRACK,
)


class SegmentedTabs(tk.Canvas):
    """两段式切换条：圆角药丸指示器 + 横向滑动。

    为什么不用 ttk.Notebook：clam 主题的页签是一层层带斜面的方框，配色怎么调都
    留有 90 年代的立体感；而且切换时整块页签瞬间位移，视觉上就是「啪」地跳一下。
    这里自己画：凹槽里放一个白色药丸，切换时药丸缓出滑到目标段，
    文字颜色同时逐帧插值，内容立刻换掉 —— 眼睛跟着动的东西走，就不会觉得突兀。

    键盘：获得焦点后 ←/→ 切换；应用层另外挂了 Ctrl+1 / Ctrl+2 / F6。
    """

    PILL_INSET = 3      # 药丸与凹槽边缘的间距
    TEXT_PAD = 25       # 文字左右留白
    MIN_SEG = 104       # 单段最小宽度
    ANIM_MS = 130       # 再长就会看清白边从字中间扫过

    def __init__(self, master, labels, on_change=None, badge_slots=(), **kw):
        scale = master.winfo_fpixels("1i") / 96.0
        self._s = max(0.8, min(scale, 2.5))
        self._height = round(kw.pop("height", 34) * self._s)

        kw.setdefault("bg", BG)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("takefocus", 1)
        kw.setdefault("cursor", "hand2")
        kw.setdefault("height", self._height)
        super().__init__(master, **kw)

        self._font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self._labels = list(labels)
        self._badge_slots = set(badge_slots)
        self._on_change = on_change
        self._index = 0
        self._hover: int | None = None
        self._focused = False
        self._anim_id: str | None = None
        self._badges: dict[int, bool] = {i: False for i in self._badge_slots}
        self._dot_cache: dict[int, tk.PhotoImage] = {}
        self._dot_d = max(6, round(6 * self._s))

        self._layout_segments()
        self._draw()

        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Left>", lambda e: self.select(self._index - 1))
        self.bind("<Right>", lambda e: self.select(self._index + 1))

    # ---------------------------------------------------------------- 几何

    def _layout_segments(self) -> None:
        """等宽分段。

        两段宽度不一致的话，药丸滑动途中宽度也得跟着变，中间帧会糊成一团；
        等宽之后药丸只做纯平移，动起来干净，视觉节奏也稳。
        """
        s = self._s
        inset = round(self.PILL_INSET * s)
        width = max([round(self.MIN_SEG * s)] +
                    [self._font.measure(label) + 2 * round(self.TEXT_PAD * s)
                     for label in self._labels])
        self._seg_w = [width] * len(self._labels)
        self._seg_x = [inset + width * i for i in range(len(self._labels))]
        self.configure(width=inset * 2 + width * len(self._labels))

    def _text_center(self, i: int) -> float:
        return self._seg_x[i] + self._seg_w[i] / 2

    # ---------------------------------------------------------------- 绘制

    def _round_points(self, x1, y1, x2, y2, r, step: float = 4.0) -> list[float]:
        """把圆角矩形采样成一条闭合路径的点列。

        以前是用「两个矩形 + 四个椭圆」拼，直边和圆弧各自独立栅格化，
        1px 描边在接缝处对不齐，拐角会出现 2~3px 的台阶和一个缺口。
        改成一条连续路径后，描边只按这一条路径画，台阶降到 1px，接缝也顺了。
        """
        r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        points: list[float] = []
        corners = (
            (x2 - r, y1 + r, -90, 0),      # 右上
            (x2 - r, y2 - r, 0, 90),       # 右下
            (x1 + r, y2 - r, 90, 180),     # 左下
            (x1 + r, y1 + r, 180, 270),    # 左上
        )
        for cx, cy, start, end in corners:
            count = max(2, int(abs(end - start) / step))
            for i in range(count + 1):
                rad = math.radians(start + (end - start) * i / count)
                points.append(cx + r * math.cos(rad))
                points.append(cy + r * math.sin(rad))
        return points

    def _draw(self) -> None:
        s = self._s
        h = self._height
        inset = round(self.PILL_INSET * s)
        radius = round(10 * s)
        total = max(self._seg_x[-1] + self._seg_w[-1] + inset, 1)
        pill_w = self._seg_w[0]
        pill_h = h - inset * 2

        self._bitmap = True
        try:
            self._track_img = photo(self, png_rgba(
                total, h, render_rounded(total, h, radius, TRACK, base=BG)))
            # 药丸的透明区紧贴轨道，底色按 TRACK 铺，边缘过渡才不会发白
            self._pill_imgs = {
                "normal": photo(self, png_rgba(pill_w, pill_h + 1, render_rounded(
                    pill_w, pill_h, radius, CARD, PILL_EDGE, PILL_SHADOW, base=TRACK))),
                "focus": photo(self, png_rgba(pill_w, pill_h + 1, render_rounded(
                    pill_w, pill_h, radius, CARD, ACCENT, PILL_SHADOW, base=TRACK))),
            }
        except Exception:                      # 极老的 Tk 没有 PNG，退回矢量画法
            self._bitmap = False

        if self._bitmap:
            self.create_image(0, 0, image=self._track_img, anchor="nw")
            self._pill = self.create_image(0, 0, image=self._pill_imgs["normal"], anchor="nw")
        else:
            self.create_polygon(self._round_points(0, 0, total, h, radius),
                                fill=TRACK, outline="")
            self._pill = self.create_polygon(
                self._round_points(0, inset, pill_w, h - inset, radius),
                fill=CARD, outline=PILL_EDGE, width=1, joinstyle="round")

        self._texts = [
            self.create_text(self._text_center(i), h / 2, text=label, font=self._font,
                             fill=TAB_IDLE, anchor="center")
            for i, label in enumerate(self._labels)
        ]
        self._dots = {
            i: self.create_image(0, 0, anchor="center", state="hidden",
                                 image=self._dot_image())
            for i in self._badge_slots
        }
        self._place_pill(self._seg_x[0], self._seg_w[0])
        self._paint_texts()

    def _place_pill(self, cx: float, cw: float) -> None:
        s = self._s
        inset = round(self.PILL_INSET * s)
        h = self._height
        radius = round(10 * s)
        self._pill_box = (cx, cw)
        if self._bitmap:
            self.coords(self._pill, cx, inset)      # 位图宽度固定，只挪位置
        else:
            self.coords(self._pill, *self._round_points(cx, inset, cx + cw, h - inset, radius))

    def _paint_texts(self, old: int | None = None, t: float = 1.0) -> None:
        """上色。动画期间传 (旧下标, 进度)，两个标签的颜色连续过渡。

        不在动画中点一次性切色：那一帧颜色突变，加上正在变化的底色，
        看着就是文字抖了一下。逐帧插值虽然每一帧都要重绘，但变化是连续的。
        """
        for i, item in enumerate(self._texts):
            if old is not None and i == old:
                color = mix(TEXT, TAB_IDLE, t)        # 旧页签：由深变浅
            elif old is not None and i == self._index:
                color = mix(TAB_IDLE, TEXT, t)        # 新页签：由浅变深
            elif i == self._index:
                color = TEXT
            elif i == self._hover:
                color = TAB_HOVER
            else:
                color = TAB_IDLE
            self.itemconfigure(item, fill=color)

        if old is not None:
            return                                     # 动画中药丸图不用动
        # 键盘焦点环。鼠标点击不抢焦点，所以平时不会出现这圈蓝色。
        if self._bitmap:
            self.itemconfigure(self._pill,
                               image=self._pill_imgs["focus" if self._focused else "normal"])
        else:
            self.itemconfigure(self._pill, outline=ACCENT if self._focused else PILL_EDGE)

    # ---------------------------------------------------------------- 交互

    def _hit(self, x: float) -> int | None:
        for i in range(len(self._labels)):
            if self._seg_x[i] <= x <= self._seg_x[i] + self._seg_w[i]:
                return i
        return None

    def _on_click(self, event) -> None:
        # 点了不抢键盘焦点：抢过来的话焦点环会一直挂着，鼠标用户看着像
        # 「这个按钮怎么一直是选中态」。要键盘操作就从 Tab 进来。
        index = self._hit(event.x)
        if index is not None:
            self.select(index)

    def _on_motion(self, event) -> None:
        index = self._hit(event.x)
        if index != self._hover:
            self._hover = index
            self._paint_texts()

    def _on_leave(self, _event) -> None:
        if self._hover is not None:
            self._hover = None
            self._paint_texts()

    def _on_focus_in(self, _event) -> None:
        self._focused = True
        self._paint_texts()

    def _on_focus_out(self, _event) -> None:
        self._focused = False
        self._paint_texts()

    def select(self, index: int, animate: bool = True) -> None:
        index = max(0, min(len(self._labels) - 1, index))
        if index == self._index:
            return
        old = self._index
        self._index = index
        # 内容立刻换，指示器再滑过去
        if self._on_change is not None:
            self._on_change(index)

        target_x, target_w = self._seg_x[index], self._seg_w[index]
        if not animate:
            self._stop_anim()
            self._place_pill(target_x, target_w)
            self._paint_texts()
            return
        self._animate_to(target_x, target_w, old)

    @property
    def active_index(self) -> int:
        """当前选中段。

        刻意不叫 `index`：Canvas 自己有个 `index()` 方法（把 item 标识换成
        序号），同名属性会把它整个盖掉 —— 现在没人调，但那是留给未来的坑。
        """
        return self._index

    # ---------------------------------------------------------------- 动画

    def _stop_anim(self) -> None:
        if self._anim_id is not None:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _animate_to(self, target_x: float, target_w: float, old: int) -> None:
        """药丸从旧段滑到新段，同时文字颜色连续过渡。

        试过让药丸在两个分段间淡入淡出，结果中间有几帧「两边都发白」，
        看着像两个都选中了 —— 还是滑动最清楚。
        """
        self._stop_anim()
        # 从当前药丸位置接着动，连点两下不会跳回原点重来
        start_x, start_w = getattr(self, "_pill_box", (target_x, target_w))
        steps = max(1, int(self.ANIM_MS / 16))
        state = {"step": 0}

        def tick() -> None:
            state["step"] += 1
            t = min(1.0, state["step"] / steps)
            k = 1 - (1 - t) ** 3                      # 缓出：快起慢收
            self._place_pill(start_x + (target_x - start_x) * k,
                             start_w + (target_w - start_w) * k)
            self._paint_texts(old, k)                 # 文字颜色逐帧插值，不在中途跳变
            if t < 1:
                self._anim_id = self.after(16, tick)
            else:
                self._anim_id = None
                self._place_pill(target_x, target_w)
                self._paint_texts()

        self._anim_id = self.after(16, tick)

    # ---------------------------------------------------------------- 状态灯

    def _dot_image(self):
        """状态灯小圆点。用 4× 超采样图而不是 create_oval ——
        Tk 的椭圆不做抗锯齿，6px 的圆点放大就是一个方块。"""
        key = self._dot_d
        img = self._dot_cache.get(key)
        if img is None:
            img = photo(self, png_rgba(key, key, render_rounded(
                key, key, key // 2 + 1, OK, base=CARD)))
            self._dot_cache[key] = img
        return img

    def set_badge(self, index: int, active: bool) -> None:
        """在指定段右侧点亮一个小圆点（用于「从站正在运行」这类后台状态）。"""
        if index not in self._dots:
            return
        self._badges[index] = bool(active)
        item = self._dots[index]
        if not active:
            self.itemconfigure(item, state="hidden")
            return
        s = self._s
        # 落在段内右侧的留白里，不进文字区，所以段落宽度和文字居中都不用为它让位
        cx = self._seg_x[index] + self._seg_w[index] - round(10 * s)
        cy = self._height / 2
        self.coords(item, cx, cy)
        self.itemconfigure(item, state="normal")

    def badge(self, index: int) -> bool:
        return self._badges.get(index, False)
