"""Tkinter 桌面界面。

两条硬规矩：

1. 所有网络/串口操作都在工作线程里跑，主线程只碰控件。工作线程把结果塞进
   queue，主线程用 after() 定期取 —— 这是 Tkinter 唯一安全的跨线程方式。
2. 单工作线程 + 串行任务。Modbus 本身就是一问一答，并发的意义不大，
   串行反而避免了请求交叉。

界面上半部分是主站（读别人），下半部分日志，第二个页签是从站（被别人读）。
现场调试时两个页签可以同时开着做自环测试。
"""

from __future__ import annotations

import base64
import collections
import math
import queue
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from .addressing import AddressError, area_of_fc, display_address, resolve_address
from .client import ModbusClient
from .codec import (
    NONPRINT,
    as_signed16,
    bin_of,
    decode,
    encode,
    format_value,
    hex_of,
    is_integer,
    patch_ascii,
    span,
)
from .datastore import DataStore
from .parsing import normalize_number as _normalize_number
from .parsing import parse_int as _parse_int
from .parsing import parse_write_values
from .pdu import describe_error, describe_write_frame, read_limit, write_limit
from .server import RtuServer, TcpServer
from .transport import HAS_SERIAL, RtuTransport, TcpTransport, TransportError, list_serial_ports
from .ui_render import icon_png, photo, png_rgba, render_rounded
from .ui_tabs import SegmentedTabs
from .ui_theme import (
    ACCENT,
    ACCENT_PRESS,
    BG,
    BORDER,
    CARD,
    DISABLED_ARROW,
    DISABLED_BG,
    DISABLED_FG,
    ERR,
    FIELD_BG,
    HEAD_BG,
    HOVER_BG,
    LOG_FONT,
    MONO,
    MUTED,
    OK,
    SELECT_BG,
    TEXT,
    UI_FONT,
    WARN,
)

# 全角归一化、整数解析、写值切分都在 parsing.py —— 命令行用的是同一份，
# 不然「界面能填 －5、命令行说无效」这种漂移迟早出现。

FC_OPTIONS = [
    ("01  读线圈", 1),
    ("02  读离散输入", 2),
    ("03  读保持寄存器", 3),
    ("04  读输入寄存器", 4),
    ("05  写单线圈", 5),
    ("06  写单寄存器", 6),
    ("15  写多线圈", 15),
    ("16  写多寄存器", 16),
]

AREA_OPTIONS = [
    ("线圈 (可读写·位)", "coils"),
    ("离散输入 (只读·位)", "discrete_inputs"),
    ("保持寄存器 (可读写)", "holding_registers"),
    ("输入寄存器 (只读)", "input_registers"),
]

# 写功能码 → 对应的读功能码，用于写完之后回读校验
READBACK_FC = {5: 1, 6: 3, 15: 1, 16: 3}

DTYPE_SHORT = {
    "uint16": "u16", "int16": "i16", "uint32": "u32", "int32": "i32",
    "float32": "f32", "uint64": "u64", "int64": "i64", "float64": "f64",
    "ascii": "ASCII",
}

DTYPE_OPTIONS = [
    ("无符号 16 位", "uint16"),
    ("有符号 16 位", "int16"),
    ("无符号 32 位", "uint32"),
    ("有符号 32 位", "int32"),
    ("单精度浮点", "float32"),
    ("无符号 64 位", "uint64"),
    ("有符号 64 位", "int64"),
    ("双精度浮点", "float64"),
    # 放在最后：前 8 项的索引不要动，测试和工作流里都有按下标取值的地方
    ("ASCII 字符", "ascii"),
]

class ModbusApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ModNova")
        # 标题栏/任务栏图标：M 品牌方块。PhotoImage 挂在 self 上防止被回收。
        self._icon_img = tk.PhotoImage(
            master=root, data=base64.b64encode(icon_png()).decode("ascii"))
        self.root.iconphoto(True, self._icon_img)
        self.root.geometry("1200x820")
        # 主站页现在是一条工具条 + 满幅表格，低于这个尺寸工具条会挤，
        # 但数据区仍然保证 20 行以上。
        self.root.minsize(1040, 680)
        self.root.configure(bg=BG)

        self.client: ModbusClient | None = None
        self.server = None
        # -- 双视图状态：True = Modbus Poll 式网格（默认），False = 逐行明细 --
        self._master_grid = True
        self._slave_grid = True
        self._last_read = None            # 最近一次读到的原始数据，切换视图时重画用
        self._slave_grid_per = 10         # 网格每行格数（随进制变化，双击定位地址用）
        self._slave_grid_pad = 0          # 首行左侧留空几格（行首对齐用）
        self._slave_base = 0              # 网格行首基准（per 的整数倍）
        self._slave_start = 0             # 网格真实起始地址
        self._events: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="modbus-io")
        self._polling = False
        self._poll_failures = 0
        self._busy = False
        self._editing = False
        self._editor: tk.Entry | None = None
        self._demo_counter = 0
        self._log_lines = 0
        self._log_paused = False
        self._log_skipped = 0
        self._slave_sig = None          # 上一次从站表格的内容指纹，用来跳过无变化的刷新
        # -- 链路健康：站号、连续失败次数、状态（on / warn / down）--
        self._link_text = ""            # 例如 "TCP 127.0.0.1:15040"
        self._link_unit = 1
        self._link_health = "off"
        self._fail_streak = 0
        self._conn_note = "未连接"       # 连接失败时的原因，直接显示在顶栏
        # -- 变化高亮：上一帧的显示值，用来判断哪一行变了 --
        self._prev_cells: list[str] | None = None
        self._fade_job = None
        # -- 迷你趋势条：选中单元格的历史值 --
        self._trend: collections.deque = collections.deque(maxlen=120)
        self._trend_key: tuple | None = None
        self._grid_per = 10             # 网格当前每行格数（点击定位要用）
        self._grid_pad = 0              # 首行左侧留空几格（十六进制对齐用）
        self._view_state: dict = {}     # 数据解释的当前值（弹层重建时恢复）
        self._written_span = None       # 刚写过的寄存器范围 (起, 止)，用来框选
        self._written_marks: list = []  # 框选框的 4 条边框
        self._written_job = None        # 框选过期定时器

        self._init_style()
        self._build_ui()
        self._on_link_mode_change()
        self._on_fc_change()
        self._drain_events()
        self._tick_slave_refresh()
        self._tick_demo()

    # ---------------------------------------------------------------- 样式

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        # 下拉列表（Combobox 的 listbox）是原生控件，样式走 option_add
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECT_BG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.root.option_add("*TCombobox*Listbox.font", UI_FONT)

        style.configure(".", background=BG, foreground=TEXT, font=UI_FONT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, borderwidth=1, relief="solid")
        style.configure("Inner.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("AppTitle.TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background=CARD, foreground=MUTED, font=MONO)
        style.configure("Big.TLabel", background=CARD, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10, "bold"))

        style.configure("TSeparator", background=BORDER)

        # 普通按钮：filled 轻拟态 —— 无边框浅灰底，悬停变深一档
        style.configure("TButton", padding=(14, 6), borderwidth=0,
                        background=FIELD_BG, foreground=TEXT)
        style.map("TButton",
                  background=[("disabled", DISABLED_BG),
                              ("pressed", "#e7e9ed"),
                              ("active", HOVER_BG)],
                  foreground=[("disabled", DISABLED_FG)])
        # 主按钮：品牌蓝胶囊（Tk 画不出真圆角，靠纯色 + 无边框保持轻盈）
        style.configure("Accent.TButton", background=ACCENT, foreground="white", borderwidth=0)
        style.map("Accent.TButton",
                  background=[("disabled", "#b7c8fe"),
                              ("pressed", ACCENT_PRESS),
                              ("active", ACCENT_PRESS)],
                  foreground=[("disabled", "#f0f4ff")])
        style.configure("Danger.TButton", background="#fef2f2", foreground=ERR, borderwidth=0)
        style.map("Danger.TButton",
                  background=[("disabled", DISABLED_BG), ("active", "#fee2e2")],
                  foreground=[("disabled", DISABLED_FG)])

        # 输入框：filled 风格 —— 浅底无边框，聚焦时白底 + 品牌蓝细边
        style.configure("TEntry", fieldbackground=FIELD_BG, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=4)
        style.map("TEntry",
                  fieldbackground=[("disabled", DISABLED_BG), ("focus", "#ffffff"),
                                   ("!focus", FIELD_BG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)],
                  foreground=[("disabled", DISABLED_FG)])
        style.configure("TCombobox", fieldbackground=FIELD_BG, background=FIELD_BG,
                        arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=4)
        # 禁用态必须一眼看出来。clam 默认的禁用底色被上面的 fieldbackground 覆盖了，
        # 结果灰掉的下拉框还是白底，看着像能点 —— 用户会反复去点它。
        style.map("TCombobox",
                  fieldbackground=[("disabled", DISABLED_BG), ("!disabled", FIELD_BG)],
                  foreground=[("disabled", DISABLED_FG)],
                  arrowcolor=[("disabled", DISABLED_ARROW)],
                  background=[("disabled", DISABLED_BG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)])
        style.configure("TSpinbox", fieldbackground=FIELD_BG, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED, padding=4)
        style.map("TSpinbox",
                  fieldbackground=[("disabled", DISABLED_BG), ("focus", "#ffffff"),
                                   ("!focus", FIELD_BG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)],
                  arrowcolor=[("disabled", DISABLED_ARROW)],
                  foreground=[("disabled", DISABLED_FG)],
                  background=[("disabled", DISABLED_BG)])

        style.configure("TCheckbutton", background=CARD, focuscolor=CARD)
        style.map("TCheckbutton",
                  background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#ffffff")])
        style.configure("TRadiobutton", background=CARD, focuscolor=CARD)
        style.map("TRadiobutton",
                  background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#ffffff")])

        style.configure("Treeview",
                        background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=26, borderwidth=0, font=("Consolas", 10))
        style.configure("Treeview.Heading",
                        background=HEAD_BG, foreground="#81858c",
                        relief="flat", font=("Microsoft YaHei UI", 9), padding=(2, 3))
        style.map("Treeview", background=[("selected", SELECT_BG)],
                  foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", HEAD_BG)])

        # 网格视图：行更紧凑，一眼能扫更多寄存器（Modbus Poll 的观感）
        style.configure("Grid.Treeview",
                        background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=26, borderwidth=0, font=("Consolas", 10))
        style.configure("Grid.Treeview.Heading",
                        background=HEAD_BG, foreground="#81858c",
                        relief="flat", font=("Microsoft YaHei UI", 9), padding=(2, 3))
        style.map("Grid.Treeview", background=[("selected", SELECT_BG)],
                  foreground=[("selected", TEXT)])
        style.map("Grid.Treeview.Heading", background=[("active", HEAD_BG)])

        # 滚动条：细、灰、无立体感，贴着内容层走
        style.configure("TScrollbar", background=CARD, troughcolor=CARD,
                        bordercolor=CARD, arrowcolor=MUTED, relief="flat")
        style.map("TScrollbar",
                  background=[("active", "#d9dce0"), ("!active", "#e7e9ed")],
                  arrowcolor=[("active", TEXT), ("!active", MUTED)])

    # ---------------------------------------------------------------- 布局

    def _build_ui(self) -> None:
        self._build_header()
        self._build_connect_bar()
        self._build_view_dialog()      # 数据解释：低频，做成弹层

        self.stack = tk.Frame(self.root, bg=BG)
        self.stack.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.stack.rowconfigure(0, weight=1)
        self.stack.columnconfigure(0, weight=1)

        self.tab_master = tk.Frame(self.stack, bg=BG)
        self.tab_slave = tk.Frame(self.stack, bg=BG)
        for frame in (self.tab_master, self.tab_slave):
            frame.grid(row=0, column=0, sticky="nsew")
        self.tab_slave.grid_remove()

        self._build_master_tab()
        self._build_slave_tab()
        self._build_log()              # 覆盖层，默认收起，不占主区高度

        # Ctrl+1 / Ctrl+2 直选，F6 在两页之间来回切。
        # 不用 Ctrl+Tab：Entry、Treeview 这些控件的类绑定把 Tab 绑给了焦点遍历，
        # 事件在类这一层就被 break 掉，到不了全局绑定。
        self.root.bind_all("<Control-Key-1>", lambda e: self.tabs.select(0))
        self.root.bind_all("<Control-Key-2>", lambda e: self.tabs.select(1))
        self.root.bind_all("<F6>", lambda e: self.tabs.select(1 - self.tabs.index))
        # 现场是左手键盘右手鼠标：读取这种高频动作不该只能点按钮
        self.root.bind_all("<F5>", lambda e: self._run_primary())
        self.root.bind_all("<Control-Return>", lambda e: self._run_primary())
        self.root.bind_all("<Escape>", lambda e: self._stop_poll())
        self.root.bind_all("<Control-Key-l>", lambda e: self._clear_log())

    def _run_primary(self) -> str:
        """F5 / Ctrl+Enter：按当前功能码跑一次（读或写）。"""
        if self._current_fc() in (1, 2, 3, 4):
            self._do_read_async()
        else:
            self._do_write_async()
        return "break"

    def _switch_tab(self, index: int) -> None:
        if index == 1:
            self.tab_master.grid_remove()
            self.tab_slave.grid()
            self.tab_slave.tkraise()
            if getattr(self, "_log_open", False):
                self.log_panel.lift()
            # 切过来立刻刷一次。刷新已经是原地更新（数据没变就一行都不动），
            # 不会再出现「先白一下再填满」的闪动，也就没理由让用户看旧值。
            self._refresh_slave_table()
        else:
            self.tab_slave.grid_remove()
            self.tab_master.grid()
            self.tab_master.tkraise()

    def _brand_mark(self, parent) -> tk.Canvas:
        """品牌方块：品牌蓝圆角底 + 白色 M。

        Tk 画布没有抗锯齿，圆角块直接复用切换条那套 4 倍超采样位图渲染，
        边缘和浏览器里 border-radius 的观感一致。
        """
        size = 26
        png = png_rgba(size, size, render_rounded(
            size, size, round(size * 0.32), ACCENT_PRESS, base=BG))
        image = photo(parent, png)
        # PhotoImage 必须挂在控件上防止被垃圾回收
        parent._brand_img = image
        mark = tk.Canvas(parent, width=size, height=size, bg=BG,
                         highlightthickness=0)
        mark.create_image(0, 0, image=image, anchor="nw")
        mark.create_text(size / 2, size / 2 + 0.5, text="M", fill="#ffffff",
                         font=("Segoe UI", 11, "bold"))
        return mark

    def _build_header(self) -> None:
        """顶栏一行装下：品牌、页签、链路状态、连接按钮。

        页签从独立一行挪进来，是为了把高度还给数据区 —— 那一行 34px
        在 780 高的窗口里就是 4.4% 的数据面积。
        """
        header = ttk.Frame(self.root, padding=(12, 6, 12, 6))
        header.pack(fill="x")

        brand = ttk.Frame(header)
        brand.pack(side="left")
        self._brand_mark(brand).pack(side="left", padx=(0, 8))
        ttk.Label(brand, text="ModNova", style="AppTitle.TLabel").pack(side="left")

        self.tabs = SegmentedTabs(
            header,
            labels=["主站 · 读别人", "从站 · 被别人读"],
            on_change=self._switch_tab,
            badge_slots=(1,),
            height=26,
        )
        self.tabs.pack(side="left", padx=(18, 0))

        self.connect_btn = ttk.Button(header, text="连接", style="Accent.TButton",
                                      width=7, command=self._toggle_connection)
        self.connect_btn.pack(side="right")
        self.led = ttk.Label(header, text="●  未连接", style="Sub.TLabel")
        self.led.pack(side="right", padx=(0, 14))

    def _bar_field(self, parent, label: str, widget) -> None:
        ttk.Label(parent, text=label, style="Sub.TLabel").pack(side="left", padx=(0, 4))
        widget.pack(side="left", padx=(0, 10))

    def _build_connect_bar(self) -> None:
        """连接参数只在「没连上」的时候占位置。

        连上之后它收起来，数据区立刻多出 40px —— 现场的使用节奏本来就是
        设置一次、然后长时间盯着数据。
        """
        bar = ttk.Frame(self.root, padding=(12, 5, 12, 6))
        bar.pack(fill="x")
        self.connect_bar = bar

        self.link_mode = tk.StringVar(value="tcp")
        ttk.Radiobutton(bar, text="TCP", value="tcp", variable=self.link_mode,
                        command=self._on_link_mode_change).pack(side="left")
        ttk.Radiobutton(bar, text="RTU", value="rtu", variable=self.link_mode,
                        command=self._on_link_mode_change).pack(side="left", padx=(6, 12))

        self.link_holder = ttk.Frame(bar)
        self.link_holder.pack(side="left")

        self.tcp_frame = ttk.Frame(self.link_holder)
        self.tcp_frame.pack(fill="x")
        self.tcp_host = ttk.Entry(self.tcp_frame, width=14)
        self.tcp_host.insert(0, "127.0.0.1")
        self._bar_field(self.tcp_frame, "主机", self.tcp_host)
        self.tcp_port = ttk.Spinbox(self.tcp_frame, from_=1, to=65535, width=6)
        self.tcp_port.set("502")
        self._bar_field(self.tcp_frame, "端口", self.tcp_port)

        self.rtu_frame = ttk.Frame(self.link_holder)
        ports = list_serial_ports()
        self.rtu_port = ttk.Combobox(self.rtu_frame, values=ports, width=9)
        self.rtu_port.set(ports[0] if ports else ("COM3" if ports == [] else ""))
        self._bar_field(self.rtu_frame, "串口", self.rtu_port)
        self.baud = ttk.Combobox(self.rtu_frame, values=["4800", "9600", "19200", "38400", "57600", "115200"],
                                 width=7, state="readonly")
        self.baud.set("9600")
        self._bar_field(self.rtu_frame, "波特率", self.baud)
        # 老仪表里 7 数据位（7E1）并不罕见，固定 8 位会一直 CRC 错
        self.databits = ttk.Combobox(self.rtu_frame, values=["8", "7", "6", "5"],
                                     width=4, state="readonly")
        self.databits.set("8")
        self._bar_field(self.rtu_frame, "数据位", self.databits)
        self.parity = ttk.Combobox(self.rtu_frame, values=["N", "E", "O", "M", "S"],
                                   width=4, state="readonly")
        self.parity.set("N")
        self._bar_field(self.rtu_frame, "校验", self.parity)
        self.stopbits = ttk.Combobox(self.rtu_frame, values=["1", "1.5", "2"],
                                     width=4, state="readonly")
        self.stopbits.set("1")
        self._bar_field(self.rtu_frame, "停止位", self.stopbits)

        common = ttk.Frame(bar)
        common.pack(side="left", padx=(8, 0))
        self.unit_var = ttk.Spinbox(common, from_=1, to=247, width=4)
        self.unit_var.set("1")
        self._bar_field(common, "站地址", self.unit_var)
        self.timeout_var = ttk.Spinbox(common, from_=0.1, to=30, increment=0.5, width=4)
        self.timeout_var.set("2")
        self._bar_field(common, "超时", self.timeout_var)
        self.retry_var = ttk.Spinbox(common, from_=0, to=5, width=3)
        self.retry_var.set("1")
        self._bar_field(common, "重试", self.retry_var)

        ttk.Button(bar, text="连接", style="Accent.TButton", width=6,
                   command=self._toggle_connection).pack(side="left", padx=(4, 0))
        self.env_label = ttk.Label(bar, text="", style="Sub.TLabel")
        self.env_label.pack(side="left", padx=(10, 0))

    def _render_link(self) -> None:
        """顶栏的链路胶囊。

        站号必须摆在这里 —— 一条总线上挂七八台设备时，「我读的是哪台」
        比「我连的是哪个口」更要紧。
        """
        if self.client is None:
            failed = "失败" in self._conn_note
            self.led.configure(text=f"●  {self._conn_note or '未连接'}",
                               foreground=ERR if failed else MUTED)
            return
        where = f"{self._link_text} · 站 {self._link_unit}"
        if self._link_health == "warn":
            self.led.configure(text=f"●  无响应 {self._fail_streak} 次 · {where}",
                               foreground=WARN)
        elif self._link_health == "down":
            self.led.configure(text=f"●  链路中断 · {where}", foreground=ERR)
        else:
            self.led.configure(text=f"●  在线 · {where}", foreground=OK)

    def _mark_link(self, ok: bool) -> None:
        """每次请求结束都过一遍：成功清零，失败累加并降级。"""
        if ok:
            self._fail_streak = 0
            self._link_health = "on"
        else:
            self._fail_streak += 1
            self._link_health = "down" if self._fail_streak >= 3 else "warn"
        self._render_link()

    def _card(self, parent, title: str, actions=None):
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(14, 11, 14, 13))
        if title:
            head = ttk.Frame(outer, style="Inner.TFrame")
            head.pack(fill="x", pady=(0, 9))
            ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
            if actions:
                actions(head)             # 标题行右侧的视图切换/格式控件
        body = ttk.Frame(outer, style="Inner.TFrame")
        body.pack(fill="both", expand=True)
        return outer, body

    def _form_row(self, parent, row: int, label: str, widget, sticky="ew"):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=3)
        widget.grid(row=row, column=1, sticky=sticky, padx=(10, 0), pady=3)
        parent.columnconfigure(1, weight=1)
        return widget

    # ------------------------------------------------------------ 主站页

    def _build_master_tab(self) -> None:
        """主站页 = 一条工具条 + 一张铺满的表格 + 一条状态栏。

        以前这里是「左栏链路 290px + 右侧操作卡 193px + 数据卡 274px」，
        数据表只剩 141px。现在设置项都搬走了（连接参数在连接条、数据解释弹层），
        剩下的高度全部给表格。
        """
        card = tk.Frame(self.tab_master, bg=CARD)
        card.pack(fill="both", expand=True)
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)

        bar = ttk.Frame(card, padding=(10, 6, 10, 4))
        bar.grid(row=0, column=0, sticky="ew")
        self._build_toolbar(bar)
        ttk.Separator(card).grid(row=1, column=0, sticky="ew")

        tree_wrap = ttk.Frame(card, style="Inner.TFrame")
        tree_wrap.grid(row=2, column=0, sticky="nsew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        # -- 明细视图：一行一个值，HEX/BIN 常驻 --
        self.detail_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.detail_wrap.rowconfigure(0, weight=1)
        self.detail_wrap.columnconfigure(0, weight=1)
        columns = ("seq", "addr", "dec", "hex", "bin")
        self.result_tree = ttk.Treeview(self.detail_wrap, columns=columns, show="headings", height=12)
        for key, text, width, anchor in (
            ("seq", "#", 44, "center"),
            ("addr", "地址", 96, "center"),
            ("dec", "值", 130, "e"),
            ("hex", "HEX", 110, "e"),
            ("bin", "BIN", 260, "w"),
        ):
            # 表头对齐必须跟数据列一致（值/HEX 是右对齐），否则表头飘在左边、数据贴在右边
            self.result_tree.heading(key, text=text, anchor=anchor)
            self.result_tree.column(key, width=width, anchor=anchor, stretch=(key == "bin"))
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.detail_wrap, orient="vertical", command=self.result_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=scroll.set)

        # -- 网格视图：行头是起始地址，每行 N 格，进制跟「格式」走 --
        self.grid_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.grid_wrap.rowconfigure(0, weight=1)
        self.grid_wrap.columnconfigure(0, weight=1)
        self.result_grid = ttk.Treeview(self.grid_wrap, style="Grid.Treeview",
                                        show="headings", height=16)
        self._setup_grid(self.result_grid, 10, 74, "e")
        self.result_grid.grid(row=0, column=0, sticky="nsew")
        gscroll = ttk.Scrollbar(self.grid_wrap, orient="vertical",
                                command=self.result_grid.yview)
        gscroll.grid(row=0, column=1, sticky="ns")
        self.result_grid.configure(yscrollcommand=gscroll.set)
        # 不装横向滚动条：列宽会平分窗口，永远铺满，那条常驻的空滚动条只是噪音

        # 两个视图叠在同一格，靠 grid_remove 切换；默认亮网格
        self.grid_wrap.grid(row=0, column=0, sticky="nsew")
        self.detail_wrap.grid(row=0, column=0, sticky="nsew")
        self.detail_wrap.grid_remove()
        self.result_tree.tag_configure("odd", background="#f9fafb")
        # 变化高亮：比 zebra 后建，优先级更高，压得住斑马纹
        self.result_tree.tag_configure("chg", background="#e4edfd")
        self.result_grid.tag_configure("chg", background="#e4edfd")
        for tree in (self.result_tree, self.result_grid):
            tree.bind("<Button-1>", self._on_table_click, add="+")
            tree.bind("<Motion>", self._on_table_motion, add="+")
            tree.bind("<Leave>", lambda e: self._set_cursor_hint(None))
            tree.bind("<Button-3>", self._on_table_menu)
            tree.bind("<Double-1>", self._on_table_double)
            self._install_copy_menu(tree)

        ttk.Separator(card).grid(row=3, column=0, sticky="ew")
        self._build_status_bar(card)
        self._draw_trend()
        self._refresh_stats()          # 启动时就把「当前按什么解释」显示出来

    def _build_toolbar(self, bar) -> None:
        """一行装下所有高频动作。低频的「类型/字节序/系数/单位」进弹层。"""
        def label(text: str) -> None:
            ttk.Label(bar, text=text, style="Sub.TLabel").pack(side="left", padx=(0, 4))

        self.fc_box = ttk.Combobox(bar, values=[n for n, _ in FC_OPTIONS],
                                   state="readonly", width=15)
        self.fc_box.current(2)
        self.fc_box.bind("<<ComboboxSelected>>", lambda e: self._on_fc_change())
        self.fc_box.pack(side="left", padx=(0, 12))

        label("地址")
        self.addr_var = ttk.Spinbox(bar, from_=0, to=65535, width=6)
        self.addr_var.set("0")
        self.addr_var.pack(side="left", padx=(0, 10))

        label("数量")
        self.count_var = ttk.Spinbox(bar, from_=1, to=2000, width=5)
        self.count_var.set("10")
        self.count_var.pack(side="left", padx=(0, 10))

        self.write_label = ttk.Label(bar, text="值", style="Sub.TLabel")
        self.write_label.pack(side="left", padx=(0, 4))
        self.write_var = ttk.Entry(bar, width=10)
        self.write_var.pack(side="left", padx=(0, 4))
        # 「这个值会占几个寄存器」在按按钮之前就该看得见：
        # float32 填一个数就是 2 个寄存器，从输入框里数不出来。
        self.write_hint = ttk.Label(bar, text="", style="Sub.TLabel")
        self.write_hint.pack(side="left", padx=(0, 10))
        self.write_var.bind("<KeyRelease>", lambda e: self._preview_write())

        self.read_btn = ttk.Button(bar, text="读取", style="Accent.TButton", width=5,
                                   command=self._do_read_async)
        self.read_btn.pack(side="left")
        self.write_btn = ttk.Button(bar, text="写入", width=5, command=self._do_write_async)
        self.write_btn.pack(side="left", padx=(6, 0))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)

        label("轮询")
        self.interval_var = ttk.Spinbox(bar, from_=50, to=60000, increment=50, width=5)
        self.interval_var.set("500")
        self.interval_var.pack(side="left")
        ttk.Label(bar, text="ms", style="Sub.TLabel").pack(side="left", padx=(3, 8))
        self.poll_btn = ttk.Button(bar, text="开始轮询", width=8, command=self._toggle_poll)
        self.poll_btn.pack(side="left")

        # 右侧：视图与显示
        self.master_view_btn = ttk.Button(bar, text="明细", width=5,
                                          command=self._toggle_master_view)
        self.master_view_btn.pack(side="right")
        self.master_fmt_box = ttk.Combobox(bar, values=("值", "HEX", "BIN"),
                                           state="readonly", width=4)
        self.master_fmt_box.set("值")
        self.master_fmt_box.pack(side="right", padx=(0, 6))
        ttk.Label(bar, text="格式", style="Sub.TLabel").pack(side="right", padx=(0, 4))
        self.master_fmt_box.bind("<<ComboboxSelected>>",
                                 lambda e: self._on_display_change())
        # 「数据解释」比「显示设置」准确：里面全是「这串 16 位数字是什么含义」，
        # 没有一项是界面外观。而且工具条上已有「格式」（值/HEX/BIN），
        # 两个词都叫「显示」会分不清谁管什么。
        ttk.Button(bar, text="数据解释",
                   command=self._open_view_dialog).pack(side="right", padx=(0, 10))

    def _build_status_bar(self, card) -> None:
        row = ttk.Frame(card, padding=(10, 4, 10, 6))
        row.grid(row=4, column=0, sticky="ew")

        self.master_status = ttk.Label(row, text="尚未通信", style="Muted.TLabel")
        self.master_status.pack(side="left")
        # 鼠标指到哪个格子，这里就报它的绝对地址和值 —— 网格里"这是哪个寄存器"
        # 全靠这一行回答，不用再做行首地址 + 列偏移的心算。
        self.cursor_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.cursor_label.pack(side="left", padx=(16, 0))
        # ASCII 模式专用：把这一屏的寄存器拼成一整串贴在状态栏上。
        # 网格里一格两个字符，横着读要人自己拼 —— 而读型号时想看的就是整串。
        self.ascii_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.ascii_label.pack(side="left", padx=(16, 0))

        self.log_toggle_btn = ttk.Button(row, text="▴ 日志 0", command=self._toggle_log_panel)
        self.log_toggle_btn.pack(side="right")
        self.trend_canvas = tk.Canvas(row, width=110, height=18, bg=CARD, highlightthickness=0)
        self.trend_canvas.pack(side="right", padx=(10, 12))
        self.stats_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.stats_label.pack(side="right")

    # -- 数据解释弹层（类型/字节序/系数/单位/地址记法）---------------------

    def _build_view_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("数据解释")
        win.configure(bg=CARD)
        win.withdraw()
        win.transient(self.root)
        win.resizable(False, False)
        # 点右上角 X 只隐藏，不销毁 —— 销毁了句柄就失效，按钮会变成哑巴
        win.protocol("WM_DELETE_WINDOW", self._close_view_dialog)
        win.bind("<Escape>", lambda e: self._close_view_dialog())
        self._view_dialog = win

        body = ttk.Frame(win, style="Inner.TFrame", padding=(18, 16, 18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="寄存器怎么解释成物理量", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.dtype_box = ttk.Combobox(body, values=[n for n, _ in DTYPE_OPTIONS],
                                      state="readonly", width=16)
        self.dtype_box.current(0)
        self.dtype_box.bind("<<ComboboxSelected>>", lambda e: self._on_dtype_change())
        self._dialog_row(body, 1, "数据类型", self.dtype_box)

        self.byteorder_box = ttk.Combobox(body, values=["ABCD", "BADC", "CDAB", "DCBA"],
                                          state="disabled", width=8)
        self.byteorder_box.set("ABCD")
        self._dialog_row(body, 2, "字节序", self.byteorder_box)

        self.scale_var = ttk.Entry(body, width=10)
        self.scale_var.insert(0, "1")
        self._dialog_row(body, 3, "× 系数", self.scale_var)

        self.unit_text_var = ttk.Entry(body, width=10)
        self._dialog_row(body, 4, "单位", self.unit_text_var)

        # 地址记法和「值怎么解释」是两件事，用一条分隔线分开，
        # 否则混在类型/字节序下面，会以为地址也是数据类型的一部分
        ttk.Separator(body).grid(row=5, column=0, columnspan=2,
                                 sticky="ew", pady=(12, 6))
        ttk.Label(body, text="地址怎么记", style="Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 2))

        # 三种记法互斥，所以是单选（Modbus Poll 也是 Base 1 / Base 0 二选一）。
        # 以前这里是两个复选框，而渲染时 PLC 优先（_addr_hex and not plc_var），
        # 两个都勾上时十六进制会悄悄失效 —— 看着像开关坏了。
        self._addr_hex = tk.BooleanVar(value=False)
        self.plc_var = tk.BooleanVar(value=False)
        self._addr_mode = tk.StringVar(value="dec")
        for offset, (text, value) in enumerate((
                ("十进制", "dec"),
                ("十六进制（0x0000）", "hex"),
                ("PLC 记法（40001 起）", "plc"))):
            ttk.Radiobutton(body, text=text, value=value, variable=self._addr_mode,
                            command=self._addr_mode_changed).grid(
                row=7 + offset, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Button(body, text="关闭", command=self._close_view_dialog).grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        # 重建时把上次的设置放回去，别把用户选的 float32 悄悄变回 u16
        state = self._view_state
        if state:
            self.dtype_box.set(state.get("dtype", "无符号 16 位"))
            self.byteorder_box.set(state.get("byteorder", "ABCD"))
            self.scale_var.delete(0, "end")
            self.scale_var.insert(0, state.get("scale", "1"))
            self.unit_text_var.delete(0, "end")
            self.unit_text_var.insert(0, state.get("unit", ""))
            self.plc_var.set(bool(state.get("plc", False)))
            self._addr_hex.set(bool(state.get("addr_hex", False)))
            self._sync_addr_mode()

    def _dialog_row(self, parent, row: int, label: str, widget) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=4)
        widget.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        parent.columnconfigure(1, weight=1)

    def _open_view_dialog(self) -> None:
        if not self._view_dialog.winfo_exists():
            self._build_view_dialog()      # 极端情况下窗口被系统销毁过，重建一个
        self._sync_addr_mode()         # 界面上的单选要和实际生效的记法一致
        win = self._view_dialog
        win.deiconify()
        win.update_idletasks()
        # 跟着主窗口居中偏上，别弹到屏幕左上角去（以前固定落在 8,31）
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.lift()
        win.focus_force()
        try:
            win.grab_set()
        except tk.TclError:
            pass

    def _addr_mode_changed(self) -> None:
        """用户点了某一种地址记法 —— 单选按钮唯一的一次写入口。"""
        mode = self._addr_mode.get()
        self._addr_hex.set(mode == "hex")
        self.plc_var.set(mode == "plc")
        self._on_display_change()

    def _sync_addr_mode(self) -> None:
        """把两个内部布尔反过来对齐到单选按钮上。

        这两个布尔可能是别处改的（弹层重建恢复、测试直接 set），打开弹层时对齐一次，
        免得界面显示「十进制」而实际已经在按 PLC 记法解析。
        """
        if self.plc_var.get():
            mode = "plc"
        elif self._addr_hex.get():
            mode = "hex"
        else:
            mode = "dec"
        self._addr_mode.set(mode)

    def _close_view_dialog(self) -> None:
        try:
            self._view_dialog.grab_release()
        except tk.TclError:
            pass
        self._view_dialog.withdraw()

    # ------------------------------------------------------------ 从站页

    def _build_slave_tab(self) -> None:
        container = ttk.Frame(self.tab_slave)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=0, minsize=300)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)

        cfg_card, cfg_body = self._card(container, "从站参数")
        cfg_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        mode_row = ttk.Frame(cfg_body, style="Inner.TFrame")
        mode_row.pack(fill="x", pady=(0, 6))
        self.slave_mode = tk.StringVar(value="tcp")
        ttk.Radiobutton(mode_row, text="TCP 监听", value="tcp",
                        variable=self.slave_mode,
                        command=self._on_slave_mode_change).pack(side="left")
        ttk.Radiobutton(mode_row, text="RTU 串口", value="rtu",
                        variable=self.slave_mode,
                        command=self._on_slave_mode_change).pack(side="left", padx=(14, 0))

        self.slave_link_holder = ttk.Frame(cfg_body, style="Inner.TFrame")
        self.slave_link_holder.pack(fill="x")

        self.slave_tcp_frame = ttk.Frame(self.slave_link_holder, style="Inner.TFrame")
        self.slave_host = ttk.Entry(self.slave_tcp_frame, width=16)
        # 默认只听本机：Modbus 没有鉴权，绑 0.0.0.0 等于把一块可读写的内存
        # 放到整个网段上。要联调远端设备时手动改成 0.0.0.0。
        self.slave_host.insert(0, "127.0.0.1")
        self._form_row(self.slave_tcp_frame, 0, "监听地址", self.slave_host)
        self.slave_port = ttk.Spinbox(self.slave_tcp_frame, from_=1, to=65535, width=8)
        self.slave_port.set("502")
        self._form_row(self.slave_tcp_frame, 1, "端口", self.slave_port)
        ttk.Label(self.slave_tcp_frame, text="局域网访问请改成 0.0.0.0",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=2,
                                             sticky="w", pady=(2, 0))

        self.slave_rtu_frame = ttk.Frame(self.slave_link_holder, style="Inner.TFrame")
        ports = list_serial_ports()
        self.slave_serial = ttk.Combobox(self.slave_rtu_frame, values=ports, width=14)
        self.slave_serial.set(ports[0] if ports else "COM3")
        self._form_row(self.slave_rtu_frame, 0, "串口", self.slave_serial)
        self.slave_baud = ttk.Combobox(self.slave_rtu_frame, values=["9600", "19200", "38400", "115200"],
                                       width=10, state="readonly")
        self.slave_baud.set("9600")
        self._form_row(self.slave_rtu_frame, 1, "波特率", self.slave_baud)
        # 数据位/校验位/停止位原来「借」主站页的控件 —— 用户在从站页看不到
        # 这两个参数的来源，改主站页才发现从站行为变了。补齐成自己的。
        self.slave_databits = ttk.Combobox(self.slave_rtu_frame, values=["8", "7", "6", "5"],
                                           width=4, state="readonly")
        self.slave_databits.set("8")
        self._form_row(self.slave_rtu_frame, 2, "数据位", self.slave_databits)
        self.slave_parity = ttk.Combobox(self.slave_rtu_frame, values=["N", "E", "O", "M", "S"],
                                         width=4, state="readonly")
        self.slave_parity.set("N")
        self._form_row(self.slave_rtu_frame, 3, "校验", self.slave_parity)
        self.slave_stopbits = ttk.Combobox(self.slave_rtu_frame, values=["1", "1.5", "2"],
                                           width=4, state="readonly")
        self.slave_stopbits.set("1")
        self._form_row(self.slave_rtu_frame, 4, "停止位", self.slave_stopbits)

        self.slave_tcp_frame.pack(fill="x")

        ttk.Separator(cfg_body).pack(fill="x", pady=10)

        common = ttk.Frame(cfg_body, style="Inner.TFrame")
        common.pack(fill="x")
        self.slave_unit = ttk.Spinbox(common, from_=0, to=247, width=8)
        self.slave_unit.set("1")
        self._form_row(common, 0, "站地址", self.slave_unit)
        self.slave_size = ttk.Spinbox(common, from_=1, to=65536, width=8)
        self.slave_size.set("10000")
        self._form_row(common, 1, "区间长度", self.slave_size)

        self.slave_btn = ttk.Button(cfg_body, text="启动从站", style="Accent.TButton",
                                    command=self._toggle_server)
        self.slave_btn.pack(fill="x", pady=(14, 0))

        self.demo_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg_body, text="输入寄存器模拟变化（便于演示）",
                        variable=self.demo_var).pack(anchor="w", pady=(10, 0))

        self.slave_stat = ttk.Label(cfg_body, text="未启动", style="Muted.TLabel",
                                    justify="left", wraplength=250)
        self.slave_stat.pack(anchor="w", pady=(10, 0))

        # -- 右：寄存器视图 --
        view_card, view_body = self._card(
            container, "寄存器视图 · 双击数值可直接改",
            actions=self._build_slave_view_actions)
        view_card.grid(row=0, column=1, sticky="nsew")
        view_body.rowconfigure(1, weight=1)
        view_body.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(view_body, style="Inner.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.area_box = ttk.Combobox(toolbar, values=[n for n, _ in AREA_OPTIONS],
                                     state="readonly", width=20)
        self.area_box.current(2)
        self.area_box.pack(side="left")
        ttk.Label(toolbar, text="起始", style="Card.TLabel").pack(side="left", padx=(12, 0))
        self.view_start = ttk.Spinbox(toolbar, from_=0, to=65535, width=7)
        self.view_start.set("0")
        self.view_start.pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, text="数量", style="Card.TLabel").pack(side="left", padx=(10, 0))
        self.view_count = ttk.Spinbox(toolbar, from_=1, to=500, width=6)
        self.view_count.set("32")
        self.view_count.pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="刷新", command=self._refresh_slave_table).pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="全部清零", command=self._clear_slave).pack(side="left", padx=(6, 0))
        self.slave_edit_hint = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.slave_edit_hint.pack(side="right")

        tree_wrap = ttk.Frame(view_body, style="Inner.TFrame")
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        # -- 明细：一行一个寄存器 --
        self.slave_detail_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.slave_detail_wrap.rowconfigure(0, weight=1)
        self.slave_detail_wrap.columnconfigure(0, weight=1)
        self.slave_tree = ttk.Treeview(
            self.slave_detail_wrap, columns=("addr", "dec", "hex", "bin"),
            show="headings", height=16)
        for key, text, width, anchor in (
            ("addr", "地址", 80, "center"),
            ("dec", "值", 120, "e"),
            ("hex", "HEX", 90, "e"),
            ("bin", "BIN", 220, "w"),
        ):
            self.slave_tree.heading(key, text=text, anchor=anchor)
            self.slave_tree.column(key, width=width, anchor=anchor, stretch=(key == "bin"))
        self.slave_tree.grid(row=0, column=0, sticky="nsew")
        sbar = ttk.Scrollbar(self.slave_detail_wrap, orient="vertical",
                             command=self.slave_tree.yview)
        sbar.grid(row=0, column=1, sticky="ns")
        self.slave_tree.configure(yscrollcommand=sbar.set)
        self.slave_tree.tag_configure("odd", background="#f9fafb")
        self.slave_tree.bind("<Double-1>", self._begin_cell_edit)

        # -- 网格：行头补零起始地址，每行 N 格，进制跟「进制」下拉走 --
        self.slave_grid_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.slave_grid_wrap.rowconfigure(0, weight=1)
        self.slave_grid_wrap.columnconfigure(0, weight=1)
        self.slave_grid = ttk.Treeview(self.slave_grid_wrap, style="Grid.Treeview",
                                       show="headings", height=19)
        self._setup_grid(self.slave_grid, 10, 66, "e")
        self.slave_grid.grid(row=0, column=0, sticky="nsew")
        gsbar = ttk.Scrollbar(self.slave_grid_wrap, orient="vertical",
                              command=self.slave_grid.yview)
        gsbar.grid(row=0, column=1, sticky="ns")
        self.slave_grid.configure(yscrollcommand=gsbar.set)
        # 列宽会平分，横向滚动条没有用处，只会占掉一行高度
        self.slave_grid.tag_configure("odd", background="#f9fafb")
        self.slave_grid.bind("<Double-1>", self._begin_grid_edit)

        # 两个视图叠在同一格；默认亮网格
        self.slave_grid_wrap.grid(row=0, column=0, sticky="nsew")
        self.slave_detail_wrap.grid(row=0, column=0, sticky="nsew")
        self.slave_detail_wrap.grid_remove()

    # -------------------------------------------------------------- 日志

    def _build_log(self) -> None:
        """日志做成覆盖层：默认收起成状态栏上的一个按钮，点开盖住底部 232px。

        以前它是常驻的 201px —— 占窗口 26%，比数据表还高。排障要看报文时展开，
        平时不占地方，看完收起，数据区一点不缩。
        """
        panel = tk.Frame(self.stack, bg=CARD, highlightthickness=1,
                         highlightbackground=BORDER)
        self.log_panel = panel
        self._log_open = False

        head = tk.Frame(panel, bg=CARD)
        head.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Label(head, text="通信日志", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(head, text="收起", width=5,
                   command=lambda: self._toggle_log_panel(False)).pack(side="right")
        self.log_pause_btn = ttk.Button(head, text="暂停", width=5,
                                        command=self._toggle_log_pause)
        self.log_pause_btn.pack(side="right", padx=(0, 6))
        ttk.Button(head, text="导出…", width=6,
                   command=self._export_log).pack(side="right", padx=(0, 6))
        ttk.Button(head, text="清空", width=5,
                   command=self._clear_log).pack(side="right", padx=(0, 6))

        body = tk.Frame(panel, bg=CARD)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.log_text = tk.Text(body, height=8, bg="#f9fafb", fg=TEXT, font=LOG_FONT,
                                relief="flat", wrap="none", state="disabled",
                                insertbackground=TEXT, padx=8, pady=6)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=bar.set)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("time", foreground=MUTED, font=MONO)
        self.log_text.tag_configure("frame", foreground="#61666b", font=MONO)

    def _toggle_log_panel(self, show: bool | None = None) -> None:
        if show is None:
            show = not self._log_open
        self._log_open = bool(show)
        if self._log_open:
            # 上移 30px 让出状态栏：一边看报文一边还要能读到 FC/地址/错误数
            self.log_panel.place(relx=0, rely=1.0, anchor="sw", relwidth=1,
                                 height=210, y=-30)
            self.log_panel.lift()
            self.log_text.see("end")
        else:
            self.log_panel.place_forget()
        self._update_log_button()

    def _update_log_button(self) -> None:
        button = getattr(self, "log_toggle_btn", None)
        if button is None:
            return
        arrow = "▾" if self._log_open else "▴"
        button.configure(text=f"{arrow} 日志 {self._log_lines}")

    def _toggle_log_pause(self) -> None:
        """暂停只停「往界面上画」，报文该收该记的一点不少 —— 翻旧帧时不被拽回底部。"""
        if not self._log_paused:
            self._append_log("日志已暂停（新条目只计数，点「继续」恢复）")
            self._log_paused = True
            self.log_pause_btn.configure(text="继续")
            return
        self._log_paused = False
        self.log_pause_btn.configure(text="暂停")
        skipped, self._log_skipped = self._log_skipped, 0
        if skipped:
            self._append_log(f"（暂停期间省略 {skipped} 条）")
        self.log_text.see("end")

    def _export_log(self) -> None:
        import time as _time
        from tkinter import filedialog
        content = self.log_text.get("1.0", "end").strip("\n")
        if not content:
            self._notify("日志是空的，没有可导出的内容", error=True)
            return
        path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            initialfile=f"modbus-log-{_time.strftime('%Y%m%d-%H%M%S')}.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as handle:
                handle.write(content + "\n")
        except OSError as exc:
            self._notify(f"导出失败：{exc}", error=True)
            return
        self._log(f"日志已导出：{path}", "ok")

    # ------------------------------------------------------------ 事件循环

    def _post(self, kind: str, *payload) -> None:
        self._events.put((kind, *payload))

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "log":
                self._append_log(*event[1:])
            elif event[0] == "done":
                _, callback, result, error = event
                try:
                    callback(result, error)
                except Exception as exc:  # 回调自身的错误不能吞掉整个事件循环
                    self._append_log(f"界面处理异常：{exc}", "err")
        self.root.after(40, self._drain_events)

    def _async(self, work, on_done) -> None:
        """把 work 丢到工作线程，完成后在主线程调 on_done(result, error)。"""

        def runner():
            try:
                result = work()
            except Exception as exc:
                self._events.put(("done", on_done, None, exc))
            else:
                self._events.put(("done", on_done, result, None))

        self._pool.submit(runner)

    def _log(self, text: str, tag: str = "") -> None:
        self._post("log", text, tag)

    def _notify(self, text: str, error: bool = False) -> None:
        """主站页的提示统一走状态栏 + 日志。

        刻意不用 messagebox：现场调试时模态框会打断连续操作，
        而且脚本化驱动界面时弹窗会直接把进程卡住。
        """
        self.master_status.configure(text=text, foreground=ERR if error else MUTED)
        self._log(text, "err" if error else "")

    def _append_log(self, text: str, tag: str = "") -> None:
        import time
        if self._log_paused:
            self._log_skipped += 1
            return
        # 只有本来就贴在底部时才跟随。否则用户往上翻旧帧，每条新报文都会把他拽回底。
        try:
            at_bottom = self.log_text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] ", "time")
        self.log_text.insert("end", text + "\n", tag)
        self._log_lines += 1
        if self._log_lines > 400:
            self.log_text.delete("1.0", "80.0")
            self._log_lines -= 79
        if at_bottom:
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._update_log_button()

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_lines = 0

    # ------------------------------------------------------------ 主站逻辑

    def _on_link_mode_change(self) -> None:
        self.tcp_frame.pack_forget()
        self.rtu_frame.pack_forget()
        if self.link_mode.get() == "tcp":
            self.tcp_frame.pack(fill="x")
        else:
            self.rtu_frame.pack(fill="x")

    def _on_slave_mode_change(self) -> None:
        self.slave_tcp_frame.pack_forget()
        self.slave_rtu_frame.pack_forget()
        if self.slave_mode.get() == "tcp":
            self.slave_tcp_frame.pack(fill="x")
        else:
            self.slave_rtu_frame.pack(fill="x")

    def _on_fc_change(self) -> None:
        fc = FC_OPTIONS[self.fc_box.current()][1]
        is_read = fc in (1, 2, 3, 4)
        self.read_btn.configure(state="normal" if is_read else "disabled")
        self.write_btn.configure(state="disabled" if is_read else "normal")
        self._prev_cells = None
        if is_read:
            self.count_var.configure(state="normal")
            self.write_label.pack_forget()
            self.write_var.pack_forget()
            self.write_hint.pack_forget()
        else:
            self.write_label.pack(side="left", padx=(0, 4), before=self.read_btn)
            self.write_var.pack(side="left", padx=(0, 4), before=self.read_btn)
            self.write_hint.pack(side="left", padx=(0, 10), before=self.read_btn)
            limit = write_limit(fc) or 123
            self.count_var.configure(state="disabled")
            self._log(f"功能码 {fc:02d} 为写操作，数量由写入值的个数决定（上限 {limit}）")
        self._preview_write()

    def _preview_write(self) -> None:
        """在工具条上实时告诉你：这串值会占几个寄存器。

        FC16 里「填了几个值」和「写了几个寄存器」不是一回事 —— float32 一个值
        占 2 个、float64 占 4 个。这个数字以前要等发完帧才知道。
        """
        label = getattr(self, "write_hint", None)
        if label is None:
            return
        fc = self._current_fc()
        if fc not in (5, 6, 15, 16):
            label.configure(text="")
            return
        raw = _normalize_number(self.write_var.get())
        if self._current_dtype() == "ascii":
            if not raw:
                label.configure(text="—")
                return
            try:
                byte_len = len(raw.encode("ascii"))
            except UnicodeEncodeError:
                label.configure(text="只能写 ASCII 字符")
                return
            # 一个寄存器两个字符，奇数长度要补一个 0x00，所以向上取整
            count = (byte_len + 1) // 2
            label.configure(text=f"→ {count} 个寄存器（{count * 2} 字符）")
            return
        items = [p for p in raw.replace("，", ",").replace("；", ",").split(",") if p.strip()]
        if not items:
            label.configure(text="—")
            return
        if fc == 6:
            label.configure(text="→ 1 个寄存器")
            return
        if fc == 5:
            label.configure(text="→ 1 个线圈")
            return
        if fc == 15:
            label.configure(text=f"→ {len(items)} 个线圈")
            return
        count = len(items) * span(self._current_dtype())
        # 只报寄存器数：字节数在状态栏和日志里都有，工具条这一行寸土寸金
        label.configure(text=f"→ {count} 个寄存器")

    def _build_transport(self):
        mode = self.link_mode.get()
        try:
            timeout = float(self.timeout_var.get())
        except ValueError:
            raise ValueError("超时时间必须是数字") from None
        if mode == "tcp":
            host = self.tcp_host.get().strip() or "127.0.0.1"
            try:
                port = int(self.tcp_port.get())
            except ValueError:
                raise ValueError("端口必须是整数") from None
            return TcpTransport(host, port, timeout=timeout)
        if not HAS_SERIAL:
            raise RuntimeError("RTU 模式需要 pyserial：pip install pyserial")
        port = self.rtu_port.get().strip()
        if not port:
            raise ValueError("请填写串口名，例如 COM3")
        return RtuTransport(
            port,
            baudrate=int(self.baud.get()),
            bytesize=int(self.databits.get()),
            parity=self.parity.get(),
            stopbits=float(self.stopbits.get()),
            timeout=timeout,
        )

    def _toggle_connection(self) -> None:
        if self.client is not None:
            self._stop_poll()
            client, self.client = self.client, None
            self._async(client.close, lambda r, e: None)
            self._set_link_state(False, "已断开")
            self._refresh_stats()
            self._log("链路已关闭")
            return

        if self._busy:
            return
        try:
            transport = self._build_transport()
            unit = self._int_field(self.unit_var, "站地址", 1, 247)
            retries = self._int_field(self.retry_var, "重试次数", 0, 5)
        except (ValueError, RuntimeError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return

        self._busy = True
        self.connect_btn.configure(state="disabled", text="连接中…")
        self._log(f"正在连接 {transport.label} …")

        client = ModbusClient(transport, unit, retries, on_log=self._client_log)
        client.transport.set_tap(self._on_frame)   # 原始报文进日志

        def work():
            transport.open()
            return client

        def done(result, error):
            self._busy = False
            self.connect_btn.configure(state="normal", text="断开")
            if error:
                self.connect_btn.configure(text="连接")
                self._set_link_state(False, "连接失败")
                self._log(f"连接失败：{self._friendly_error(error)}", "err")
                return
            self.client = result
            self._set_link_state(True, transport.label)
            self._refresh_stats()
            self._log(f"已连接 {transport.label}，站地址 {unit}", "ok")

        self._async(work, done)

    def _set_link_state(self, connected: bool, text: str) -> None:
        self._conn_note = text
        if connected:
            self._link_text = text
            self._link_health = "on"
            self._fail_streak = 0
            try:
                self._link_unit = int(self.unit_var.get())
            except ValueError:
                self._link_unit = 1
        else:
            self._link_text = ""
            self._link_health = "off"
            self._fail_streak = 0
        self._render_link()
        self.connect_btn.configure(text="断开" if connected else "连接")
        self.env_label.configure(text=text)
        # 连上就把参数条收起来，把 40px 还给数据区；断开时再露出来
        if connected:
            self.connect_bar.pack_forget()
        else:
            self.connect_bar.pack(fill="x", before=self.stack)

    def _require_client(self) -> ModbusClient | None:
        if self.client is None:
            self._notify("尚未连接，请先点“连接”", error=True)
            return None
        return self.client

    # -- 输入解析与错误文案 ------------------------------------------------
    #
    # 以前这里直接 int(entry.get())，用户把地址填成 "1O"（字母 O）时，
    # 状态栏会原样冒出 `invalid literal for int() with base 10: '1O'`。
    # 那是给写代码的人看的，不是给调设备的人看的。

    @staticmethod
    def _int_field(widget, label: str, minimum: int | None = None,
                   maximum: int | None = None) -> int:
        raw = _normalize_number(widget.get())
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}必须是整数（现在填的是「{raw or '空'}」）") from None
        if minimum is not None and value < minimum:
            raise ValueError(f"{label}不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label}不能大于 {maximum}")
        return value

    def _friendly_error(self, exc: BaseException) -> str:
        """给用户看的错误一句话；英文原文（Python 内部错误）只写日志。"""
        text = describe_error(exc).strip() or exc.__class__.__name__
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            self._log(f"内部错误：{exc.__class__.__name__}: {text}", "err")
            return "操作失败（内部错误），细节已写入日志"
        return text

    def _parse_write_values(self, fc: int) -> list | None:
        """解析「写入值」输入框。切分与类型转换见 parsing.parse_write_values。

        GUI 只负责把错误变成状态栏提示 —— 早期这里自己切分，分隔符集合
        与 CLI 不一致（少了中文顿号），两边对同一串输入的解释会不一样。
        """
        raw = self.write_var.get().strip()
        if not raw:
            self._notify("请先在「写入值」里填写数据", error=True)
            return None
        try:
            return parse_write_values(raw, fc, self._current_dtype())
        except ValueError as exc:
            self._notify(str(exc), error=True)
            return None

    def _do_read_async(self, note: str = "") -> None:
        """读一次。note 非空时，读完后状态栏先显示它（写后回读用）。

        不这么做的话，「已写入 2 个寄存器」会被紧接着的「FC03 地址 0 共 10 点」
        冲掉 —— 而这恰恰是用户最想确认的那句话。
        """
        client = self._require_client()
        if client is None:
            return
        fc = self._current_fc()
        if fc not in (1, 2, 3, 4):
            self._notify("当前功能码是写操作，请点“写入”", error=True)
            return
        limit = read_limit(fc) or 125
        try:
            address = self._resolve_address(
                self._int_field(self.addr_var, "起始地址", 0, 65535), fc)
            count = self._int_field(self.count_var, "数量", 1, limit)
        except (ValueError, AddressError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return

        self._begin_request(
            f"读取中…（超时 {self.timeout_var.get()} s，重试 {self.retry_var.get()} 次）")
        self._async(lambda: client.read(fc, address, count),
                    lambda values, error: self._on_read_done(values, error, address, fc, note))

    def _begin_request(self, text: str) -> None:
        """请求期间给个明确反馈。

        超时 2s × 重试 3 时，界面会有近 8 秒毫无动静 —— 只把按钮变灰，
        用户会以为程序卡死，然后去点第二次。
        """
        self.read_btn.configure(state="disabled")
        self.write_btn.configure(state="disabled")
        self.master_status.configure(text=text, foreground=MUTED)

    def _end_request(self) -> None:
        is_read = self._current_fc() in (1, 2, 3, 4)
        self.read_btn.configure(state="normal" if is_read else "disabled")
        self.write_btn.configure(state="disabled" if is_read else "normal")

    def _on_read_done(self, values, error, address: int, fc: int, note: str = "") -> None:
        self._end_request()
        if error:
            reason = self._friendly_error(error)
            self.master_status.configure(text=f"读取失败：{reason}", foreground=ERR)
            self._log(f"读取失败：{reason}", "err")
            self._mark_link(False)
            self._refresh_stats()
            return
        self._mark_link(True)
        self._render_values(values, address)
        latency = self.client.last_latency_ms if self.client else None
        extra = f" · {latency:.1f} ms" if latency is not None else ""
        if note:
            # 回读的是整个视野，不是这次写的数量 —— 别再报一遍数字，会被误读
            text = f"{note} · 已回读确认{extra}"
        else:
            text = f"FC{fc:02d} 地址 {address} 共 {len(values)} 点{extra}"
        self.master_status.configure(text=text, foreground=MUTED)
        self._refresh_stats()

    # -- 类型与地址 --------------------------------------------------------

    def _current_fc(self) -> int:
        index = self.fc_box.current()
        return FC_OPTIONS[index if index >= 0 else 0][1]

    def _current_dtype(self) -> str:
        index = self.dtype_box.current()
        return DTYPE_OPTIONS[index if index >= 0 else 0][1]

    def _on_dtype_change(self) -> None:
        # 16 位数值没有字节序可言，把下拉灰掉比留着让人误选更清楚。
        # ASCII 是个例外：它虽然也只有一个寄存器，但高字节/低字节谁在前
        # 直接决定读出来是 "AB" 还是 "BA"，必须让人选。
        dtype = self._current_dtype()
        narrow = span(dtype) == 1 and dtype != "ascii"
        self.byteorder_box.configure(state="disabled" if narrow else "readonly")
        self._on_display_change()
        self._preview_write()

    def _display_address(self, address: int, fc: int) -> int:
        """协议地址 → 界面显示地址（PLC 记法时换算）。实现见 addressing。"""
        area = area_of_fc(fc) or "holding_registers"
        mode = "plc" if self.plc_var.get() else "protocol"
        return display_address(address, area, mode)

    def _resolve_address(self, address: int, fc: int) -> int:
        """界面地址 → 协议地址。实现见 addressing.resolve_address。"""
        mode = "plc" if self.plc_var.get() else "protocol"
        protocol, _area = resolve_address(fc, address, mode)
        return protocol

    def _client_log(self, text: str) -> None:
        """客户端自己的日志分流进界面。

        「写 FC16 地址 4 ← 16320 16416 16480 …」这条界面会打更清楚的一版
        （值 + 类型 + 十六进制寄存器），这里丢掉，免得一个动作两行还互相矛盾。
        """
        if text.startswith("写 FC"):
            return
        self._log(text)

    def _on_frame(self, direction: str, unit: int, frame: bytes) -> None:
        self._log(f"{direction.upper()}  " + " ".join("%02X" % b for b in frame), "frame")
        # 写请求附一行解读：数量到底几个，从帧里读，不从界面参数猜
        if direction == "tx":
            note = describe_write_frame(frame)
            if note:
                self._log("      └ " + note, "frame")

    def _render_values(self, values: list, address: int) -> None:
        # 读回来的原始数据先存档：切换 视图/进制 时不用重发请求就能重画
        self._last_read = (values, address)
        self._render_master_data()
        self._push_trend(values, address)

    def _render_master_data(self) -> None:
        self._update_ascii_preview()
        if self._last_read is None:
            return
        values, address = self._last_read
        if self._master_grid:
            self._fill_master_grid(values, address)
        else:
            self._fill_master_detail(values, address)
        self._paint_written()

    def _update_ascii_preview(self) -> None:
        """ASCII 模式下把整屏寄存器拼成字符串贴到状态栏。

        尾部连续的控制字节（设备字符串区常见的 0x00 填充）不显示 ——
        否则型号后面永远拖着一串「·」，把真正的内容挤到左边看不见。
        """
        label = getattr(self, "ascii_label", None)
        if label is None:
            return
        if self._current_dtype() != "ascii" or self._last_read is None:
            label.configure(text="")
            return
        values, _ = self._last_read
        text = "".join(decode(values, "ascii", self.byteorder_box.get() or "ABCD"))
        text = text.rstrip(NONPRINT)
        label.configure(text=f'"{text}"' if text else "")

    # -- 写入范围框选 -----------------------------------------------------

    def _mark_written(self, address: int, count: int) -> None:
        """记住这次写覆盖了哪段地址，回读渲染完就在表格上框出来。

        看「2 个还是更多个」最直接的办法就是数框了几个格子 —— 比读日志快。
        2.6 秒后自动消失，免得下次看到还以为是刚写的。
        """
        self._written_span = (address, address + max(1, int(count)))
        if self._written_job is not None:
            try:
                self.root.after_cancel(self._written_job)
            except Exception:
                pass
        self._written_job = self.root.after(2600, self._expire_written)

    def _expire_written(self) -> None:
        self._written_job = None
        self._written_span = None
        self._clear_written_marks()

    def _clear_written_marks(self) -> None:
        for bar in self._written_marks:
            try:
                bar.destroy()
            except Exception:
                pass
        self._written_marks.clear()

    def _paint_written(self) -> None:
        """给「刚写过的格子」围一圈 2px 的蓝边。

        没用 tag 是因为 Treeview 的 tag 只能整行染色：写 2 个寄存器会把那行
        10 格全染蓝，反倒看不出写了几个。围边不挡字，一格一个框，数量一目了然。
        """
        self._clear_written_marks()
        span = self._written_span
        if span is None or self._last_read is None:
            return
        start, end = span
        _, address = self._last_read
        step = self._cell_step()
        tree = self.result_grid if self._master_grid else self.result_tree
        targets: list[tuple[str, str]] = []

        if self._master_grid:
            pad, per = self._grid_pad, self._grid_per
            for row, iid in enumerate(tree.get_children()):
                for col in range(per):
                    index = row * per + col - pad
                    if index < 0:
                        continue
                    if start <= address + index * step < end:
                        targets.append((iid, f"c{col}"))
        else:
            for index, iid in enumerate(tree.get_children()):
                if start <= address + index * step < end:
                    targets.append((iid, "dec"))
        if not targets:
            return
        tree.update_idletasks()        # bbox 依赖布局，不先刷一下会拿到空值
        for iid, column in targets:
            box = tree.bbox(iid, column)
            if not box:
                continue                   # 滚出视口了，不画
            x, y, w, h = box
            for px, py, pw, ph in ((x, y, w, 2), (x, y + h - 2, w, 2),
                                   (x, y, 2, h), (x + w - 2, y, 2, h)):
                bar = tk.Frame(tree, bg=ACCENT)
                bar.place(x=px, y=py, width=pw, height=ph)
                self._written_marks.append(bar)

    # -- 变化高亮 ---------------------------------------------------------

    def _change_flags(self, cells: list[str]) -> list[bool]:
        """和上一帧比，哪些格子变了。

        第一次渲染、长度变了、换了视图或格式，都不算「变化」—— 否则切一下
        就满屏蓝底，真正的跳变反而被淹掉。
        """
        prev = self._prev_cells
        self._prev_cells = list(cells)
        if prev is None or len(prev) != len(cells):
            return [False] * len(cells)
        return [a != b for a, b in zip(prev, cells)]

    def _schedule_fade(self, tree) -> None:
        if self._fade_job is not None:
            try:
                self.root.after_cancel(self._fade_job)
            except Exception:
                pass
        self._fade_job = self.root.after(900, lambda: self._fade_tags(tree))

    def _fade_tags(self, tree) -> None:
        self._fade_job = None
        for iid in tree.get_children():
            tags = tree.item(iid, "tags")
            if "chg" in tags:
                tree.item(iid, tags=tuple(t for t in tags if t != "chg"))

    # -- 显示形态：地址记法与二进制分组 -----------------------------------

    def _grid_fmt(self) -> str:
        """「格式」下拉当前生效的值。

        格式（值/HEX/BIN）只有网格视图有语义；明细视图里控件虽然被禁用，
        但 `get()` 仍然返回上次选的 HEX/BIN —— 直接读控件会让
        `_cell_step()` 以为「一格一个寄存器」，进而把就地写的地址算成
        `address + index * 1`（正确值是一格 span 个寄存器），
        写错地址的同时还按十六进制解析用户输入。所有需要格式的地方
        必须走这个函数。
        """
        return self.master_fmt_box.get() if self._master_grid else "值"

    def _addr_text(self, address: int, fc: int) -> str:
        shown = self._display_address(address, fc)
        # PLC 记法本身就是 40001 这种十进制语义，再转十六进制只会更绕
        if self._addr_hex.get() and not self.plc_var.get():
            return f"0x{shown:04X}"
        return str(shown)

    @staticmethod
    def _bin_text(value: int) -> str:
        """16 位按 4 位分组：1000 0000 0000 0000 才数得清是第几位。"""
        return bin_of([value], 4)

    def _fill_master_detail(self, values: list, address: int) -> None:
        tree = self.result_tree
        tree.delete(*tree.get_children())
        fc = self._current_fc()
        rows: list[tuple] = []
        cells: list[str] = []

        if fc in (1, 2):
            for offset, bit in enumerate(values):
                text = "1" if bit else "0"
                rows.append((offset, self._addr_text(address + offset, fc), text,
                             "01" if bit else "00", "●" if bit else "○"))
                cells.append(text)
        else:
            dtype = self._current_dtype()
            unit = self.unit_text_var.get().strip()
            try:
                scale = float(self.scale_var.get() or 1)
            except ValueError:
                scale = 1.0
            step = span(dtype)
            decoded = decode(values, dtype, self.byteorder_box.get() or "ABCD", scale, 0.0)
            for index, value in enumerate(decoded):
                start = address + index * step
                chunk = values[index * step:(index + 1) * step]
                hexa = hex_of(chunk)
                bina = " ".join(self._bin_text(r) for r in chunk)
                text = format_value(value, dtype) + (f" {unit}" if unit else "")
                rows.append((index, self._addr_text(start, fc), text, hexa, bina))
                cells.append(text)

        changed = self._change_flags(cells)
        for index, row in enumerate(rows):
            tags = ["chg"] if changed[index] else []
            if index % 2:
                tags.append("odd")
            tree.insert("", "end", values=row, tags=tuple(tags))
        if any(changed):
            self._schedule_fade(tree)

    def _hex_grid(self) -> bool:
        """十六进制地址时，网格按 16 格一行。

        这样行首地址的末位等于列头，第 3 列就是 0x_3，不用在脑子里做
        「0x0003 + 5 = 0x0008」的加法 —— 十六进制本来就是 16 的进制。
        """
        return bool(self._addr_hex.get()) and not self.plc_var.get()

    def _fill_master_grid(self, values: list, address: int) -> None:
        tree = self.result_grid
        fc = self._current_fc()
        per = 16 if self._hex_grid() else 10

        if fc in (1, 2):
            # 位区：●/○ 本身就是最好的网格观感，进制选择不参与
            self._emit_cells(tree, values, address, fc, 10, 1, 40, "center",
                             lambda b: "●" if b else "○")
            return

        fmt = self._grid_fmt()
        if fmt == "HEX":
            self._emit_cells(tree, values, address, fc, per, 1, 66, "e",
                             lambda r: f"{r:04X}")
            return
        if fmt == "BIN":
            # 分组后一格 19 个字符，每行 4 格仍不必横向滚动
            self._emit_cells(tree, values, address, fc, 4, 1, 150, "w",
                             self._bin_text)
            return

        # 值：按类型解码；网格里不放单位，纯数字更像仪表
        dtype = self._current_dtype()
        try:
            scale = float(self.scale_var.get() or 1)
        except ValueError:
            scale = 1.0
        step = span(dtype)
        decoded = decode(values, dtype, self.byteorder_box.get() or "ABCD", scale, 0.0)
        if dtype == "ascii":
            # 一格两个字符，74px 太浪费；左对齐才像字符串
            self._emit_cells(tree, decoded, address, fc, per, 1, 44, "w",
                             lambda v: format_value(v, dtype))
            return
        self._emit_cells(tree, decoded, address, fc, per, step, 74, "e",
                         lambda v: format_value(v, dtype))

    def _emit_cells(self, tree, values: list, address: int, fc: int, per: int,
                    step: int, width: int, anchor: str, render) -> None:
        """按每行 per 格排版，并把首行左侧补空，让行首地址落在 per 的整数倍上。

        起始地址 0x0003 → 行首 0x0000、第一个值落在列头 3；十进制起始 5 →
        行首 0、第一个值落在列头 5。两种进制都是「行首 + 列头 = 地址」。
        """
        # 行首地址对齐到 per 的整数倍：这样「格子地址 = 行首 + 列头」，
        # 而列头就是地址的末位，不用再心算「5 + 2 = 7」。
        pad = address % per
        cells = [""] * pad + [render(v) for v in values]
        base = address - pad
        rows = (len(cells) + per - 1) // per

        self._grid_per = per
        self._grid_pad = pad
        structure_changed = self._setup_grid(tree, per, width, anchor)
        labels = [self._addr_text(base + r * per * step, fc) for r in range(rows)]
        self._emit_grid_rows(tree, per, cells, labels, structure_changed)

    def _setup_grid(self, tree, per: int, width: int, anchor: str) -> bool:
        """按进制重排网格列：第 0 列是行头地址，其后 per 个数据格。

        只在列结构/列宽真的变化时才 `configure(columns=...)`：
        Tk 会因此丢弃所有列状态并重排整表，是单次刷新里最贵的动作，
        而它原来在每个轮询周期（最快 50ms）都被调用一次。
        返回 True 表示结构变了，调用方需要整表重建。
        """
        signature = (per, width, anchor)
        if getattr(tree, "_grid_signature", None) == signature:
            return False
        tree._grid_signature = signature
        tree.configure(columns=("addr",) + tuple(f"c{i}" for i in range(per)))
        tree.heading("addr", text="地址", anchor="w")
        tree.column("addr", width=74, minwidth=60, anchor="w", stretch=False)
        for i in range(per):
            tree.heading(f"c{i}", text=(format(i, "X") if per == 16 else str(i)),
                         anchor=anchor)
            # 窗口拉宽时让格子平分多出来的宽度 —— 否则右侧留一大条空白，
            # 数据反而挤在左边一小片，跟"数据区是主角"的用意相反。
            tree.column(f"c{i}", width=width, minwidth=40, anchor=anchor, stretch=True)
        return True

    def _emit_grid_rows(self, tree, per: int, cells: list, labels: list,
                        structure_changed: bool = False) -> None:
        """一维 cell 序列切成每行 per 格。

        行数没变时只改变化的格子（`tree.set`）：轮询时整表 delete+insert
        会把选中行、滚动位置和列宽一起丢掉，值没变也是满屏重排。
        """
        rows = (len(cells) + per - 1) // per
        changed = self._change_flags([str(c) for c in cells])
        existing = tree.get_children()
        same_shape = (not structure_changed and len(existing) == rows
                      and list(existing) == [str(r) for r in range(rows)])

        if same_shape:
            for r in range(rows):
                iid = str(r)
                if tree.set(iid, "addr") != labels[r]:
                    tree.set(iid, "addr", labels[r])
                for c in range(per):
                    pos = r * per + c
                    want = str(cells[pos]) if pos < len(cells) else ""
                    if tree.set(iid, f"c{c}") != want:
                        tree.set(iid, f"c{c}", want)
                tags = []
                if any(changed[r * per:(r + 1) * per]):
                    tags.append("chg")
                if r % 2:
                    tags.append("odd")
                if tuple(tags) != tuple(tree.item(iid, "tags")):
                    tree.item(iid, tags=tuple(tags))
        else:
            keep_y = tree.yview()[0]
            keep_x = tree.xview()[0]
            tree.delete(*existing)
            for r in range(rows):
                chunk = cells[r * per:(r + 1) * per]
                chunk += [""] * (per - len(chunk))
                tags = []
                if any(changed[r * per:(r + 1) * per]):
                    tags.append("chg")
                if r % 2:
                    tags.append("odd")
                tree.insert("", "end", iid=str(r), values=(labels[r], *chunk),
                            tags=tuple(tags))
            tree.yview_moveto(keep_y)
            tree.xview_moveto(keep_x)
        if any(changed):
            self._schedule_fade(tree)

    # -- 迷你趋势条 -------------------------------------------------------

    def _set_trend_key(self, key: int) -> None:
        self._trend_key = key
        self._trend.clear()
        if self._last_read is not None:
            values, address = self._last_read
            if address <= key < address + len(values):
                self._trend.append(float(values[key - address]))
        self._draw_trend()

    def _push_trend(self, values: list, address: int) -> None:
        key = self._trend_key
        if key is None or not (address <= key < address + len(values)):
            return
        self._trend.append(float(values[key - address]))
        self._draw_trend()

    def _draw_trend(self) -> None:
        canvas = self.trend_canvas
        canvas.delete("all")
        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))
        pad = 3
        data = list(self._trend)
        if len(data) < 2:
            canvas.create_text(width / 2, height / 2, text="趋势 —",
                               fill=MUTED, font=("Microsoft YaHei UI", 7))
            return
        low, high = min(data), max(data)
        carve = (high - low) or 1.0
        count = len(data)
        points: list[float] = []
        for index, value in enumerate(data):
            x = pad + (width - 2 * pad) * index / (count - 1)
            y = height - pad - (height - 2 * pad) * (value - low) / carve
            points.extend((x, y))
        canvas.create_line(*points, fill=ACCENT, width=1.4)
        canvas.create_oval(points[-2] - 1.8, points[-1] - 1.8,
                           points[-2] + 1.8, points[-1] + 1.8,
                           fill=ACCENT, outline="")

    def _cell_step(self) -> int:
        """表格里一格代表几个寄存器：HEX/BIN/位区一格就是一个。"""
        fc = self._current_fc()
        if fc in (1, 2) or self._grid_fmt() in ("HEX", "BIN"):
            return 1
        return span(self._current_dtype())

    def _cell_at(self, tree, event) -> tuple[int, int] | None:
        """事件落在哪个格子 → (格子序号, 该格首寄存器的协议地址)。"""
        if self._last_read is None:
            return None
        _, address = self._last_read
        row_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not row_id or column in ("#1", ""):
            return None
        step = self._cell_step()
        if tree is self.result_grid:
            if not self._master_grid:
                return None
            # #1 是地址列，数据格从 #2 开始 —— 少减这一位，整列会错开一格
            data_col = int(column[1:]) - 2
            index = int(row_id) * self._grid_per + data_col - self._grid_pad
            if index < 0:                  # 首行左侧的留空格
                return None
        else:
            index = int(tree.index(row_id))
        return index, address + index * step

    def _on_table_click(self, event) -> None:
        """点中一个格子 → 这格的历史画到趋势条上。"""
        tree = event.widget
        hit = self._cell_at(tree, event)
        if hit is None:
            return
        _, key = hit
        values, address = self._last_read
        if address <= key < address + len(values):
            self._set_trend_key(key)

    def _on_table_motion(self, event) -> None:
        """鼠标扫过格子时，把「这是哪个寄存器、值多少」报到状态栏。"""
        tree = event.widget
        hit = self._cell_at(tree, event)
        if hit is None:
            self._set_cursor_hint(None)
            return
        index, address = hit
        row_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        try:
            name = tree["columns"][int(column[1:]) - 1]
            text = str(tree.set(row_id, name))
        except Exception:
            text = ""
        if not text:
            self._set_cursor_hint(None)
            return
        fc = self._current_fc()
        self._set_cursor_hint(f"{self._addr_text(address, fc)} = {text}")

    def _set_cursor_hint(self, text: str | None) -> None:
        label = getattr(self, "cursor_label", None)
        if label is None:
            return
        label.configure(text=text or "")

    # -- 右键复制 ---------------------------------------------------------

    def _install_copy_menu(self, tree) -> None:
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="复制该格", command=lambda: self._copy_cell(tree))
        menu.add_command(label="复制整表（TSV）", command=lambda: self._copy_table(tree))
        tree._copy_menu = menu       # 留个引用，否则菜单被回收就弹不出来

    def _on_table_menu(self, event):
        tree = event.widget
        row = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if row:
            tree.selection_set(row)
        self._menu_cell = (tree, row, column)
        menu = getattr(tree, "_copy_menu", None)
        if menu is not None:
            menu.tk_popup(event.x_root, event.y_root)
            menu.grab_release()
        return "break"

    def _copy_cell(self, tree) -> None:
        cell = getattr(self, "_menu_cell", None)
        if not cell or cell[0] is not tree:
            return
        _, row, column = cell
        if not row or not column:
            return
        values = tree.item(row, "values")
        try:
            index = int(column[1:]) - 1
        except ValueError:
            return
        if 0 <= index < len(values):
            text = str(values[index])
            self._to_clipboard(text)
            self._notify(f"已复制：{text}")

    def _copy_table(self, tree) -> None:
        columns = tree["columns"]
        lines = ["\t".join(str(tree.heading(col, "text")) for col in columns)]
        for iid in tree.get_children():
            lines.append("\t".join(str(v) for v in tree.item(iid, "values")))
        if len(lines) <= 1:
            self._notify("表里还没有数据", error=True)
            return
        self._to_clipboard("\n".join(lines))
        self._notify(f"已复制 {len(lines) - 1} 行（TSV，可直接粘进 Excel）")

    def _to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # -- 双击：线圈取反 ---------------------------------------------------

    def _on_table_double(self, event):
        """双击单元格：线圈取反、保持寄存器就地改值。"""
        tree = event.widget
        hit = self._cell_at(tree, event)
        if hit is None:
            return None
        index, key = hit
        fc = self._current_fc()
        if fc in (2, 4):
            which = "离散输入（FC02）" if fc == 2 else "输入寄存器（FC04）"
            self._notify(f"{which}是只读的，要改请切到保持寄存器（FC03）", error=True)
            return "break"
        if fc == 1:
            values, address = self._last_read
            if not (address <= key < address + len(values)):
                return "break"
            target = 0 if values[key - address] else 1
            self._toggle_coil(key, target)
            return "break"
        return self._edit_master_cell(tree, event, key)

    def _edit_master_cell(self, tree, event, address: int):
        """在当前格子上开一个行内编辑器，回车即写到这个地址。

        不用先去「功能码 / 地址 / 值」里改一遍 —— 想改哪个数值，就在哪个格子上改。
        float32 这类跨两个寄存器的，改一格就是写整段。
        """
        if self.client is None:
            self._notify("尚未连接，无法写入", error=True)
            return "break"
        row_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not row_id or not column:
            return None
        try:
            name = tree["columns"][int(column[1:]) - 1]
            text = str(tree.set(row_id, name))
        except Exception:
            return None
        # 明细视图只允许改「值」列，HEX/BIN 列是给人核对的
        if tree is self.result_tree and name != "dec":
            return None
        if not text:
            return "break"
        unit = self.unit_text_var.get().strip()
        if unit and text.endswith(unit):
            text = text[:-len(unit)].strip()
        bbox = tree.bbox(row_id, column)
        if not bbox:
            return None
        self._spawn_cell_editor(
            tree, row_id, column, bbox, address, text, "dec",
            on_commit=lambda _i, value, _f: self._write_cell_async(address, value),
            commit_on_blur=False,
        )
        return "break"

    def _register_at(self, address: int) -> int | None:
        """当前视野里某个地址的原始寄存器值（就地编辑改字符时要用）。"""
        if self._last_read is None:
            return None
        values, base = self._last_read
        index = address - base
        if 0 <= index < len(values):
            return int(values[index])
        return None

    def _write_cell_async(self, address: int, text: str) -> None:
        """把某个格子改成 text：按当前类型编码，然后整屏回读确认。"""
        client = self._require_client()
        if client is None:
            return
        text = _normalize_number(text)
        fmt = self._grid_fmt()
        try:
            if fmt == "HEX":
                raw = _parse_int(text)
                if not 0 <= raw <= 0xFFFF:
                    raise ValueError("超出 0 ~ 0xFFFF")
                payload, write_fc = [raw], 6
            elif fmt == "BIN":
                raw = int(text.replace(" ", ""), 2)
                if not 0 <= raw <= 0xFFFF:
                    raise ValueError("超出 16 位范围")
                payload, write_fc = [raw], 6
            elif self._current_dtype() == "ascii":
                # ASCII 一格就是一个寄存器（两个字符）。改一格只改这一格 ——
                # 想写整串字符串请用工具条的「写入」，那样地址和长度都看得见。
                if len(text) > 2:
                    raise ValueError(
                        "ASCII 一格只放 2 个字符；写长字符串请用工具条的「写入」")
                original = self._register_at(address)
                if original is None:
                    raise ValueError("这格不在当前读取范围内，请先读一次再改")
                payload = [patch_ascii(text, original,
                                       self.byteorder_box.get() or "ABCD")]
                write_fc = 6
            else:
                dtype = self._current_dtype()
                try:
                    scale = float(self.scale_var.get() or 1)
                except ValueError:
                    scale = 1.0
                # 整数必须先走整数解析：`float(text)` 只有 53 位尾数，
                # 就地改 uint64/int64 会把低位静默改掉（codec.encode 里
                # 特意绕开了 float，这里再转一次等于把防护绕过去）。
                if is_integer(dtype):
                    raw_value = _parse_int(text)
                else:
                    raw_value = float(text)
                    if not math.isfinite(raw_value):
                        raise ValueError("值必须是有限数，NaN / ∞ 不能写入")
                payload = encode([raw_value], dtype,
                                 self.byteorder_box.get() or "ABCD", scale, 0.0)
                write_fc = 16 if len(payload) > 1 else 6
        except (TypeError, ValueError) as exc:
            self._notify(f"值无效：{exc}", error=True)
            return

        self._begin_request(f"写入地址 {address} …")

        def done(result, error):
            self._end_request()
            if error:
                reason = self._friendly_error(error)
                self._notify(f"写入失败：{reason}", error=True)
                self._mark_link(False)
                self._refresh_stats()
                return
            self._mark_link(True)
            counted, noun, bytes_ = self._write_extent(write_fc, len(payload))
            self._log(f"就地写入：地址 {address} ← {text} → "
                      f"{counted} {noun}（{bytes_} 字节）{self._hex_list(payload)}", "ok")
            self._notify(f"已写入 地址 {address} = {text} · "
                         f"{counted} {noun}，正在回读")
            self._refresh_stats()
            self._mark_written(address, len(payload))
            # 整屏回读，确认真的落到了从站；把写入摘要带过去，别被读结果冲掉
            self._do_read_async(f"已写入 地址 {address} = {text} · {counted} {noun}")

        self._async(lambda: client.write(write_fc, address, payload), done)

    def _toggle_coil(self, address: int, value: int) -> None:
        client = self._require_client()
        if client is None:
            return
        self._begin_request(f"写线圈 地址 {address} → {value} …")

        def done(result, error):
            self._end_request()
            if error:
                reason = self._friendly_error(error)
                self._notify(f"写线圈失败：{reason}", error=True)
                self._mark_link(False)
                self._refresh_stats()
                return
            self._mark_link(True)
            self._notify(f"线圈 地址 {address} 已置 {value}，正在回读确认")
            self._refresh_stats()
            self._do_read_async()          # 整屏回读，比只读一个点有用

        self._async(lambda: client.write(5, address, [value]), done)


    # -- 视图切换（网格 ↔ 明细），tests 也走这两个入口 -------------------

    def _on_display_change(self) -> None:
        """换格式/换地址记法：这一屏整体变了，不算「数据变化」。"""
        self._sync_view_state()
        self._prev_cells = None
        self._render_master_data()
        if getattr(self, "stats_label", None) is not None:
            self._refresh_stats()

    def _view_summary(self) -> str:
        """一句话说清「现在这些数是怎么解释出来的」。

        类型/字节序/系数/单位搬进弹层之后，必须在这里留个回声 ——
        否则用户看到 220.5 却不知道是按 float32-CDAB 还是 ×0.1 算的。
        """
        dtype = self._current_dtype()
        parts = [DTYPE_SHORT.get(dtype, dtype)]
        if span(dtype) > 1:
            parts.append(self.byteorder_box.get() or "ABCD")
        try:
            scale = float(self.scale_var.get() or 1)
        except ValueError:
            scale = 1.0
        if scale != 1:
            parts.append(f"×{scale:g}")
        unit = self.unit_text_var.get().strip()
        if unit:
            parts.append(unit)
        if self.plc_var.get():
            parts.append("PLC 址")
        return " ".join(parts)

    def _sync_view_state(self) -> None:
        """把「数据解释」的当前取值存下来，弹层重建时恢复。

        单独一个方法：以前这段写在 `_view_summary()` 里，于是一个叫
        summary 的纯读取函数顺带改状态 —— 任何新增的「读摘要」调用点
        都会悄悄刷新快照，改起来没人敢动。
        """
        self._view_state = {
            "addr_hex": bool(self._addr_hex.get()),
            "dtype": self.dtype_box.get(),
            "byteorder": self.byteorder_box.get(),
            "scale": self.scale_var.get(),
            "unit": self.unit_text_var.get(),
            "plc": self.plc_var.get(),
        }

    def _refresh_stats(self) -> None:
        summary = self._view_summary()
        client = self.client
        if client is None:
            self.stats_label.configure(text=summary)
            return
        latency = client.last_latency_ms
        # request_count 只记「收到应答」的次数，error_count 记失败尝试（含重试），
        # 所以失败数可能比成功数大 —— 那说明链路在抖，正是要看见的信息。
        parts = [summary, f"成功 {client.request_count}", f"失败 {client.error_count}"]
        if latency is not None:
            parts.append(f"{latency:.1f} ms")
        self.stats_label.configure(text=" · ".join(parts))

    def _toggle_master_view(self) -> None:
        self.set_master_view(not self._master_grid)

    def set_master_view(self, grid: bool) -> None:
        self._master_grid = grid
        self._prev_cells = None
        if grid:
            self.detail_wrap.grid_remove()
            self.grid_wrap.grid(row=0, column=0, sticky="nsew")
            self.master_view_btn.configure(text="明细")
            self.master_fmt_box.configure(state="readonly")
        else:
            self.grid_wrap.grid_remove()
            self.detail_wrap.grid(row=0, column=0, sticky="nsew")
            self.master_view_btn.configure(text="网格")
            # 明细视图的「值」列永远是物理量，格式下拉在这里没有意义。
            # 只置灰不重置，会让控件带着 HEX/BIN 的旧值回到网格视图。
            self.master_fmt_box.set("值")
            self.master_fmt_box.configure(state="disabled")
        self._render_master_data()

    def _build_slave_view_actions(self, head) -> None:
        self.slave_view_btn = ttk.Button(head, text="明细", width=6,
                                         command=self._toggle_slave_view)
        self.slave_view_btn.pack(side="right")
        self.slave_fmt_box = ttk.Combobox(head, values=("十进制", "有符号", "HEX", "BIN"),
                                          state="readonly", width=6)
        self.slave_fmt_box.set("十进制")
        self.slave_fmt_box.pack(side="right")
        ttk.Label(head, text="进制", style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self.slave_fmt_box.bind("<<ComboboxSelected>>",
                                lambda e: self._refresh_slave_table())

    def _toggle_slave_view(self) -> None:
        self.set_slave_view(not self._slave_grid)

    def set_slave_view(self, grid: bool) -> None:
        self._slave_grid = grid
        if grid:
            self.slave_detail_wrap.grid_remove()
            self.slave_grid_wrap.grid(row=0, column=0, sticky="nsew")
            self.slave_view_btn.configure(text="明细")
        else:
            self.slave_grid_wrap.grid_remove()
            self.slave_detail_wrap.grid(row=0, column=0, sticky="nsew")
            self.slave_view_btn.configure(text="网格")
        self._slave_sig = None            # 强制按新视图/进制重建
        self._refresh_slave_table()

    def _do_write_async(self) -> None:
        client = self._require_client()
        if client is None:
            return
        fc = self._current_fc()
        if fc not in (5, 6, 15, 16):
            self._notify("当前功能码是读操作，请点“读取”", error=True)
            return
        values = self._parse_write_values(fc)
        if values is None:
            return
        try:
            address = self._resolve_address(
                self._int_field(self.addr_var, "起始地址", 0, 65535), fc)
        except (ValueError, AddressError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return
        if fc in (5, 6) and len(values) != 1:
            self._notify("单点写入只能填一个值", error=True)
            return

        if fc in (5, 15):
            payload = values
        else:
            # 用户填的是物理量（比如 220.5），这里负责编码成寄存器序列
            try:
                scale = float(self.scale_var.get() or 1)
            except ValueError:
                scale = 1.0
            try:
                payload = encode(values, self._current_dtype(),
                                 self.byteorder_box.get() or "ABCD", scale, 0.0)
            except ValueError as exc:
                self._notify(str(exc), error=True)
                return
            if fc == 6 and len(payload) != 1:
                self._notify(f"{self._current_dtype()} 要占 {len(payload)} 个寄存器，"
                             f"单点写放不下，请改用「写多寄存器」", error=True)
                return

        def done(result, error):
            self._end_request()
            if error:
                self._notify(f"写入失败：{self._friendly_error(error)}", error=True)
                self._mark_link(False)
                self._refresh_stats()
                return
            self._mark_link(True)
            counted, noun, bytes_ = self._write_extent(fc, len(payload))
            self.master_status.configure(
                text=(f"已写入 FC{fc:02d} 地址 {address} · "
                      f"{counted} {noun}（{bytes_} 字节）"), foreground=OK)
            self._log(f"写入成功：地址 {address} ← {self._write_brief(values)} "
                      f"→ {counted} {noun} {self._hex_list(payload)}", "ok")
            self._refresh_stats()
            self._mark_written(address, len(payload))
            self._readback(fc, address, len(payload),
                           f"已写入 {counted} {noun}（{bytes_} 字节）")

        counted, noun, _ = self._write_extent(fc, len(payload))
        self._begin_request(f"写入中… {counted} {noun}")
        self._async(lambda: client.write(fc, address, payload), done)

    @staticmethod
    def _write_extent(fc: int, payload_len: int) -> tuple[int, str, int]:
        """这次写到底占多少：(数量, 量词, 字节数)。

        线圈按位算字节（8 个一字节），寄存器按 2 字节算 —— 这是 Modbus 帧里
        「字节数」字段的定义，界面上就该用同一套口径说话。
        """
        if fc in (5, 15):
            return payload_len, "个线圈", (payload_len + 7) // 8
        return payload_len, "个寄存器", payload_len * 2

    @staticmethod
    def _hex_list(payload: list) -> str:
        """寄存器值用十六进制列出：0x4148 0x0000 比 16799 0 好核对。"""
        if not payload:
            return "[]"
        if isinstance(payload[0], bool):
            return "[" + " ".join("1" if v else "0" for v in payload) + "]"
        return "[" + " ".join("0x%04X" % int(v) for v in payload) + "]"

    def _write_brief(self, values: list) -> str:
        """「220.5（f32 ABCD ×1）」—— 值加解释方式，回看日志时不用猜。"""
        dtype = self._current_dtype()
        if dtype == "ascii":
            text = "".join(str(v) for v in values)
            order = self.byteorder_box.get() or "ABCD"
            return f'"{text}"（ASCII {order}）'
        shown = ", ".join(format_value(v, dtype) if isinstance(v, float) else str(v)
                          for v in values)
        if len(values) > 4:
            shown = (", ".join(format_value(v, dtype) for v in values[:4])
                     + f" …（共 {len(values)} 个）")
        kind = DTYPE_SHORT.get(dtype, dtype)
        try:
            scale = float(self.scale_var.get() or 1)
        except ValueError:
            scale = 1.0
        extra = f" {kind}"
        if span(dtype) > 1:
            extra += f" {self.byteorder_box.get() or 'ABCD'}"
        if scale != 1:
            extra += f" ×{scale:g}"
        return f"{shown}（{extra.strip()}）"

    def _readback(self, write_fc: int, address: int, count: int, note: str = "") -> None:
        """写完回读一次，确认数据真的落到了从站里。

        回读必须用对应的**读**功能码 —— 拿写功能码去读，从站会直接回异常。
        """
        client = self.client
        read_fc = READBACK_FC.get(write_fc)
        if client is None or read_fc is None:
            return
        limit = read_limit(read_fc) or 125
        length = min(max(1, count), limit)
        read_from = address
        # 当前表格已经读了一段更宽的范围时，按原范围回读 ——
        # 否则写完一格，屏幕上 30 个寄存器会突然缩成 6 个，像丢了数据。
        if self._last_read is not None:
            old_values, old_addr = self._last_read
            if old_addr <= address and address + count <= old_addr + len(old_values):
                read_from = old_addr
                length = min(len(old_values), limit)

        def done(values, error):
            if error:
                self._notify(f"写入已发出，但回读失败：{self._friendly_error(error)}", error=True)
                self._mark_link(False)
                self._refresh_stats()
                return
            self._mark_link(True)
            self._refresh_stats()
            # 必须按**回读的真实起点**渲染：回读可能为了保住原视野而从更早的
            # 地址开始（上面 read_from），拿写入地址当基址会让整屏地址标签
            # 整体偏移，而 _last_read 一旦带错基址，后面的就地编辑会写错寄存器。
            self._render_values(values, read_from)
            latency = client.last_latency_ms
            extra = f" · {latency:.1f} ms" if latency is not None else ""
            if note:
                self._notify(f"{note} · 已回读确认{extra}")
            else:
                self._notify(f"FC{read_fc:02d} 地址 {address} · "
                             f"回读 OK {len(values)} 个寄存器{extra}")

        self._async(lambda: client.read(read_fc, read_from, length), done)

    def _toggle_poll(self) -> None:
        if self._polling:
            self._stop_poll()
            return
        client = self._require_client()
        if client is None:
            return
        fc = FC_OPTIONS[self.fc_box.current()][1]
        if fc not in (1, 2, 3, 4):
            self._notify("轮询只能用于读操作", error=True)
            return
        self._polling = True
        self._poll_failures = 0
        self.poll_btn.configure(text="停止轮询")
        self._log("开始轮询")
        self._poll_tick()

    def _stop_poll(self) -> None:
        if not self._polling:
            return
        self._polling = False
        self.poll_btn.configure(text="开始轮询")
        self._log("轮询已停止")

    def _poll_tick(self) -> None:
        if not self._polling or self.client is None:
            return
        fc = self._current_fc()
        try:
            address = self._resolve_address(
                self._int_field(self.addr_var, "起始地址", 0, 65535), fc)
            # 上限按功能码走：FC03 是 125，FC01 才是 2000。
            # 原来一律按 2000 放行，填 200 个寄存器开始轮询后每 500ms
            # 报一次同样的「数量必须在 1~125 之间」，还得手动按 Esc 才停。
            count = self._int_field(self.count_var, "数量", 1, read_limit(fc) or 125)
            interval = max(50, self._int_field(self.interval_var, "轮询间隔", 50, 60000))
        except (ValueError, AddressError):
            self._stop_poll()
            return

        def done(values, error):
            if error:
                self._poll_failures += 1
                self.master_status.configure(
                    text=f"轮询失败：{self._friendly_error(error)}", foreground=ERR)
                self._mark_link(False)
                if self._poll_failures >= 5:
                    # 连续 5 次同样的失败（多为参数错误）再轮下去只是刷屏，
                    # 停掉并把原因留在状态栏，比让用户自己按 Esc 更明确。
                    self._stop_poll()
                    self.master_status.configure(
                        text=f"轮询已自动停止（连续 {self._poll_failures} 次失败）："
                             f"{self._friendly_error(error)}", foreground=ERR)
                    return
            else:
                self._poll_failures = 0
                self._mark_link(True)
                if not self._editing:      # 正在就地编辑时别重画，否则输入框会被顶走
                    self._render_values(values, address)
                latency = self.client.last_latency_ms if self.client else None
                extra = f" · {latency:.1f} ms" if latency is not None else ""
                self.master_status.configure(
                    text=f"轮询中 · 地址 {address} 共 {len(values)} 点{extra}", foreground=MUTED)
            self._refresh_stats()
            if self._polling:
                self.root.after(interval, self._poll_tick)

        self._async(lambda: self.client.read(fc, address, count), done)

    # ------------------------------------------------------------ 从站逻辑

    def _toggle_server(self) -> None:
        if self.server is not None and self.server.running:
            server, self.server = self.server, None
            self._async(server.stop, lambda r, e: self._log("从站已停止"))
            self.slave_btn.configure(text="启动从站", style="Accent.TButton")
            self.slave_stat.configure(text="已停止")
            self.tabs.set_badge(1, False)
            return

        if self._busy:
            return
        try:
            unit = self._int_field(self.slave_unit, "从站站号", 1, 247)
            size = self._int_field(self.slave_size, "寄存器数量", 1, 65536)
            store = DataStore(size=size)
            if self.slave_mode.get() == "tcp":
                server = TcpServer(
                    host=self.slave_host.get().strip() or "0.0.0.0",
                    port=self._int_field(self.slave_port, "监听端口", 0, 65535),
                    store=store, unit=unit, on_log=self._log,
                )
            else:
                if not HAS_SERIAL:
                    raise RuntimeError("RTU 模式需要 pyserial：pip install pyserial")
                server = RtuServer(
                    port=self.slave_serial.get().strip(),
                    baudrate=int(self.slave_baud.get()),
                    bytesize=int(self.slave_databits.get()),
                    parity=self.slave_parity.get(),
                    stopbits=float(self.slave_stopbits.get()),
                    store=store, unit=unit, on_log=self._log,
                )
        except (ValueError, RuntimeError, TransportError) as exc:
            self.slave_stat.configure(text=f"启动失败：{exc}")
            self._log(f"无法启动从站：{exc}", "err")
            return

        self._busy = True
        self.slave_btn.configure(state="disabled", text="启动中…")

        def done(result, error):
            self._busy = False
            self.slave_btn.configure(state="normal")
            if error:
                self.slave_btn.configure(text="启动从站")
                self.slave_stat.configure(text=f"启动失败：{error}")
                self._log(f"从站启动失败：{error}", "err")
                return
            self.server = server
            self.slave_btn.configure(text="停止从站", style="Danger.TButton")
            self.slave_stat.configure(text=f"运行中 · 端口 {server.port} · 站 {unit}")
            self._log(f"从站已启动（{server.port}）", "ok")
            self.tabs.set_badge(1, True)      # 在「从站」上点一颗绿灯，主站页也看得见它在跑
            self._refresh_slave_table()

        self._async(server.start, done)

    def _current_area(self) -> str:
        return AREA_OPTIONS[self.area_box.current()][1]

    def _refresh_slave_table(self) -> None:
        if self.server is None or self._editing:
            return
        store = self.server.store
        area = self._current_area()
        try:
            start = max(0, int(self.view_start.get()))
            count = max(1, int(self.view_count.get()))
        except ValueError:
            start, count = 0, 32
        start = min(start, store.size - 1)
        count = min(count, store.size - start)

        # 批量读：一格一次 get() 就是 500 次加锁 + 500 次信号扫描，每 800ms 一轮
        values = store.read_area(area, start, count)
        is_bit = area in ("coils", "discrete_inputs")
        # 进制只在网格视图有意义；位区 ●/○ 已经是最佳显示，也不参与
        fmt = "" if (is_bit or not self._slave_grid) else self.slave_fmt_box.get()
        self.slave_fmt_box.configure(
            state="readonly" if (self._slave_grid and not is_bit) else "disabled")

        signature = (area, start, count, self._slave_grid, fmt, tuple(values))
        if signature == self._slave_sig:
            return                            # 一个字节都没变，连重画都不做

        if self._slave_grid:
            self._fill_slave_grid(start, values, is_bit, fmt)
        else:
            self._fill_slave_detail(start, values, is_bit)
        self._slave_sig = signature

    def _fill_slave_detail(self, start: int, values: list, is_bit: bool) -> None:
        tree = self.slave_tree

        signed = self.slave_fmt_box.get() == "有符号"

        def row_of(offset: int, value) -> tuple:
            index = start + offset
            if is_bit:
                return (index, "1" if value else "0", "01" if value else "00",
                        "●" if value else "○")
            shown = as_signed16(value) if signed else value
            return (index, shown, hex_of([value]), bin_of([value]))

        existing = tree.get_children()
        same_rows = [str(start + offset) for offset in range(len(values))]
        if list(existing) == same_rows:
            # 行集合没变，只改值。整表 delete+insert 会让画面闪一下，
            # 还会把选中行和滚动条位置一起丢掉。
            for offset, value in enumerate(values):
                iid = same_rows[offset]
                wanted = tuple(str(v) for v in row_of(offset, value))
                if tuple(str(v) for v in tree.item(iid, "values")) != wanted:
                    tree.item(iid, values=wanted)
        else:
            keep = tree.yview()[0]            # 换页或改数量时才重建，先记住滚动位置
            tree.delete(*existing)
            for offset, value in enumerate(values):
                tree.insert("", "end", iid=same_rows[offset], values=row_of(offset, value),
                            tags=("odd",) if offset % 2 else ())
            tree.yview_moveto(keep)

    def _fill_slave_grid(self, start: int, values: list, is_bit: bool, fmt: str) -> None:
        tree = self.slave_grid
        if is_bit:
            per, width, anchor = 10, 40, "center"
            cells = ["●" if v else "○" for v in values]
        elif fmt == "HEX":
            per, width, anchor = 10, 66, "e"
            cells = [hex_of([v], prefix=False) for v in values]
        elif fmt == "BIN":
            per, width, anchor = 4, 128, "w"
            cells = [bin_of([v]) for v in values]
        elif fmt == "有符号":
            per, width, anchor = 10, 66, "e"
            cells = [str(as_signed16(v)) for v in values]
        else:
            per, width, anchor = 10, 66, "e"
            cells = [str(v) for v in values]
        # 和主站一致：行首对齐到 per 的整数倍，列头就是地址末位
        pad = start % per
        base = start - pad
        cells = [""] * pad + cells
        self._slave_grid_per = per            # 双击定位地址要用
        self._slave_grid_pad = pad
        self._slave_base = base               # 行首基准（per 的整数倍）
        self._slave_start = start             # 真实起始地址，用于判断留空格
        self._setup_grid(tree, per, width, anchor)

        rows = (len(cells) + per - 1) // per
        existing = tree.get_children()
        same_rows = [str(r) for r in range(rows)]
        if list(existing) == same_rows:
            # 行集合没变：只原地改有变化的格子，不闪屏、不丢滚动位置
            for r in range(rows):
                iid = same_rows[r]
                want_addr = str(base + r * per)
                if tree.set(iid, "addr") != want_addr:
                    tree.set(iid, "addr", want_addr)
                for c in range(per):
                    pos = r * per + c
                    want = cells[pos] if pos < len(cells) else ""
                    if tree.set(iid, f"c{c}") != want:
                        tree.set(iid, f"c{c}", want)
        else:
            keep = tree.yview()[0]
            tree.delete(*existing)
            for r in range(rows):
                chunk = cells[r * per:(r + 1) * per]
                chunk += [""] * (per - len(chunk))
                tree.insert("", "end", iid=str(r),
                            values=(str(base + r * per), *chunk),
                            tags=("odd",) if r % 2 else ())
            tree.yview_moveto(keep)

    def _tick_slave_refresh(self) -> None:
        # 没起从站就什么都不用采；页签不在前台也不必刷新。
        if self.server is None:
            self.root.after(800, self._tick_slave_refresh)
            return
        if self.slave_grid.winfo_ismapped() or self.slave_tree.winfo_ismapped():
            try:
                self._refresh_slave_table()
            except Exception:
                pass
        self.root.after(800, self._tick_slave_refresh)

    def _tick_demo(self) -> None:
        if self.demo_var.get() and self.server is not None and self.server.running:
            store = self.server.store
            self._demo_counter = (self._demo_counter + 3) % 65536
            try:
                store.set("input_registers", 0, self._demo_counter)
                store.set("input_registers", 1, int(32767 * (1 + math.sin(self._demo_counter / 20))))
                store.set("input_registers", 2, self._demo_counter % 2)
                store.set("coils", 0, self._demo_counter % 6 < 3)
                store.set("discrete_inputs", 1, self._demo_counter % 10 < 5)
            except Exception:
                pass
        self.root.after(250, self._tick_demo)

    def _begin_cell_edit(self, event) -> None:
        tree = self.slave_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        row_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if not row_id or column != "#2" or self.server is None:
            return
        bbox = tree.bbox(row_id, column)
        if not bbox:
            return
        self._spawn_cell_editor(tree, row_id, column, bbox,
                                int(row_id), tree.set(row_id, "dec"), "dec")

    def _begin_grid_edit(self, event) -> None:
        """网格视图的双击编辑：列偏移 + 行头起始地址 = 寄存器地址。"""
        tree = self.slave_grid
        if tree.identify_region(event.x, event.y) != "cell" or self.server is None:
            return
        row_id = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        try:
            col = int(column[1:]) - 2      # #1 是行头地址列，#2 起才是数据格
        except ValueError:
            return
        if not row_id or col < 0:
            return
        bbox = tree.bbox(row_id, column)
        if not bbox:
            return
        area = self._current_area()
        # 绝对地址 = 行首基准 + 行 × 每行格数 + 列。以前少加了起始地址，
        # 起始不是 0 的时候双击会写到别的寄存器上。
        index = self._slave_base + int(row_id) * self._slave_grid_per + col
        if index < self._slave_start:      # 首行左侧的留空格
            return
        if area in ("coils", "discrete_inputs"):
            initial = "1" if tree.set(row_id, f"c{col}") == "●" else "0"
            fmt = "dec"
        else:
            initial = tree.set(row_id, f"c{col}")
            shown = self.slave_fmt_box.get()
            fmt = "dec" if shown in ("十进制", "有符号") else shown
        self._spawn_cell_editor(tree, row_id, column, bbox, index, initial, fmt)

    def _spawn_cell_editor(self, tree, row_id, column, bbox, index: int,
                           initial: str, fmt: str, on_commit=None,
                           commit_on_blur: bool = True) -> None:
        """在单元格上放一个行内 Entry，回车/失焦提交，Esc 取消。"""
        self._editing = True
        x, y, width, height = bbox
        editor = tk.Entry(tree, justify="center", font=MONO, relief="solid", borderwidth=1)
        editor.place(x=x, y=y, width=width, height=height)
        editor.insert(0, initial)
        editor.select_range(0, "end")
        editor.focus_set()
        self._editor = editor

        def commit(_event=None):
            if self._editor is None:
                return
            text = self._editor.get().strip()
            self._editor.destroy()
            self._editor = None
            self._editing = False
            if not text:
                return
            if on_commit is not None:
                on_commit(index, text, fmt)
            else:
                self._apply_cell_edit(index, text, fmt)

        editor.bind("<Return>", commit)
        editor.bind("<Escape>", lambda e: self._cancel_edit())
        # 从站是本地内存，失焦提交没什么代价；主站是往真设备写，
        # 手一抖点到别处就发一帧写命令太危险 —— 必须按回车才写。
        editor.bind("<FocusOut>", commit if commit_on_blur else (lambda e: self._cancel_edit()))

    def _cancel_edit(self) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
        self._editing = False

    def _apply_cell_edit(self, index: int, text: str, fmt: str = "dec") -> None:
        text = _normalize_number(text)
        area = self._current_area()
        store = self.server.store
        try:
            if area in ("coils", "discrete_inputs"):
                value = text.lower() in ("1", "on", "true", "yes", "●")
            elif fmt == "HEX":
                value = int(text, 16)
            elif fmt == "BIN":
                value = int(text, 2)
            else:
                value = _parse_int(text)

            # 允许填负数：寄存器本身是 16 位无符号，但现场模拟的多是 int16，
            # 填 -5 就按补码存成 0xFFFB —— 主站按 int16 读回来正好是 -5。
            if not -0x8000 <= value <= 0xFFFF:
                raise ValueError("超出 -32768 ~ 65535（int16 补码范围）")
            raw = value & 0xFFFF
            store.set(area, index, raw)
            if value < 0:
                self.slave_edit_hint.configure(
                    text=f"已改 [{index}] = {value} → 0x{raw:04X}", foreground=OK)
                self._log(f"本地修改 {area}[{index}] = {value}（按补码存 0x{raw:04X}）")
            else:
                self.slave_edit_hint.configure(text=f"已改 [{index}] = {raw}", foreground=OK)
                self._log(f"本地修改 {area}[{index}] = {raw}")
        except ValueError as exc:
            self.slave_edit_hint.configure(text=f"值无效：{text}", foreground=ERR)
            self._log(f"值无效：{text}（{exc}）", "err")
        self._refresh_slave_table()

    def _clear_slave(self) -> None:
        if self.server is None:
            self.slave_stat.configure(text="请先启动从站")
            return
        self.server.store.clear()
        self._refresh_slave_table()
        self._log("从站数据已清零")

    # ------------------------------------------------------------ 关闭

    def _on_close(self) -> None:
        self._polling = False
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self._pool.shutdown(wait=False)
        self.root.destroy()


def main() -> None:
    try:  # Windows 高 DPI，避免界面被系统放大后发虚
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    app = ModbusApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
