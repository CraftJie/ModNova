"""受控控件层：圆角按钮、复选框、状态胶囊。

为什么要有这一层：ttk 是**主题引擎**，不是设计系统。它能改颜色和 padding，
但做不出圆角（`borderwidth=0` 就是矩形），做不出状态胶囊，clam 主题的复选
勾甚至是一道粗 X —— 看着像禁用。而圆角和状态块恰好是这套界面里最重、
出现频率最高的两个形状。

所以这里只做 4 个原语，其余一律不碰：Treeview、Combobox、Text、Scrollbar
继续吃 Tk 原生红利（数据面正是 Tk 最稳的地方）。

两条硬约束：

1. **API 与 ttk 对齐**。支持 `configure(text=/state=)`、`cget("text")`、
   `invoke()`、`pack/grid/place`，这样 120 项回归里那些
   `app.connect_btn.cget("text")` 的断言不用改。
2. **底色必须传对**。圆角靠 alpha 与父容器底色混合，底色猜错就会在四个角
   留下一圈异色边。`_bg_of()` 负责推断，推断不出来时退回卡片色。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from . import ui_theme as T
from .ui_render import mix, png_rgba, render_rounded, render_triangle

# 圆角图是按 (宽, 高, 半径, 填充, 底色) 生成的，同一个尺寸反复生成要跑一遍
# 4 倍超采样，几十毫秒就没了。缓存下来：同尺寸同色的按钮共用一张图。
_IMG_CACHE: dict[tuple, tk.PhotoImage] = {}
_IMG_LIMIT = 240


def _font_of(spec) -> tkfont.Font:
    return tkfont.Font(font=spec)


def _bg_of(widget) -> str:
    """推断某个控件的实际底色 —— 圆角外侧要铺这个色，猜错就有异色边。"""
    try:
        bg = widget.cget("background")
        if bg:
            return str(bg)
    except tk.TclError:
        pass
    try:
        name = widget.cget("style") or widget.winfo_class()
        bg = ttk.Style(widget).lookup(name, "background")
        if bg:
            return str(bg)
    except tk.TclError:
        pass
    return T.CARD


def _apply_config(widget, cnf, kw) -> None:
    """把 ttk 风格的 configure 参数应用到 Canvas。

    不走 `Misc.configure`：它对 `cnf=None` + 关键字参数的组合会走
    `_cnfmerge`，在那里把 None 和 dict 摊平成一个奇怪的列表，最后把值本身
    当成 Tcl 命令名传下去（`invalid command name "56"`）。这里直接合并成
    一个 dict，逐项走底层 configure，行为可预期。
    """
    merged: dict = {}
    if isinstance(cnf, dict):
        merged.update(cnf)
    merged.update(kw)
    for key, value in merged.items():
        widget.tk.call(widget._w, "configure", "-" + str(key), value)


def rounded_bg(master, w: int, h: int, r: int, fill: str, base: str | None = None,
               corners: str = "1111"):
    """取一张 w×h 的圆角图（带缓存）。corners 见 render_rounded。"""
    base = base or _bg_of(master)
    key = (w, h, r, fill, base, corners)
    img = _IMG_CACHE.get(key)
    if img is None:
        if len(_IMG_CACHE) >= _IMG_LIMIT:
            _IMG_CACHE.clear()
        png = png_rgba(w, h, render_rounded(w, h, r, fill, base=base, corners=corners))
        img = tk.PhotoImage(master=master, data=_b64(png))
        _IMG_CACHE[key] = img
    return img


def _b64(png: bytes) -> str:
    import base64
    return base64.b64encode(png).decode("ascii")


def tri_bg(master, w: int, h: int, color: str, base: str | None = None):
    """取一张 w×h 的实心三角图（带缓存）。"""
    base = base or _bg_of(master)
    key = ("tri", w, h, color, base)
    img = _IMG_CACHE.get(key)
    if img is None:
        if len(_IMG_CACHE) >= _IMG_LIMIT:
            _IMG_CACHE.clear()
        img = tk.PhotoImage(master=master,
                            data=_b64(png_rgba(w, h, render_triangle(w, h, color, base))))
        _IMG_CACHE[key] = img
    return img


# ---------------------------------------------------------------------------
# 按钮
# ---------------------------------------------------------------------------

# 变体 → (常态底, 悬停底, 按下底, 文字色)
_BUTTON_TONES = {
    "primary":   (T.ACCENT, T.ACCENT_HOVER, T.ACCENT_PRESS, "#ffffff"),
    "secondary": (T.FIELD_BG, T.HOVER_BG, "#dfe4ec", T.TEXT),
    "danger":    (T.ERR_SOFT, "#fce4e4", "#fbdada", T.ERR),
    "ghost":     (None, T.HOVER_BG, "#e2e7ee", T.MUTED),
}

_STYLE_TO_VARIANT = {
    "Accent.TButton": "primary",
    "Danger.TButton": "danger",
    "Ghost.TButton": "ghost",
}

# 按钮右侧「可展开」三角的尺寸
CHEV_W, CHEV_H, CHEV_GAP = 6, 4, 6
# 分裂按钮右侧那一格的宽度（只放一个 ▾）
SPLIT_W = 26


class RoundedButton(tk.Canvas):
    """真圆角按钮。

    `style` 沿用 ttk 的命名（Accent.TButton / Danger.TButton / Ghost.TButton），
    这样把 `ttk.Button(` 换成 `RoundedButton(` 就能直接跑，不必逐处改写风格名。

    传了 `split_command` 就是「分裂按钮」：左边主区执行 command，右侧一道
    细分隔线隔出 SPLIT_W 宽的窄条，只负责 command=split_command（顶栏的
    连接键就是这样：主区直接连，窄条只展开参数）。两块热区各算各的，
    但共用同一张圆角底、同一套悬停色 —— 视觉上是一个按钮，行为上是两个。
    """

    def __init__(self, master, text: str = "", style: str = "TButton",
                 command=None, width=None, height: int = T.CTRL_H,
                 padx: int = T.SP_XL, bg: str | None = None,
                 min_width: int = 0, chevron: bool = False,
                 split_command=None, **kw):
        self._text = text
        self._variant = _STYLE_TO_VARIANT.get(style, "secondary")
        self._command = command
        self._height = height
        self._padx = padx
        self._base = bg or _bg_of(master)
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._min_chars = width
        # chevron=True 在文字右侧画一个小三角：「点下去是展开，不是执行」。
        # 它是画出来的装饰、不进 text —— 否则 cget("text") 会带上符号，
        # 满屏按文案做的断言全得跟着改。
        self._chevron = chevron
        self._split_cmd = split_command
        # 指针此刻落在哪个热区："main" / "split"；按下和松开必须以同一个
        # 热区为准，否则「在主区按下、拖到 ▾ 上松开」会误触发展开
        self._zone = "main"
        self._press_zone = "main"
        # 工具条上同一排按钮必须等宽：按字数自适应会出现 80px 和 56px
        # 并排，看起来像两组不同的东西。min_width 把它们拉到同一条线上。
        self._min_width = min_width

        super().__init__(master, height=height, bg=self._base,
                         highlightthickness=0, bd=0, takefocus=1,
                         cursor="hand2", **kw)

        self._font = _font_of(T.UI_FONT)
        self._cw = self._natural_width()
        self.configure(width=self._cw)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Motion>", self._on_motion)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self._redraw()

    # -- 尺寸 ---------------------------------------------------------

    def _natural_width(self) -> int:
        text_w = self._font.measure(self._text) if self._text else 0
        if self._split_cmd is not None:
            return max(text_w + self._padx * 2 + SPLIT_W, self._min_width)
        extra = CHEV_W + CHEV_GAP if self._chevron else 0
        return max(text_w + extra + self._padx * 2, self._min_width)

    def _on_configure(self, event) -> None:
        # pack(fill="x") 会拉伸画布，但 Canvas 不会自己重画内容，必须手动跟。
        if event.width != self._cw and event.width > 1:
            self._cw = event.width
            self._redraw()

    # -- 绘制 ---------------------------------------------------------

    def _tone(self):
        normal, hover, press, fg = _BUTTON_TONES[self._variant]
        if self._state == "disabled":
            return T.DISABLED_BG, T.DISABLED_FG
        if self._pressed:
            return press, fg
        if self._hover:
            return hover, fg
        return normal, fg

    def _redraw(self) -> None:
        self.delete("all")
        fill, fg = self._tone()
        w, h = max(self._cw, 8), self._height
        if fill:                      # ghost 不画底，直接落在父容器上
            self.create_image(0, 0, image=rounded_bg(self, w, h, T.R_MD, fill, self._base),
                              anchor="nw")
        if self._split_cmd is not None:
            self._draw_split(w, h, fill, fg)
        elif self._text:
            if self._chevron:
                tw = self._font.measure(self._text)
                total = tw + CHEV_W + CHEV_GAP
                x0 = (w - total) / 2
                self.create_text(x0, h / 2 + 1, text=self._text, fill=fg,
                                 font=self._font, anchor="w")
                self.create_image(x0 + tw + CHEV_GAP, (h - CHEV_H) / 2 + 1,
                                  image=tri_bg(self, CHEV_W, CHEV_H, fg,
                                               fill or self._base),
                                  anchor="nw")
            else:
                self.create_text(w / 2, h / 2 + 1, text=self._text, fill=fg,
                                 font=self._font, anchor="center")
        if self._pressed:
            self.configure(cursor="hand2")
        self.configure(cursor="arrow" if self._state == "disabled" else "hand2")

    def _draw_split(self, w: int, h: int, fill: str | None, fg: str) -> None:
        """主区文字 + 一道分隔线 + 右侧窄条里的 ▾。

        窄条单独变亮（整块一起亮的话，人分不清「我要连」和「我要改参数」
        是两次不同的点击）；▾ 自己也画成图，4px 高的三角用 create_polygon
        画出来就是阶梯斜边。
        """
        zone = min(SPLIT_W, max(1, w // 2))
        x0 = w - zone
        if fill and self._state != "disabled" and self._zone == "split":
            hot = mix(fill, fg, 0.22 if self._pressed else 0.13)
            # 只圆右上/右下（"左上 右上 右下 左下" 里只有后两位是 1）：
            # 左边贴着主区必须是直的，右边要跟着胶囊一起圆
            self.create_image(x0, 0, anchor="nw",
                              image=rounded_bg(self, zone, h, T.R_MD, hot, fill, "0110"))
        if fill:
            self.create_line(x0 + 0.5, 7, x0 + 0.5, h - 7,
                             fill=mix(fill, fg, 0.30))
        if self._text:
            self.create_text(x0 / 2, h / 2 + 1, text=self._text, fill=fg,
                             font=self._font, anchor="center")
        self.create_image(x0 + (zone - CHEV_W) / 2, (h - CHEV_H) / 2 + 1,
                          image=tri_bg(self, CHEV_W, CHEV_H, fg, fill or self._base),
                          anchor="nw")

    # -- 事件 ---------------------------------------------------------

    def _zone_of(self, event) -> str:
        """指针落在哪个热区。没有 split_command 的按钮永远只有 main。"""
        if self._split_cmd is None or event is None:
            return "main"
        zone = min(SPLIT_W, max(1, self._cw // 2))
        return "split" if event.x >= self._cw - zone else "main"

    def _on_enter(self, event=None):
        if self._state == "disabled":
            return
        self._hover = True
        self._zone = self._zone_of(event)
        self._redraw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._pressed = False
        self._zone = "main"
        self._redraw()

    def _on_motion(self, event=None):
        if self._state == "disabled":
            return
        zone = self._zone_of(event)
        if zone != self._zone:
            self._zone = zone
            self._redraw()

    def _on_press(self, event=None):
        if self._state == "disabled":
            return
        self._press_zone = self._zone_of(event)
        self._zone = self._press_zone
        self._pressed = True
        self._redraw()

    def _on_release(self, event=None):
        if self._state == "disabled":
            return
        was = self._pressed
        self._pressed = False
        if event is not None:
            self._zone = self._zone_of(event)
        self._redraw()
        # 按下后把指针移出再松开，不应该触发 —— 和 ttk 的行为一致
        if was and event is not None:
            if 0 <= event.x <= self._cw and 0 <= event.y <= self._height:
                # 在哪个热区按下就必须在哪个热区松开：从主区拖到 ▾ 上
                # 松手不能变成「展开参数」
                if self._zone != self._press_zone:
                    return
                if self._zone == "split" and self._split_cmd is not None:
                    self._split_cmd()
                else:
                    self.invoke()

    def invoke(self):
        if self._state != "disabled" and self._command:
            return self._command()
        return None

    def winfo_class(self):
        """对外仍报 TButton。

        它就是一个按钮：Tk 侧的真实类名由 Tcl 命令决定（Canvas），不受这里
        影响，事件绑定照常；但「按类名找按钮」的代码（布局回归里筛「数据定义」
        那一条）需要它还叫 TButton。
        """
        return "TButton"

    # -- ttk 兼容 -----------------------------------------------------

    def configure(self, cnf=None, **kw):
        rerender = False
        if "text" in kw:
            self._text = kw.pop("text")
            self._cw = self._natural_width()
            _apply_config(self, None, {"width": self._cw})
            rerender = True
        if "state" in kw:
            self._state = kw.pop("state")
            rerender = True
        if "chevron" in kw:
            # 「连接 ▾」连上之后变「断开」：断开是直接动作、没有展开，
            # 箭头留着就是在骗人
            self._chevron = bool(kw.pop("chevron"))
            self._cw = self._natural_width()
            _apply_config(self, None, {"width": self._cw})
            rerender = True
        if "command" in kw:
            self._command = kw.pop("command")
        if "style" in kw:
            # 运行期换风格（「启动从站」→「停止从站」要转成危险色）
            self._variant = _STYLE_TO_VARIANT.get(kw.pop("style"), "secondary")
            rerender = True
        _apply_config(self, cnf, kw)
        if rerender:
            self._redraw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


# 让 ttk.Button / ttk.Checkbutton 的调用点可以整体替换而不报错
Button = RoundedButton


# ---------------------------------------------------------------------------
# 复选框
# ---------------------------------------------------------------------------

class RoundCheck(tk.Canvas):
    """圆角复选框：勾用两段折线画，不用 clam 那个粗 X。"""

    BOX = 16

    def __init__(self, master, text: str = "", variable: tk.BooleanVar | None = None,
                 command=None, bg: str | None = None, height: int = 24, **kw):
        self._base = bg or _bg_of(master)
        self._var = variable
        self._command = command
        self._text = text
        self._hover = False
        super().__init__(master, height=height, bg=self._base,
                         highlightthickness=0, bd=0, cursor="hand2", **kw)
        self._font = _font_of(T.UI_FONT)
        text_w = self._font.measure(text) if text else 0
        self._cw = self.BOX + (T.SP_M + text_w if text else 0)
        self.configure(width=self._cw)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    def _on_enter(self, _e=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._redraw()

    def _checked(self) -> bool:
        return bool(self._var.get()) if self._var is not None else False

    def _toggle(self, _e=None):
        if self._var is not None:
            self._var.set(not self._var.get())
        self._redraw()
        if self._command:
            self._command()

    def _redraw(self):
        self.delete("all")
        on = self._checked()
        h = int(self.cget("height")) if str(self.cget("height")).isdigit() else 24
        top = (h - self.BOX) / 2
        if on:
            self.create_image(0, top, anchor="nw",
                              image=rounded_bg(self, self.BOX, self.BOX, T.R_SM,
                                               T.ACCENT, self._base))
            # 勾：两段折线，比 clam 的 X 更像「选中」
            self.create_line(4, top + 9, 7, top + 12, 12, top + 5,
                             fill="#ffffff", width=2, capstyle="round",
                             joinstyle="round")
        else:
            edge = T.BORDER if self._hover else T.DISABLED_ARROW
            self.create_image(0, top, anchor="nw",
                              image=rounded_bg(self, self.BOX, self.BOX, T.R_SM,
                                               "#ffffff", self._base))
            self.create_rectangle(0.5, top + 0.5, self.BOX - 0.5, top + self.BOX - 0.5,
                                  outline=edge, width=1)
        if self._text:
            self.create_text(self.BOX + T.SP_M, h / 2, text=self._text,
                             fill=T.TEXT, font=self._font, anchor="w")

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
        if "variable" in kw:
            self._var = kw.pop("variable")
        _apply_config(self, cnf, kw)
        self._redraw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)


Checkbutton = RoundCheck


# ---------------------------------------------------------------------------
# 状态胶囊
# ---------------------------------------------------------------------------

# 语义色 → (底, 字, 点)
# 胶囊内圆点的几何：左内边距、直径、与文字的间隙
DOT_X, DOT_D, DOT_GAP = 11, 9, 7

_PILL_TONES = {
    "ok":   (T.OK_SOFT, T.OK, T.OK),
    "warn": (T.WARN_SOFT, T.WARN, T.WARN),
    "err":  (T.ERR_SOFT, T.ERR, T.ERR),
    "idle": (T.SUNKEN, T.MUTED, T.DISABLED_FG),
}

_FG_TO_TONE = {
    T.OK: "ok", T.WARN: "warn", T.ERR: "err", T.MUTED: "idle",
}


class StatusPill(tk.Canvas):
    """链路状态胶囊：一个圆点 + 一段文字 + 一层淡色底。

    原来那一串「● 在线 · TCP 127.0.0.1:3678 · 站 1」是 9pt 灰字整行染绿，
    把「谁在线」和「地址是什么」混成一句话。现在：状态是点，事实是字，
    底色只在异常时出现 —— 正常态不抢视线。
    """

    def __init__(self, master, text: str = "", tone: str = "idle",
                 bg: str | None = None, height: int = T.CTRL_H, max_width: int = 460):
        self._base = bg or _bg_of(master)
        self._text = text
        self._tone = tone
        self._max = max_width
        self._h = height
        super().__init__(master, height=height, bg=self._base,
                         highlightthickness=0, bd=0)
        self._font = _font_of(T.UI_FONT)
        self._redraw()

    def _redraw(self):
        self.delete("all")
        # 画布给足 30px 与按钮同高，胶囊本身 26px 居中 —— 这样在顶栏里
        # 状态胶囊和「断开」按钮的上边缘是对齐的
        pill = 26
        off = (self._h - pill) / 2
        text = self._text or ""
        # 链接信息可能很长，截断后再测量，避免胶囊把窗口撑宽
        avail = self._max - 34
        while text and self._font.measure(text) > avail:
            text = text[:-2] + "…"
        fill, fg, dot = _PILL_TONES.get(self._tone, _PILL_TONES["idle"])
        tw = self._font.measure(text)
        w = DOT_X + DOT_D + DOT_GAP + tw + T.SP_M
        self.tk.call(self._w, "configure", "-width", w)
        self.create_image(0, off, anchor="nw",
                          image=rounded_bg(self, w, pill, pill // 2, fill, self._base))
        cy = off + pill / 2
        # 圆点必须是**图**，不能用 create_oval：Tk 画椭圆不做抗锯齿，
        # 9px 的圆放大看就是一圈台阶（现场 100% 缩放时同样能看出八边形）。
        # 走 render_rounded 的 4× 超采样，r 取半个直径就是正圆。
        self.create_image(DOT_X, cy, anchor="w",
                          image=rounded_bg(self, DOT_D, DOT_D,
                                           DOT_D // 2 + 1, dot, self._base))
        self.create_text(DOT_X + DOT_D + DOT_GAP, cy + 1, text=text,
                         fill=fg, font=self._font, anchor="w")

    def set(self, text: str, tone: str = "idle"):
        self._text = text
        self._tone = tone
        self._redraw()

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
        if "foreground" in kw:
            fg = kw.pop("foreground")
            self._tone = _FG_TO_TONE.get(str(fg), self._tone)
        _apply_config(self, cnf, kw)
        self._redraw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)


class Badge(tk.Canvas):
    """数字徽标：页签上的运行计数。"""

    def __init__(self, master, text: str = "", bg: str | None = None,
                 height: int = 18):
        self._base = bg or _bg_of(master)
        self._text = text
        super().__init__(master, height=height, bg=self._base,
                         highlightthickness=0, bd=0)
        self._font = _font_of(T.FONT_SM)
        self._redraw()

    def _redraw(self):
        self.delete("all")
        tw = self._font.measure(self._text)
        w = max(18, tw + 12)
        h = 18
        self.tk.call(self._w, "configure", "-width", w)
        self.create_image(0, 0, anchor="nw",
                          image=rounded_bg(self, w, h, h // 2, T.ACCENT, self._base))
        self.create_text(w / 2, h / 2, text=self._text, fill="#ffffff",
                         font=self._font, anchor="center")

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._text = str(kw.pop("text"))
        _apply_config(self, cnf, kw)
        self._redraw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)
