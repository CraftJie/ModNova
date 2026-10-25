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
import ipaddress
import math
import os
import queue
import socket
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from .addressing import AddressError, area_of_fc, display_address, resolve_address
from .applog import install_excepthook
from .client import ModbusClient
from .config import Config
from .codec import (
    DTYPE_LABEL,
    NONPRINT,
    as_signed16,
    bin_of,
    DataType,
    decode,
    encode,
    format_value,
    hex_of,
    is_integer,
    patch_ascii,
    span,
)
from .datastore import BIT_AREAS, DataStore
from .frameparser import parse_frame
from .parsing import normalize_number as _normalize_number
from .parsing import parse_int as _parse_int
from .parsing import parse_write_values, split_values as _split_values
from .pdu import (
    DEVICE_ID_OBJECTS,
    ModbusError,
    ParameterError,
    describe_error,
    describe_write_frame,
    read_limit,
    write_limit,
)
from .server import FaultConfig, RtuOverTcpServer, RtuServer, TcpServer
from .transport import HAS_SERIAL, RtuOverTcpTransport, RtuTransport, TcpTransport, TransportError, list_serial_ports
from .ui_render import icon_png, photo, png_rgba, render_rounded
from .ui_tabs import SegmentedTabs

# 连接浮层里标签列的固定宽度：TCP/RTU 两套字段靠它对齐
POP_LABEL_W = 56
from .ui_kit import Checkbutton as RoundCheck, RoundedButton, StatusPill
from .ui_theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_PRESS,
    BG,
    BORDER,
    BORDER_SOFT,
    CARD,
    CTRL_H,
    DISABLED_ARROW,
    DISABLED_BG,
    DISABLED_FG,
    ERR,
    ERR_SOFT,
    FAINT,
    FIELD_BG,
    FONT_DISPLAY,
    FONT_SECTION,
    FONT_SM,
    FONT_TITLE,
    HEAD_BG,
    HEAD_EDGE,
    HEAD_FG,
    HOVER_BG,
    LOG_CRC,
    LOG_DIR_RX,
    LOG_DIR_TX,
    LOG_FC,
    LOG_FONT,
    LOG_PAYLOAD,
    MONO,
    MUTED,
    OK,
    PAD_CARD,
    PAD_CONTAINER,
    SELECT_BG,
    SP_L,
    SP_M,
    SP_S,
    SP_XL,
    SP_XS,
    UI_FONT,
    SUNKEN,
    TEXT,
    UI_FONT,
    WARN,
)

# 工具条上的一排按钮统一这个宽度（原来是 ttk 的 11 字符最小宽，
# 换成自绘后按字数自适应，4 字按钮会比 2 字按钮窄一截）
TOOLBAR_BTN = 104

# Modbus 标准端口。它不参与「记住上次参数」：现场调试常临时连到 5020/1502
# 上的模拟器或网关，那种一次性连接如果被记住，下次开软件看到的「默认端口」
# 就不是 502 了 —— 像配置坏了，而用户根本没动过它。
DEFAULT_TCP_PORT = 502

# 日志面板最多保留多少条。
#
# 原来只留 400 行：轮询一开，几分钟前那几帧就被滚掉了，回头想对「刚才那次
# 超时前后发了什么」已经找不到。Tk 的 Text 内部是 btree，插入和裁剪的成本
# 几乎不随总行数增长 —— 实测窗口 400 行和 10 万行的稳态追加速度都是每秒
# 215 条（瓶颈在 see("end") 的重绘，跟行数无关），所以放大到 10 万只是多占
# 几十 MB 内存，不会拖慢界面。
LOG_MAX_LINES = 100_000

# 满了之后一批删多少：约 1%。每来一条就删一次虽然也不慢，但索引树会被
# 反复搅动，而且「删 79 行」这种零碎步长会让滚动位置一直在抖。
LOG_TRIM_STEP = 1_000

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
    ("22  掩码写寄存器", 22),
]

# 链路模式的显示标签 ↔ 内部键。link_mode / slave_mode 里始终存内部键
# （tcp / rtutcp / rtu），下拉框只负责显示 —— _build_transport、_toggle_server
# 和测试都只认键，标签以后想改文案不影响逻辑。
LINK_LABELS = {"tcp": "TCP", "rtutcp": "RTU over TCP", "rtu": "RTU"}
LINK_KEYS = {label: key for key, label in LINK_LABELS.items()}
SLAVE_MODE_LABELS = {"tcp": "TCP 监听", "rtutcp": "RTU over TCP 透传", "rtu": "RTU 串口"}
SLAVE_MODE_KEYS = {label: key for key, label in SLAVE_MODE_LABELS.items()}

AREA_OPTIONS = [
    ("线圈 (可读写·位)", "coils"),
    ("离散输入 (只读·位)", "discrete_inputs"),
    ("保持寄存器 (可读写)", "holding_registers"),
    ("输入寄存器 (只读)", "input_registers"),
]

# 写功能码 → 对应的读功能码，用于写完之后回读校验
READBACK_FC = {5: 1, 6: 3, 15: 1, 16: 3, 22: 3}

# 明细视图最多渲染的行数。Tk Treeview 是整表重建模型，2000 行线圈
# 每 500ms 轮询重画一次会把界面拖垮；超出部分截断并提示（概览视图
# 每 10 格才一行，不在其列）。读寄存器上限 125，天然够不到这个数。
MAX_DETAIL_ROWS = 1000

# 显示格式。三个选项统一用中文，不混排「值 / HEX / BIN」——
# 中英混排在同一个下拉里既不像同一组概念，也不便朗读；
# 十六进制带 0x 前缀后就无需再靠选项名区分进制。
FMT_VALUE = "数值"
FMT_HEX = "十六进制"
FMT_BIN = "二进制"
FMT_OPTIONS = (FMT_VALUE, FMT_HEX, FMT_BIN)

# 地址记法：同一串字符三种读法，互斥。它决定「地址框里的字是什么意思」，
# 属于参数的一部分，所以摆在地址框右边，而不是塞进「数据定义」弹层 ——
# 那个弹层回答的是「这一串 16 位数字是什么含义」，地址记法跟它混在一起，
# 会让人以为地址也是数据类型的一部分。
ADDR_NOTATIONS = (("十进制", "dec"), ("十六进制", "hex"), ("PLC 地址", "plc"))
ADDR_NOTATION_KEYS = {label: key for label, key in ADDR_NOTATIONS}


DTYPE_SHORT = {
    "uint16": "u16", "int16": "i16", "uint32": "u32", "int32": "i32",
    "float32": "f32", "uint64": "u64", "int64": "i64", "float64": "f64",
    "ascii": "ASCII",
}

# 由 codec.DTYPE_LABEL 派生（ vocabulary 单一来源 ），顺序即 DataType 枚举序：
# ASCII 殿后 —— 前 8 项的下标不要动，测试和工作流里都有按下标取值的地方。
DTYPE_OPTIONS = [(DTYPE_LABEL[t], t.value) for t in DataType]


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

        # 运行日志尽早就位：之后的每一条 _log（含报文）都会镜像进滚动文件，
        # 界面被关掉/崩掉之后，事后追查靠的就是它
        # 运行日志默认不落盘：界面看完就完，要留证据时用日志面板的「导出」。
        # self.logger 为 None 时 _log 只进界面，一行文件都不写。
        self.logger = None

        # 用户配置：连接成功后回写链路参数，下次开软件直接回填。
        # 读盘失败 / 文件损坏已在 Config 内部兜底成默认值，这里不另加 try ——
        # 配置文件坏了就打不开工具，是最恼人的失败方式。
        self.config = Config()

        self.client: ModbusClient | None = None
        self.server = None
        # -- 双视图状态：True = 概览（Modbus Poll 式网格，默认），False = 明细 --
        self._master_grid = True
        self._slave_grid = True
        self._last_read = None            # 最近一次读到的原始数据，切换视图时重画用
        self._slave_grid_per = 10         # 网格每行格数（随格式变化，双击定位地址用）
        self._slave_grid_pad = 0          # 首行左侧留空几格（行首对齐用）
        self._slave_base = 0              # 网格行首基准（per 的整数倍）
        self._slave_start = 0             # 网格真实起始地址
        self._events: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="modbus-io")
        self._polling = False
        self._poll_failures = 0
        # 切到写功能码会自动停轮询，切回读功能码要能自己接上 —— 否则用户
        # 只是看了眼线圈，回来发现轮询没了，还得手动点一下。用户主动按
        # 「停止轮询」/Esc 的不在这个范围内，那个意图要尊重。
        self._poll_resume_on_read = False
        # FC2B 探测到的设备名（给顶栏链路胶囊用），断开时清掉
        self._device_note = ""
        self._busy = False
        self._request_active = False
        self._request_seq = 0
        self._editing = False
        self._editor: tk.Entry | None = None
        self._demo_counter = 0
        self._log_lines = 0
        self._log_paused = False
        self._log_held: list[tuple[str, str]] = []   # 暂停期间的条目缓存，恢复时补记
        self._slave_sig = None          # 上一次从站表格的内容指纹，用来跳过无变化的刷新
        # -- 链路健康：站号、连续失败次数、状态（on / warn / down）--
        self._link_text = ""            # 例如 "TCP 127.0.0.1:15040"
        self._link_unit = 1
        self._link_health = "off"
        self._fail_streak = 0
        self._conn_note = "未连接"       # 连接失败时的原因，直接显示在顶栏
        # -- 变化高亮：上一帧的显示值，用来判断哪一行变了 --
        self._prev_cells: list[str] | None = None
        # 明细视图上次因超长截断记日志时的点数：轮询每拍都会重画这张表，
        # 只在点数变化时提醒一次，否则每秒两条把通信日志刷没
        self._detail_trunc: int | None = None
        self._fade_job = None
        # -- 迷你趋势条：选中单元格的历史值 --
        self._trend: collections.deque = collections.deque(maxlen=120)
        self._trend_key: int | None = None
        self._grid_per = 10             # 网格当前每行格数（点击定位要用）
        self._grid_pad = 0              # 首行左侧留空几格（十六进制对齐用）
        self._view_state: dict = {}     # 数据定义的当前值（弹层重建时恢复）
        # -- 地址记法：工具条上那个下拉的两个出口 --
        # 渲染、解析、弹层恢复都读这两个布尔，下拉框只是它们的编辑器。
        # 放在这里而不是弹层构造函数里：工具条比弹层先建，而且地址框的
        # 输入校验要在每次按键时问一句「现在是不是十六进制记法」。
        self._addr_hex = tk.BooleanVar(value=False)
        self.plc_var = tk.BooleanVar(value=False)
        self._written_span = None       # 刚写过的寄存器范围 (起, 止)，用来框选
        self._written_marks: list = []  # 框选框的 4 条边框
        self._written_job = None        # 框选过期定时器

        self._init_style()
        self._build_ui()
        self._on_link_mode_change()
        self._on_fc_change()
        self._log("运行日志仅在界面显示；需要留档时用「导出」手动保存")
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
        style.configure("Card.TFrame", background=CARD, borderwidth=1, relief="solid",
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Inner.TFrame", background=CARD)
        style.configure("Sunken.TFrame", background=SUNKEN)
        style.configure("Rule.TFrame", background=BORDER_SOFT)

        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Sunken.TLabel", background=SUNKEN, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure("MutedSunken.TLabel", background=SUNKEN, foreground=MUTED)
        style.configure("SubSunken.TLabel", background=SUNKEN, foreground=MUTED)
        style.configure("Faint.TLabel", background=CARD, foreground=FAINT, font=FONT_SM)
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT,
                        font=FONT_TITLE)
        style.configure("Section.TLabel", background=CARD, foreground=TEXT,
                        font=FONT_SECTION)
        style.configure("AppTitle.TLabel", background=BG, foreground=MUTED,
                        font=FONT_DISPLAY)
        style.configure("Sub.TLabel", background=BG, foreground=MUTED)
        style.configure("Big.TLabel", background=CARD, foreground=TEXT, font=FONT_TITLE)

        style.configure("TSeparator", background=BORDER_SOFT)

        # 按钮三语义：中性 filled、主操作 accent、危险 danger（淡红底红字）。
        # padding 的纵向值决定控件高度 —— 与 TEntry/TCombobox 取同一个 5，
        # 这样同一行里的输入框和按钮基线才不差 1~2px。
        style.configure("TButton", padding=(SP_XL, 5), borderwidth=0,
                        background=FIELD_BG, foreground=TEXT,
                        focusthickness=0, focuscolor=FIELD_BG)
        style.map("TButton",
                  background=[("disabled", DISABLED_BG),
                              ("pressed", "#dfe4ec"),
                              ("active", HOVER_BG)],
                  foreground=[("disabled", DISABLED_FG)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        borderwidth=0)
        style.map("Accent.TButton",
                  background=[("disabled", "#b9c9f7"),
                              ("pressed", ACCENT_PRESS),
                              ("active", ACCENT_HOVER)],
                  foreground=[("disabled", "#f4f7ff")])
        # 危险操作：淡红底 + 红字，与中性按钮同尺寸 —— 不再整条铺红
        style.configure("Danger.TButton", background=ERR_SOFT, foreground=ERR,
                        borderwidth=0)
        style.map("Danger.TButton",
                  background=[("disabled", DISABLED_BG),
                              ("pressed", "#fbdada"),
                              ("active", "#fce4e4")],
                  foreground=[("disabled", DISABLED_FG)])
        # 幽灵按钮：只给文字，用在卡片标题行的次级动作
        style.configure("Ghost.TButton", background=CARD, foreground=MUTED,
                        borderwidth=0, padding=(SP_M, 3))
        style.map("Ghost.TButton",
                  background=[("active", HOVER_BG), ("pressed", "#e2e7ee")],
                  foreground=[("active", TEXT), ("disabled", DISABLED_FG)])
        # 输入框：filled 风格 —— 浅底无边框，聚焦时白底 + 品牌蓝细边
        style.configure("TEntry", fieldbackground=FIELD_BG, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=(SP_S, 5))
        style.map("TEntry",
                  fieldbackground=[("disabled", DISABLED_BG), ("focus", "#ffffff"),
                                   ("!focus", FIELD_BG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)],
                  foreground=[("disabled", DISABLED_FG)])
        style.configure("TCombobox", fieldbackground=FIELD_BG, background=FIELD_BG,
                        arrowcolor=MUTED, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=(SP_S, 5))
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
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED, padding=(SP_S, 5))
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
                        rowheight=27, borderwidth=0, font=MONO,
                        bordercolor=BORDER_SOFT, lightcolor=CARD, darkcolor=CARD)
        # 表头：底色只比卡片深一档（1.05:1），靠 HEAD_EDGE 的 1px 底边和数据区
        # 切开 —— Tk 没有阴影，单靠填充差永远差一口气
        style.configure("Treeview.Heading",
                        background=HEAD_BG, foreground=HEAD_FG,
                        relief="flat", font=UI_FONT, padding=(SP_S, 5),
                        bordercolor=HEAD_EDGE, lightcolor=HEAD_BG, darkcolor=HEAD_EDGE)
        style.map("Treeview", background=[("selected", SELECT_BG)],
                  foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", HEAD_BG)])

        # 概览视图：行更紧凑，一眼能扫更多寄存器（Modbus Poll 的观感）
        style.configure("Grid.Treeview",
                        background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=26, borderwidth=0, font=MONO,
                        bordercolor=BORDER_SOFT, lightcolor=CARD, darkcolor=CARD)
        style.configure("Grid.Treeview.Heading",
                        background=HEAD_BG, foreground=HEAD_FG,
                        relief="flat", font=UI_FONT, padding=(SP_S, 4),
                        bordercolor=HEAD_EDGE, lightcolor=HEAD_BG, darkcolor=HEAD_EDGE)
        style.map("Grid.Treeview", background=[("selected", SELECT_BG)],
                  foreground=[("selected", TEXT)])
        style.map("Grid.Treeview.Heading", background=[("active", HEAD_BG)])

        # 滚动条：细、灰、无立体感，贴着内容层走
        style.configure("TScrollbar", background=CARD, troughcolor=SUNKEN,
                        bordercolor=CARD, arrowcolor=FAINT, relief="flat",
                        width=10)
        style.map("TScrollbar",
                  background=[("active", "#cdd4de"), ("!active", "#e2e7ee")],
                  arrowcolor=[("active", TEXT), ("!active", FAINT)])

    # ---------------------------------------------------------------- 布局

    def _build_ui(self) -> None:
        self._build_header()
        self._build_conn_popover()
        self._build_view_dialog()      # 数据定义：低频，做成弹层

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
        # 现场是左手键盘右手鼠标：切页、停轮询、手动补读都该有键可用，
        # 不必每次把手从键盘挪到鼠标上点按钮
        self.root.bind_all("<F5>", lambda e: self._run_primary())
        self.root.bind_all("<Control-Return>", lambda e: self._run_primary())
        self.root.bind_all("<Escape>", lambda e: self._stop_poll(manual=True))
        # Ctrl+I：问一句「对面是谁」。和右键菜单是同一个入口 —— 工具条上不再
        # 常驻一个「识别」按钮，把位置让给读/写/轮询这些真正高频的动作
        self.root.bind_all("<Control-Key-i>", self._on_identify_key)
        self.root.bind_all("<Control-Key-l>", lambda e: self._clear_log())
        self._sync_master_chrome()

    def _run_primary(self) -> str:
        """F5 / Ctrl+Enter：跟随当前页 —— 主站页执行一次，从站页刷新表格。

        主站页已经没有「读取」按钮（数据由轮询持续刷新），F5 在读功能码下
        等价于「手动补读一次」，写功能码下就是执行写入。
        全局快捷键不看页签就发主站请求，是切换体验上的一种「残留」：
        在从站页按 F5（肌肉记忆里是「刷新」），未连接时「尚未连接」会写进
        从站页看不见的主站状态栏，已连接时更是后台真发一帧读。从站页的
        F5 语义就应该是刷新从站表格；没起从站或没变化时它自然空转。
        """
        if self.tabs.index == 1:
            self._refresh_slave_table()
            return "break"
        if self._current_fc() in (1, 2, 3, 4):
            self._read_once()
        else:
            self._do_write_async()
        return "break"

    def _on_identify_key(self, _event=None):
        """Ctrl+I：读设备标识。吃了这个事件，免得 Entry 里插进一个制表符。"""
        self._identify_device_async()
        return "break"

    def _switch_tab(self, index: int) -> None:
        # 先把打开中的就地编辑器按「失焦」收尾：页签点击刻意不抢键盘焦点
        # （SegmentedTabs 的设计），FocusOut 不会自己来 —— 不处理的话编辑器
        # 会被 grid_remove 藏起来活到最后，切回来悬在可能已经错位的坐标上。
        # 合成 FocusOut 复用编辑器自己的语义：从站失焦提交、主站失焦取消。
        if self._editor is not None:
            try:
                self._editor.event_generate("<FocusOut>")
            except tk.TclError:
                self._cancel_edit()
        if index == 1:
            self.tab_master.grid_remove()
            self.tab_slave.grid()
            self.tab_slave.tkraise()
            # 切过来立刻刷一次。刷新已经是原地更新（数据没变就一行都不动），
            # 不会再出现「先白一下再填满」的闪动，也就没理由让用户看旧值。
            self._refresh_slave_table()
        else:
            self.tab_slave.grid_remove()
            self.tab_master.grid()
            self.tab_master.tkraise()
        # 日志覆盖层只跟主站页走：在主站开的日志不该盖到从站表格上。
        # 这里两头都要重摆，光处理一头都不对 ——
        #   只在进从站页 lift：日志会一直盖在从站页上（2026-10-24 用户报的 bug）；
        #   只在回主站页 lift：tkraise 已把主站页顶到日志上面，日志开着却看不见。
        # _log_open 只记「用户开没开」，可见性由当前页决定；切页不动日志内容，
        # 从站页期间的「主站接入 / 故障注入」照样记着，切回主站就能看到。
        if index == 0 and self._log_open:
            self._show_log_panel()
        else:
            self.log_panel.pack_forget()
        # 主站专属顶栏控件（连接按钮/状态灯）同理：不跟到从站页，
        # 切回时按连接状态决定去留；连接浮层也要顺手收起
        self._sync_master_chrome()

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
        header = ttk.Frame(self.root, padding=(PAD_CONTAINER, SP_M, PAD_CONTAINER, SP_M))
        header.pack(fill="x")

        brand = ttk.Frame(header)
        brand.pack(side="left")
        self._brand_mark(brand).pack(side="left", padx=(0, SP_M))
        ttk.Label(brand, text="ModNova", style="AppTitle.TLabel").pack(side="left")

        self.tabs = SegmentedTabs(
            header,
            labels=["主站 · 读别人", "从站 · 被别人读"],
            on_change=self._switch_tab,
            badge_slots=(1,),
            height=CTRL_H,
        )
        self.tabs.pack(side="left", padx=(SP_XL, 0))

        # 分裂按钮：主区是「连/断」这个动作本身（未连接=按当前参数直接连，
        # 已连接=断开），右侧窄条 ▾ 只负责展开参数表单。这样最常用的动作
        # 一次点击完成，「换 IP / 换串口」这种低频操作才需要第二步。
        self.connect_btn = RoundedButton(header, text="连接", style="Accent.TButton",
                                         min_width=96,
                                         command=self._on_connect_click,
                                         split_command=self._on_connect_menu)
        self.led = StatusPill(header, text="未连接", tone="idle", max_width=360)
        self.led.pack(side="right", padx=(0, SP_L))

    def _on_connect_click(self) -> None:
        """主区：未连接 = 直接按当前参数连，已连接 = 断开。

        参数表单不拦在中间了 —— 默认值（127.0.0.1:502 / 站 1）就是现场
        最常见的组合，而「先弹表单再点连接」等于把每一次连接都变成两步。
        要改参数走右边的 ▾。
        """
        self._close_conn_popover()
        self._toggle_connection()

    def _on_connect_menu(self) -> None:
        """窄条 ▾：只展开/收起参数表单，不碰连接状态。"""
        self._toggle_conn_popover()

    def _on_conn_go(self) -> None:
        """浮层底部那颗「连接/断开」：动作在表单里完成，做完就收起表单。

        连接失败时浮层必须留着 —— 失败的原因通常就在刚填的那几项里，
        收起来等于把「为什么没连上」一起收走了（失败原因进状态栏和日志）。
        """
        if self.client is not None:
            self._close_conn_popover()
        self._toggle_connection()

    # -- 连接参数的记忆 ----------------------------------------------------
    #
    # 现场一台设备配一次参数，之后每天开软件都是同一组 IP / 端口。让人每次
    # 重填就是在赌他记得住 —— 而记错一位数字的表现是「连不上」，看起来像
    # 设备故障。所以：**连成功**就把这组参数落盘，下次启动直接回填。
    #
    # 只记「成功过」的那一组：每改一次就存的话，一次手滑改错也会变成下次的
    # 默认值，等于把失败固化成起点。

    @staticmethod
    def _num_of(text, fallback, cast=int):
        """控件文本 → 数字，读不出来时退回默认值（配置文件可以被手改）。"""
        try:
            return cast(str(text).strip())
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _fill_field(widget, value: str) -> None:
        widget.delete(0, "end")
        widget.insert(0, value)

    def _save_link_settings(self) -> None:
        """连接成功后把当前这组链路参数写进配置文件。"""
        link = {
            "mode": self.link_mode.get(),
            "host": self.tcp_host.get().strip() or "127.0.0.1",
            "port": self._num_of(self.tcp_port.get(), DEFAULT_TCP_PORT),
            "port_name": self.rtu_port.get().strip(),
            "baud": self._num_of(self.baud.get(), 9600),
            "databits": self._num_of(self.databits.get(), 8),
            "parity": self.parity.get() or "N",
            "stopbits": self._num_of(self.stopbits.get(), 1.0, float),
            "unit": self._num_of(self.unit_var.get(), 1),
            "timeout": self._num_of(self.timeout_var.get(), 2.0, float),
            "retries": self._num_of(self.retry_var.get(), 0),
        }
        self.config.update_link(link)
        try:
            self.config.save()
        except OSError as exc:
            # 存不下只意味着「下次要重填」，不能因此动已经连上的链路
            self._log(f"连接参数未能保存：{exc}")

    def _restore_link_settings(self) -> None:
        """按上次成功的参数回填控件。

        Config 已把用户文件叠在默认值上，字段一定齐；仍然逐项校验范围 ——
        配置文件是纯文本，手改错了不该让界面带着非法值启动。
        """
        link = self.config.link

        mode = link.get("mode")
        if mode in LINK_LABELS:                  # 未知值（手改错）就留在默认 TCP
            self.link_mode.set(mode)
            self._on_link_mode_change()

        host = str(link.get("host") or "").strip()
        if host:
            self._fill_field(self.tcp_host, host)

        # 端口不参与记忆，永远从 502 起（这里显式写回去，不再读配置）。
        #
        # 端口不像 IP / 串口那样是「这台设备的身份」，它基本恒为 502；真正会
        # 变的是调试时临时连一次别处的服务（本机模拟器起在 5020、别人的网关
        # 开在 1502）。那种一次性连法一旦被记住，下次开软件看到的「默认端口」
        # 就不是 502 了 —— 像配置被改坏，而用户根本没改过它。502 是标准值，
        # 让它当默认比「记住最后一次」更符合直觉；真要连非标端口，当场改一次
        # 也就一次。
        self._fill_field(self.tcp_port, str(DEFAULT_TCP_PORT))

        unit = self._num_of(link.get("unit"), 0)
        if 1 <= unit <= 247:
            self._fill_field(self.unit_var, str(unit))

        timeout = self._num_of(link.get("timeout"), 0.0, float)
        if 0.1 <= timeout <= 30:
            self._fill_field(self.timeout_var, f"{timeout:g}")

        retries = self._num_of(link.get("retries"), -1)
        if 0 <= retries <= 5:
            self._fill_field(self.retry_var, str(retries))

        # 串口名可能是「上次插着、这次没插」的那个口：照样回填。
        # 现场最常见的抱怨是「明明存过 COM5，打开却是空的」—— 设备没插
        # 是设备的事，界面不该假装从没配过。没在下拉列表里就补进去。
        name = str(link.get("port_name") or "").strip()
        if name:
            values = list(self.rtu_port.cget("values") or ())
            if name not in values:
                self.rtu_port.configure(values=[name] + values)
            self.rtu_port.set(name)

        # 下拉型的参数：只接受列表内的值，否则宁可留在默认档
        baud = str(self._num_of(link.get("baud"), 0))
        if baud in list(self.baud.cget("values") or ()):
            self.baud.set(baud)
        databits = str(self._num_of(link.get("databits"), 0))
        if databits in list(self.databits.cget("values") or ()):
            self.databits.set(databits)
        parity = str(link.get("parity") or "")
        if parity in list(self.parity.cget("values") or ()):
            self.parity.set(parity)
        stopbits = f"{self._num_of(link.get('stopbits'), 0.0, float):g}"
        if stopbits in list(self.stopbits.cget("values") or ()):
            self.stopbits.set(stopbits)

    def _bar_field(self, parent, label: str, widget) -> None:
        ttk.Label(parent, text=label, style="Sub.TLabel").pack(side="left", padx=(0, SP_S))
        widget.pack(side="left", padx=(0, SP_L))

    def _install_input_guard(self, widget, maximum: int | float,
                             decimal: bool = False, notation: bool = False) -> None:
        """给输入框挂按键级校验：非法字符拒、超上限拒。

        Spinbox 的 from_/to 只约束点击箭头，键盘敲进去的它不管 —— 站地址能敲出
        9999 就是这么来的；Entry 更是没有任何内置限制。所以统一在按键这一层
        拦一道：清空允许（中间态），下限留给连接/启动时的 _int_field 报人话。
        粘贴也走同一道闸。
        decimal=True 的小数字段（超时）额外放行一个小数点，"2." 这类中间态合法。
        notation=True 的地址框还要放行 0x 十六进制、0b 二进制和 PLC 六位记法
        （十进制最大 465535 = 400001 + 65535 - 1）—— 记法换算与区域的语义
        校验交给 _address_field，只有它报得清「哪里不对」。
        十六进制记法下裸写的「1A」也放行：既然下拉已经说明这里是十六进制，
        再逼人补一个 0x 就是让控件白设。
        注意 Tk 的 validate 对程序化 insert 同样生效，这里拦的不只是键盘。
        """
        def digits_ok(digits: str, base: int) -> bool:
            """按进制判字符。以前这里一律用 str.isdigit()，于是 0x1F / 0x0A
            全被当成非法输入挡在门外 —— 十六进制里出现 a-f 才是常态。"""
            allowed = "0123456789abcdef" if base == 16 else "01"
            return all(ch in allowed for ch in digits.lower())

        def check(proposed: str) -> bool:
            if proposed == "":
                return True
            if notation:
                body = _normalize_number(proposed)
                low = body.lower()
                bare_hex = self._addr_hex.get() and not low.startswith("0b")
                if low.startswith("0x") or bare_hex:
                    digits = body[2:] if low.startswith("0x") else body
                    if digits == "":
                        return True          # 「0x」是打字中间态，先放行
                    return digits_ok(digits, 16) and int(digits, 16) <= maximum
                if low.startswith("0b"):
                    digits = body[2:]
                    if digits == "":
                        return True
                    return digits_ok(digits, 2) and int(digits, 2) <= maximum
                return body.isdigit() and int(body) <= 465535
            if decimal:
                if proposed.count(".") > 1:
                    return False
                if not proposed.replace(".", "").isdigit():
                    return False
                try:
                    return float(proposed) <= maximum
                except ValueError:
                    return False
            return proposed.isdigit() and int(proposed) <= maximum

        widget.configure(validate="key",
                         validatecommand=(self.root.register(check), "%P"))

    def _build_conn_popover(self) -> None:
        """连接参数收进顶栏按钮的浮层，不再常驻一整行。

        原来这条参数条占主站页 40px。而现场节奏是「设一次参数、然后盯半天
        数据」，那 40px 绝大多数时间是空的，还把「连接」这个动作拆到了两个
        地方（参数条填、顶栏点）。现在：点顶栏连接键右边那一格展开这份表单，
        填完点表单底部的「连接」就连上并自动收起 —— 参数只在「要改」的时候
        才占位置，主键仍然是一点即连。

        控件对象沿用原名（tcp_host / tcp_port / unit_var …）：它们是参数的
        唯一来源，工作流和测试都直接读写这些变量，改名字只会制造两套入口。
        """
        pop = tk.Toplevel(self.root)
        pop.withdraw()
        pop.overrideredirect(True)          # 无标题栏：它是浮层，不是一个窗口
        pop.configure(bg=BORDER)            # 外圈 1px 描边靠底色露出来
        # overrideredirect 的窗口不受 owner 关系保护，主窗口一旦被置前
        # 就会把它盖住；transient 把它钉成 root 的从属窗口，才始终浮在上面
        pop.transient(self.root)
        self.conn_popover = pop

        body = tk.Frame(pop, bg=CARD, padx=PAD_CARD, pady=PAD_CARD)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.conn_body = body
        # 标签列宽定成常量：TCP 与 RTU 两套字段在各自子网格里排版，
        # 子网格不继承这里的列宽，两边各设一次才能真正对齐
        body.columnconfigure(0, minsize=POP_LABEL_W)
        body.columnconfigure(1, weight=1, minsize=236)

        ttk.Label(body, text="连接设置", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, SP_M))

        self.link_mode = tk.StringVar(value="tcp")
        self.link_box = ttk.Combobox(body, values=list(LINK_LABELS.values()),
                                     state="readonly")
        self.link_box.set(LINK_LABELS["tcp"])
        self.link_box.bind("<<ComboboxSelected>>", self._on_link_box_selected)
        row = self._pop_field(body, 1, "连接方式", self.link_box)

        # 连接方式不同，字段个数就不同（TCP 2 个 / RTU 5 个），各占一个子网格
        self.link_holder = ttk.Frame(body, style="Inner.TFrame")
        self.link_holder.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.link_holder.columnconfigure(0, weight=1)
        row += 1

        self.tcp_frame = ttk.Frame(self.link_holder, style="Inner.TFrame")
        self.tcp_frame.columnconfigure(0, minsize=POP_LABEL_W)
        self.tcp_frame.columnconfigure(1, weight=1)
        self.tcp_frame.pack(fill="x")
        self.tcp_host = ttk.Entry(self.tcp_frame)
        self.tcp_host.insert(0, "127.0.0.1")
        self._pop_field(self.tcp_frame, 0, "IP 地址", self.tcp_host)
        self.tcp_port = ttk.Spinbox(self.tcp_frame, from_=1, to=65535)
        self.tcp_port.set(str(DEFAULT_TCP_PORT))
        self._install_input_guard(self.tcp_port, 65535)
        self._pop_field(self.tcp_frame, 1, "端口", self.tcp_port)

        self.rtu_frame = ttk.Frame(self.link_holder, style="Inner.TFrame")
        self.rtu_frame.columnconfigure(0, minsize=POP_LABEL_W)
        self.rtu_frame.columnconfigure(1, weight=1)
        ports = list_serial_ports()
        self.rtu_port = ttk.Combobox(self.rtu_frame, values=ports)
        self.rtu_port.set(ports[0] if ports else "")
        self._pop_field(self.rtu_frame, 0, "串口", self.rtu_port)
        self.baud = ttk.Combobox(self.rtu_frame,
                                 values=["4800", "9600", "19200", "38400",
                                         "57600", "115200"],
                                 state="readonly")
        self.baud.set("9600")
        self._pop_field(self.rtu_frame, 1, "波特率", self.baud)
        # 老仪表里 7 数据位（7E1）并不罕见，固定 8 位会一直 CRC 错
        self.databits = ttk.Combobox(self.rtu_frame, values=["8", "7", "6", "5"],
                                     state="readonly")
        self.databits.set("8")
        self._pop_field(self.rtu_frame, 2, "数据位", self.databits)
        self.parity = ttk.Combobox(self.rtu_frame, values=["N", "E", "O", "M", "S"],
                                   state="readonly")
        self.parity.set("N")
        self._pop_field(self.rtu_frame, 3, "校验", self.parity)
        self.stopbits = ttk.Combobox(self.rtu_frame, values=["1", "1.5", "2"],
                                     state="readonly")
        self.stopbits.set("1")
        self._pop_field(self.rtu_frame, 4, "停止位", self.stopbits)

        self.unit_var = ttk.Spinbox(body, from_=1, to=247)
        self.unit_var.set("1")
        # Modbus 单元号合法域 1~247（0 广播、248+ 保留）：手敲也不许越过 247
        self._install_input_guard(self.unit_var, 247)
        row = self._pop_field(body, row, "设备地址", self.unit_var)

        self.timeout_var = ttk.Spinbox(body, from_=0.1, to=30, increment=0.5)
        self.timeout_var.set("2")
        self._install_input_guard(self.timeout_var, 30, decimal=True)
        row = self._pop_field(body, row, "超时", self.timeout_var)

        self.retry_var = ttk.Spinbox(body, from_=0, to=5)
        self.retry_var.set("1")
        self._install_input_guard(self.retry_var, 5)
        row = self._pop_field(body, row, "重试", self.retry_var)

        # 这里原来有一行「连接状态 / 失败原因」（env_label）。撤掉的理由：
        # 连没连上按钮自己会变「断开」，失败原因同时进了状态栏和日志 ——
        # 同一句话在三个地方各说一遍，浮层反而多出一行只读的文字。
        self.conn_go = RoundedButton(body, text="连接", style="Accent.TButton",
                                     command=self._on_conn_go)
        self.conn_go.grid(row=row, column=0, columnspan=2, sticky="ew",
                          pady=(SP_M, 0))

        # 关浮层是「点外面」这一种手势（外加 Esc）：bind_all 常驻，回调里
        # 先看浮层在不在，不在就立刻返回 —— 解绑反而更容易漏掉某条路径
        self.root.bind_all("<Button-1>", self._on_popover_click, add="+")
        self.root.bind_all("<Escape>", self._on_popover_escape, add="+")

        # 控件都建好之后再回填：上次连成功过的那组参数直接出现在表单里，
        # 主键一点就连上，用户不需要重新敲一遍 IP
        self._restore_link_settings()

    def _pop_field(self, parent, row: int, label: str, widget) -> int:
        """浮层里的一行「标签 + 控件」，返回下一行号。"""
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=SP_S)
        widget.grid(row=row, column=1, sticky="ew", padx=(SP_L, 0), pady=SP_S)
        return row + 1

    # -- 连接浮层 ----------------------------------------------------------

    def _popover_open(self) -> bool:
        pop = getattr(self, "conn_popover", None)
        return bool(pop is not None and pop.winfo_ismapped())

    def _toggle_conn_popover(self) -> None:
        if self._popover_open():
            self._close_conn_popover()
        else:
            self._open_conn_popover()

    def _open_conn_popover(self) -> None:
        """贴着按钮下沿右对齐浮出 —— 位置跟按钮走，不跟窗口走。"""
        if self.tabs.index != 0:
            return
        pop = self.conn_popover
        pop.update_idletasks()
        w = pop.winfo_reqwidth()
        x = self.connect_btn.winfo_rootx() + self.connect_btn.winfo_width() - w
        y = self.connect_btn.winfo_rooty() + self.connect_btn.winfo_height() + 6
        x = max(8, min(x, self.root.winfo_screenwidth() - w - 8))
        pop.geometry(f"+{x}+{y}")
        pop.deiconify()
        pop.lift()
        if self.client is None:
            # 打开就把光标放进该改的那个字段，并全选：改 IP / 换串口是这里
            # 最高频的动作，全选后直接打字就替换，不用先按 Ctrl+A
            target = self.tcp_host if self.link_mode.get() != "rtu" else self.rtu_port
            try:
                target.focus_set()
                target.select_range(0, "end")
            except tk.TclError:
                pass

    def _close_conn_popover(self) -> None:
        pop = getattr(self, "conn_popover", None)
        if pop is not None and pop.winfo_ismapped():
            pop.withdraw()

    def _on_popover_click(self, event) -> None:
        # 点在自己身上不关，点在别处全关。bind_all 只该挂一次，否则漏一个就是
        # 「关不掉」；现在全应用只剩连接参数这一个浮层了。
        if not self._popover_open():
            return
        # 点在自己的控件上（含 Combobox 弹出的原生列表 —— 它的窗口路径
        # 是浮层的子路径）就不关；其余一律关
        try:
            path = str(event.widget)
        except Exception:
            path = ""
        if path.startswith(str(self.conn_popover)) or path.startswith(str(self.connect_btn)):
            return
        self._close_conn_popover()

    def _on_popover_escape(self, _event=None) -> None:
        self._close_conn_popover()

    def _sync_master_chrome(self) -> None:
        """主站专属的顶栏控件（连接按钮 / LED 状态灯）只在主站页出现。

        连/断与切页两头都从这里过：在从站页断开连接时，这些控件不能
        弹到从站页上；切回主站页时按连接状态恢复。

        - 连接/断开按钮：主站页常驻（连接中它就是「断开」）——参数浮层
          也挂在它右边那一格 ▾ 下面，所以它是本页唯一的主站链路入口；
        - LED 状态灯：也是主站链路信息，跟按钮一起只留在主站页 ——
          从站页顶部只放从站自己的事，运行状态看页签绿灯徽标和「从站参数」卡
          （2026-10-24 用户三连问：参数条、连接按钮、连接状态灯）。
        - 浮层：连上了、或者切到从站页，都必须收起来 —— 一个浮在别的
          页签上的连接表单比任何布局问题都更让人困惑。
        """
        if self.tabs.index != 0 or self.client is not None:
            self._close_conn_popover()
        if self.tabs.index == 0:
            self.led.pack(side="right", padx=(0, SP_L))
            connected = self.client is not None
            self.connect_btn.configure(
                text="断开" if connected else "连接",
                style="Danger.TButton" if connected else "Accent.TButton")
            self.connect_btn.pack(side="right", before=self.led)
        else:
            self.connect_btn.pack_forget()
            self.led.pack_forget()

    def _render_link(self) -> None:
        """顶栏的链路胶囊。

        站号必须摆在这里 —— 一条总线上挂七八台设备时，「我读的是哪台」
        比「我连的是哪个口」更要紧。
        """
        if self.client is None:
            failed = "失败" in self._conn_note
            self.led.configure(text=f"{self._conn_note or '未连接'}",
                               foreground=ERR if failed else MUTED)
            return
        where = f"{self._link_text} · 站 {self._link_unit}"
        # 设备支持 FC2B 时白捡的型号名贴在最末 —— 「我读的是哪台」这件事，
        # 一个型号名比一个站号更让人放心
        if self._device_note:
            where += f" · {self._device_note}"
        if self._link_health == "warn":
            self.led.configure(text=f"无响应 {self._fail_streak} 次 · {where}",
                               foreground=WARN)
        elif self._link_health == "down":
            self.led.configure(text=f"链路中断 · {where}", foreground=ERR)
        else:
            self.led.configure(text=f"在线 · {where}", foreground=OK)

    @staticmethod
    def _replied(error: BaseException) -> bool:
        """这次失败是不是「从站答了，只是说不行」？

        `ModbusError` 表示收到了并从线上解出了一帧异常响应 —— 线是通的；
        `ParameterError` 是本地校验没过（请求根本没发出去），不能算在内，
        它虽然也继承 `ModbusError`。
        """
        return isinstance(error, ModbusError) and not isinstance(error, ParameterError)

    def _mark_link(self, ok: bool, replied: bool = False) -> None:
        """每次请求结束都过一遍：成功清零，失败累加并降级。

        replied=True 的失败不降级：从站回了异常码，说明链路本身是好的，
        它只是拒绝了这一次请求。把「地址填错」标成「链路中断」，人会去查
        网线和串口，而真正该改的是地址或功能码。拒绝的理由状态栏已经写清，
        顶栏只需要老老实实说「在线」。
        """
        if ok or replied:
            self._fail_streak = 0
            self._link_health = "on"
        else:
            self._fail_streak += 1
            self._link_health = "down" if self._fail_streak >= 3 else "warn"
        self._render_link()

    def _card(self, parent, title: str, actions=None):
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(PAD_CARD, SP_L, PAD_CARD, SP_L))
        if title:
            head = ttk.Frame(outer, style="Inner.TFrame")
            head.pack(fill="x", pady=(0, SP_M))
            ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side="left")
            if actions:
                actions(head)             # 标题行右侧的视图切换/格式控件
        body = ttk.Frame(outer, style="Inner.TFrame")
        body.pack(fill="both", expand=True)
        return outer, body

    def _form_row(self, parent, row: int, label: str, widget, sticky="ew"):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=SP_S)
        widget.grid(row=row, column=1, sticky=sticky, padx=(SP_L, 0), pady=SP_S)
        parent.columnconfigure(1, weight=1)
        return widget

    # ------------------------------------------------------------ 主站页

    def _build_master_tab(self) -> None:
        """主站页 = 一条工具条 + 一张铺满的表格 + 一条状态栏。

        以前这里是「左栏链路 290px + 右侧操作卡 193px + 数据卡 274px」，
        数据表只剩 141px。现在设置项都搬走了（连接参数在连接条、数据定义弹层），
        剩下的高度全部给表格。
        """
        card = tk.Frame(self.tab_master, bg=CARD)
        card.pack(fill="both", expand=True)
        card.rowconfigure(3, weight=1)
        card.columnconfigure(0, weight=1)

        bar = ttk.Frame(card, style="Sunken.TFrame",
                        padding=(PAD_CARD, SP_M, PAD_CARD, SP_S))
        bar.grid(row=0, column=0, sticky="ew")
        self._build_toolbar(bar)

        # 写入预览独占一行（2026-10-24）：原先挤在工具条里，切到写多寄存器后
        # 一段地址映射文本能把最右的「数据定义」按钮整个顶出窗口 —— 按钮是
        # 最后 pack 的，空间不够先牺牲它，用户就找不到改类型的地方了。
        # 挪到独立行后工具条宽度恒定，「数据定义」任何功能码下都在。
        # 「这个值会占几个寄存器」在按按钮之前就该看得见：
        # float32 填一个数就是 2 个寄存器，从输入框里数不出来。
        self.write_hint_row = ttk.Frame(card, style="Sunken.TFrame",
                                        padding=(PAD_CARD, 0, PAD_CARD, SP_S))
        self.write_hint = ttk.Label(self.write_hint_row, text="", style="SubSunken.TLabel")
        self.write_hint.pack(side="left")
        ttk.Separator(card).grid(row=2, column=0, sticky="ew")

        tree_wrap = ttk.Frame(card, style="Inner.TFrame")
        tree_wrap.grid(row=3, column=0, sticky="nsew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        # -- 明细视图：一行一个值，数值/十六进制/二进制三列常驻 --
        self.detail_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.detail_wrap.rowconfigure(0, weight=1)
        self.detail_wrap.columnconfigure(0, weight=1)
        columns = ("seq", "addr", "dec", "hex", "bin")
        self.result_tree = ttk.Treeview(self.detail_wrap, columns=columns, show="headings", height=12)
        for key, text, width, anchor in (
            ("seq", "#", 40, "center"),
            ("addr", "地址", 104, "center"),
            ("dec", FMT_VALUE, 160, "e"),
            ("hex", FMT_HEX, 140, "e"),
            ("bin", FMT_BIN, 320, "w"),
        ):
            # 表头对齐必须跟数据列一致（数值/十六进制是右对齐），否则表头飘在左边、数据贴在右边
            self.result_tree.heading(key, text=text, anchor=anchor)
            self.result_tree.column(key, width=width, anchor=anchor, stretch=(key == "bin"))
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.detail_wrap, orient="vertical", command=self.result_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(SP_XS, 0))
        self.result_tree.configure(yscrollcommand=scroll.set)

        # -- 概览视图：行头是起始地址，每行 N 格，进制跟「格式」走 --
        self.grid_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.grid_wrap.rowconfigure(0, weight=1)
        self.grid_wrap.columnconfigure(0, weight=1)
        self.result_grid = ttk.Treeview(self.grid_wrap, style="Grid.Treeview",
                                        show="headings", height=16)
        self._setup_grid(self.result_grid, 10, 74, "e")
        self.result_grid.grid(row=0, column=0, sticky="nsew")
        gscroll = ttk.Scrollbar(self.grid_wrap, orient="vertical",
                                command=self.result_grid.yview)
        gscroll.grid(row=0, column=1, sticky="ns", padx=(SP_XS, 0))
        self.result_grid.configure(yscrollcommand=gscroll.set)
        # 不装横向滚动条：列宽会平分窗口，永远铺满，那条常驻的空滚动条只是噪音

        # 两个视图叠在同一格，靠 grid_remove 切换；默认亮概览
        self.grid_wrap.grid(row=0, column=0, sticky="nsew")
        self.detail_wrap.grid(row=0, column=0, sticky="nsew")
        self.detail_wrap.grid_remove()

        # 空状态：只读 10 个寄存器时表格里只有一行，其余全白。一片空白既不说
        # 「在等」也不说「没连上」，用户会反复点按钮确认自己是不是漏了什么。
        # 用 place 浮在数据区中央 —— 不参与 grid，不影响行数计算。
        self.empty_hint = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.empty_title = ttk.Label(self.empty_hint, text="尚未连接",
                                     style="CardTitle.TLabel")
        self.empty_title.pack()
        self.empty_desc = ttk.Label(self.empty_hint, text="", style="Muted.TLabel")
        self.empty_desc.pack(pady=(SP_S, 0))
        self.result_tree.tag_configure("odd", background="#f6f8fb")
        # 变化高亮：比 zebra 后建，优先级更高，压得住斑马纹
        self.result_tree.tag_configure("chg", background="#e8eefc")
        self.result_grid.tag_configure("chg", background="#e8eefc")
        for tree in (self.result_tree, self.result_grid):
            tree.bind("<Button-1>", self._on_table_click, add="+")
            tree.bind("<Motion>", self._on_table_motion, add="+")
            tree.bind("<Leave>", lambda e: self._set_cursor_hint(None))
            tree.bind("<Button-3>", self._on_table_menu)
            tree.bind("<Double-1>", self._on_table_double)
            self._install_copy_menu(tree)

        ttk.Separator(card).grid(row=4, column=0, sticky="ew")
        self._build_status_bar(card)
        self._draw_trend()
        self._refresh_stats()          # 启动时就把「当前按什么解释」显示出来

    def _build_toolbar(self, bar) -> None:
        """一条工具条按三区排列：参数区 │ 轮询区 ……………… 视图区。

        分区依据是「这个词属于哪一步」，而不是「控件长什么样」。以前是
        参数-动作-参数-动作 交替（地址/数量 → 写入/识别 → 轮询间隔 →
        开始轮询），读功能码下还留着一个灰掉的「写入」占位，整条看过去
        就是一堆零件平铺。

        读写两组控件现在互斥：读码用轮询区，写码用「值 + 写入」。这既是
        语义上的正确，也是布局上的稳压器 —— 以前写码下工具条比读码下宽
        106 px，1040 宽的窗口里把最右边的「数据定义」压成一条 20 px 的
        碎块，107 px 宽的文字整个看不见。
        """
        def label(parent, text: str) -> None:
            ttk.Label(parent, text=text, style="SubSunken.TLabel").pack(side="left", padx=(0, 4))

        # -- 参数区：功能码 / 地址 / 数量，写码下尾部长出「值 + 写入」--
        param = ttk.Frame(bar)
        param.pack(side="left")
        self._param_group = param

        self.fc_box = ttk.Combobox(param, values=[n for n, _ in FC_OPTIONS],
                                   state="readonly", width=15)
        self.fc_box.current(2)
        self.fc_box.bind("<<ComboboxSelected>>", lambda e: self._on_fc_change())
        self.fc_box.pack(side="left", padx=(0, SP_L))

        label(param, "地址")
        # 地址框是 Entry，不是 Spinbox。Spinbox 的箭头和滚轮按十进制整数加一减一，
        # 而地址有三套记法：十六进制下框里写着「1A」，Tk 把它读成 1，滚一格变 2、
        # 再滚一格变 1 —— 值没了，地址还悄悄换到了别处。地址是「点位」不是「旋钮」，
        # 本来也没人靠一格一格爬；而误触滚轮的代价，读码时是读错一片，写码时是写进
        # 别的寄存器。所以这里干脆不给滚轮留接口。
        self.addr_var = ttk.Entry(param, width=7)
        self._set_addr_text("0")
        # 记法守卫：0x 十六进制 / 0b 二进制 / PLC 六位（400001）都要敲得进去
        self._install_input_guard(self.addr_var, 65535, notation=True)
        self.addr_var.pack(side="left", padx=(0, 6))
        # 改地址要连带两件事：写入预览里的地址映射（改地址不刷新，映射就是在骗人），
        # 以及「用户自己写了 0x 前缀」时把记法下拉同步过去（见 _on_addr_key）
        self.addr_var.bind("<KeyRelease>", self._on_addr_key)
        # Tk 8.6 的 Entry 没有 Ctrl+A（8.7 才补上 <<SelectAll>>），而 Windows 上
        # 「全选→重打」是改地址的默认手法。没有它，框里残留的 0x0000 会跟新敲的
        # 0x4000 拼成 0x00000x4000，被守卫一个字符一个字符地拒掉 —— 看起来就是
        # 「0x4000 写不进去」。
        for seq in ("<Control-a>", "<Control-A>"):
            self.addr_var.bind(seq, self._select_all_addr)
        # Entry 本来就没有滚轮绑定，这里显式吃掉一次：将来谁把它换回 Spinbox，
        # 这条断言式的绑定会立刻把「滚轮改地址」挡回去，不至于又悄悄回归。
        self.addr_var.bind("<MouseWheel>", lambda _e: "break")

        # 记法下拉紧挨着地址框：两者是同一件事的两半（框里这串字按哪套编号读）。
        # 它原来藏在「数据定义」弹层里做单选框，而那个弹层管的是「值怎么解释」，
        # 地址记法混在里面既难找，也容易被当成数据类型的一部分。
        self.addr_notation = ttk.Combobox(param, values=[t for t, _ in ADDR_NOTATIONS],
                                          state="readonly", width=8)
        self.addr_notation.set(ADDR_NOTATIONS[0][0])
        self.addr_notation.bind("<<ComboboxSelected>>", self._on_addr_notation)
        self.addr_notation.pack(side="left", padx=(0, 10))

        label(param, "数量")
        self.count_var = ttk.Spinbox(param, from_=1, to=2000, width=5)
        self.count_var.set("10")
        self._install_input_guard(self.count_var, 2000)
        self.count_var.pack(side="left", padx=(0, 10))

        # 写码专用，读码下不 pack（而是禁用）—— 一个灰掉的强调色按钮是
        # 整条工具条最显眼的「你不能点我」，可读码时本来就没有写的语义
        self.write_label = ttk.Label(param, text="值", style="SubSunken.TLabel")
        self.write_var = ttk.Entry(param, width=9)
        self.write_var.bind("<KeyRelease>", lambda e: self._preview_write())
        self.write_btn = RoundedButton(param, text="写入", style="Accent.TButton", min_width=TOOLBAR_BTN,
                                    command=self._do_write_async)
        # 「这个值会占几个寄存器」的预览提示在独立的一行（write_hint_row，
        # 见 _build_master_tab）—— 放回这行工具条会把右侧按钮挤没。

        # -- 轮询区：间隔与启停，写码下整区隐藏（轮询只对读码有意义）--
        poll = ttk.Frame(bar)
        poll.pack(side="left")
        self._poll_group = poll
        ttk.Separator(poll, orient="vertical").pack(side="left", fill="y", padx=(0, SP_L))
        label(poll, "轮询")
        self.interval_var = ttk.Spinbox(poll, from_=50, to=60000, increment=50, width=5)
        self.interval_var.set("500")
        self._install_input_guard(self.interval_var, 60000)
        self.interval_var.pack(side="left")
        ttk.Label(poll, text="ms", style="SubSunken.TLabel").pack(side="left", padx=(3, 8))
        self.poll_btn = RoundedButton(poll, text="开始轮询", min_width=TOOLBAR_BTN,
                                      command=self._toggle_poll)
        self.poll_btn.pack(side="left")

        # -- 视图区：靠右对齐，由左及右是「数据是什么」→「怎么看」--
        # 组内一律 side="left"：明细视图下「格式」这一对要临时收起再插回，
        # 而 side="left" 的 before= 就是朴素意义上的「插到它左边」。
        # 原来组内是 side="right" + before=view_btn，插回时反而跑到按钮右边，
        # 切一次视图顺序就从「格式｜下拉｜明细」变成「明细｜下拉｜格式」。
        view = ttk.Frame(bar)
        view.pack(side="right")
        self._view_group = view
        # 「数据定义」比「显示设置」准确：里面全是「这串 16 位数字是什么含义」，
        # 没有一项是界面外观。而且工具条上已有「格式」（数值/十六进制/二进制），
        # 两个词都叫「显示」会分不清谁管什么。
        RoundedButton(view, text="数据定义", min_width=TOOLBAR_BTN,
                   command=self._open_view_dialog).pack(side="left", padx=(0, 10))
        self.fmt_label = ttk.Label(view, text="格式", style="SubSunken.TLabel")
        self.fmt_label.pack(side="left", padx=(0, 4))
        self.master_fmt_box = ttk.Combobox(view, values=FMT_OPTIONS,
                                           state="readonly", width=8)
        self.master_fmt_box.set(FMT_VALUE)
        self.master_fmt_box.pack(side="left", padx=(0, 10))
        self.master_fmt_box.bind("<<ComboboxSelected>>",
                                 lambda e: self._on_display_change())
        # 视图切换：一个按钮，标签就是另一侧视图的名字（在概览时写「明细」）。
        # 以前是「概览 / 明细」两段常驻、当前段高亮：一个动作被拆成两个热区，
        # 切换时得先读哪段是亮的、再去点另一段，还白占一个按钮的宽度。
        # 合成一个按钮后只有一处可点，也不用再维护「高亮哪一段」那套样式。
        self.master_view_btn = self._build_view_button(view, self._toggle_master_view)
        self._sync_master_view_switch()

    def _build_view_button(self, parent, on_pick) -> RoundedButton:
        """主站页与从站页共用的视图切换按钮。

        标签只写另一侧视图的名字：在概览时写「明细」，在明细时写「概览」。
        工具条上其余控件也都是名词（数据定义 / 格式 / 轮询），加一串动词
        反而比邻座长一截；「现在在哪个视图」由标签反推 —— 写着「明细」，
        说明点下去会去明细，也就是说此刻在概览。

        宽度定成 5 个字符单位（约 65px）：ttk 的 TButton 自带 11 字符最小宽
        （约 107px），两个字撑在那里面像个空胶囊。5 刚好容下两个字再留一点边，
        和左边「写入」这类两字按钮同宽，工具条看着才是一排按钮。

        两个页签共用同一个构件 —— 同一个概念（一张表怎么摊开看）在两处
        用两种控件，最容易让人以为它们不是一回事。
        """
        btn = RoundedButton(parent, text="明细", min_width=TOOLBAR_BTN, command=on_pick)
        btn.pack(side="left")
        return btn

    @staticmethod
    def _sync_view_button(btn, grid: bool) -> None:
        """标签写另一侧视图的名字：在概览写「明细」，在明细写「概览」。"""
        btn.configure(text="明细" if grid else "概览")

    def _build_status_bar(self, card) -> None:
        row = ttk.Frame(card, style="Sunken.TFrame",
                        padding=(PAD_CARD, SP_S, PAD_CARD, SP_S))
        row.grid(row=5, column=0, sticky="ew")

        self.master_status = ttk.Label(row, text="尚未通信", style="MutedSunken.TLabel")
        self.master_status.pack(side="left")
        # 鼠标指到哪个格子，这里就报它的绝对地址和值 —— 概览里"这是哪个寄存器"
        # 全靠这一行回答，不用再做行首地址 + 列偏移的心算。
        # 两个可选提示直到有内容才 pack 出去（见 _set_status_extra）：
        # 空标签也会画出几像素白底，三个并排看着像无意义的装饰条。
        self.cursor_label = ttk.Label(row, text="", style="MutedSunken.TLabel")
        # ASCII 模式专用：把这一屏的寄存器拼成一整串贴在状态栏上。
        # 概览里一格两个字符，横着读要人自己拼 —— 而读型号时想看的就是整串。
        self.ascii_label = ttk.Label(row, text="", style="MutedSunken.TLabel")

        self.log_toggle_btn = RoundedButton(row, text="▴ 日志 0", command=self._toggle_log_panel)
        self.log_toggle_btn.pack(side="right")
        self.trend_canvas = tk.Canvas(row, width=150, height=20, bg=SUNKEN, highlightthickness=0)
        # -- 迷你趋势条暂时从状态栏下架（2026-10-24）：绘制与点选逻辑全部保留
        #    （_draw_trend / _push_trend / _set_trend_key / _on_table_click 照常运行，
        #    只是画在一张不显示的画布上），恢复显示 = 取消下面这行的注释。
        # self.trend_canvas.pack(side="right", padx=(10, 12))
        self.stats_label = ttk.Label(row, text="", style="MutedSunken.TLabel")
        self.stats_label.pack(side="right")

    # -- 数据定义弹层（类型/字节序/系数/偏移量/单位）-------------------------

    def _build_view_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("数据定义")
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
        ttk.Label(body, text="寄存器数据的解释方式", style="CardTitle.TLabel").grid(
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
        self._dialog_row(body, 3, "比例系数", self.scale_var)

        # core.read/write 一直支持 bias，但弹层以前只有系数没有偏移，
        # 显示值 = 原始值 × scale + bias 的公式在界面上缺了一半
        self.bias_var = ttk.Entry(body, width=10)
        self.bias_var.insert(0, "0")
        self._dialog_row(body, 4, "偏移量", self.bias_var)

        self.unit_text_var = ttk.Entry(body, width=10)
        self._dialog_row(body, 5, "单位", self.unit_text_var)

        # 把换算关系写成一行公式：系数与偏移量各自是什么、按什么顺序作用，
        # 看公式比看两个输入框名更直接
        ttk.Label(body, text="显示值 = 原始值 × 比例系数 + 偏移量",
                  style="Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # 地址记法原来在这里（三种记法三个单选），现在搬到工具条上「地址」旁边
        # 的下拉 —— 它决定的是「地址框里那串字怎么读」，属于参数，不该埋在
        # 一个讲「数值怎么解释」的弹层里。

        # 「关闭」不是主操作：靠右、按内容定宽。整条铺满的灰条是全应用里
        # 最重的形状之一，放在这里会跟「填写参数」这件事抢注意力
        RoundedButton(body, text="关闭", command=self._close_view_dialog).grid(
            row=8, column=0, columnspan=2, sticky="e", pady=(SP_XL, 0))

        # 重建时把上次的设置放回去，别把用户选的 float32 悄悄变回 u16
        state = self._view_state
        if state:
            self.dtype_box.set(state.get("dtype", "无符号 16 位"))
            self.byteorder_box.set(state.get("byteorder", "ABCD"))
            self.scale_var.delete(0, "end")
            self.scale_var.insert(0, state.get("scale", "1"))
            self.bias_var.delete(0, "end")
            self.bias_var.insert(0, state.get("bias", "0"))
            self.unit_text_var.delete(0, "end")
            self.unit_text_var.insert(0, state.get("unit", ""))
            self.plc_var.set(bool(state.get("plc", False)))
            self._addr_hex.set(bool(state.get("addr_hex", False)))
            self._sync_addr_notation()

    def _dialog_row(self, parent, row: int, label: str, widget) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row, column=0, sticky="w", pady=4)
        widget.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        parent.columnconfigure(1, weight=1)

    # -- 报文解析弹层（粘贴一帧 hex，逐字段拆给人看）-----------------------

    def _build_parser_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("报文解析")
        win.configure(bg=CARD)
        win.withdraw()
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._close_parser_dialog)
        win.bind("<Escape>", lambda e: self._close_parser_dialog())

        body = ttk.Frame(win, style="Inner.TFrame", padding=(18, 16, 18, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        ttk.Label(body, text="粘贴一帧十六进制报文（自动识别 TCP / RTU 外壳）",
                  style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        input_row = ttk.Frame(body, style="Inner.TFrame")
        input_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.parser_mode = ttk.Combobox(
            input_row, values=("自动", "Modbus TCP", "Modbus RTU", "裸 PDU"),
            state="readonly", width=11)
        self.parser_mode.current(0)
        self.parser_mode.pack(side="left")
        RoundedButton(input_row, text="解析", style="Accent.TButton",
                   command=self._run_parser).pack(side="left", padx=(8, 0))
        RoundedButton(input_row, text="填入选中日志行",
                   command=self._parser_fill_from_log).pack(side="left", padx=(8, 0))

        self.parser_input = tk.Text(body, height=3, bg="#f6f8fb", fg=TEXT,
                                    font=LOG_FONT, relief="flat", padx=8, pady=6,
                                    insertbackground=TEXT, wrap="char")
        self.parser_input.grid(row=2, column=0, sticky="ew")
        ttk.Label(body, text="解析结果", style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(8, 2))
        self.parser_output = tk.Text(body, height=12, bg="#f6f8fb", fg=TEXT,
                                     font=LOG_FONT, relief="flat", padx=8, pady=6,
                                     state="disabled", wrap="word")
        self.parser_output.grid(row=4, column=0, sticky="nsew")
        RoundedButton(body, text="关闭", command=self._close_parser_dialog).grid(
            row=5, column=0, sticky="e", pady=(SP_L, 0))
        self._parser_dialog = win

    _PARSER_MODES = {"自动": "auto", "Modbus TCP": "tcp",
                     "Modbus RTU": "rtu", "裸 PDU": "pdu"}

    def _parser_fill_from_log(self) -> None:
        """把日志里最后一条 TX/RX 帧的 hex 填进输入框，省一次复制粘贴。"""
        content = self.log_text.get("1.0", "end")
        for line in reversed(content.splitlines()):
            s = line.strip()
            for tag in ("TX ", "RX "):
                pos = s.find(tag)
                if pos != -1 and pos <= 14:
                    hex_text = s[pos + len(tag):].strip()
                    if hex_text:
                        self.parser_input.delete("1.0", "end")
                        self.parser_input.insert("1.0", hex_text)
                        return
        self._log("日志里没有找到报文帧（TX/RX 行）")

    def _run_parser(self) -> None:
        mode = self._PARSER_MODES.get(self.parser_mode.get(), "auto")
        raw = self.parser_input.get("1.0", "end").strip()
        self.parser_output.configure(state="normal")
        self.parser_output.delete("1.0", "end")
        try:
            lines = parse_frame(raw, mode)
        except ValueError as exc:
            self.parser_output.insert("end", f"✕ {exc}\n", "err")
        except Exception as exc:               # 解析器自身的缺陷也不该无声无息
            self.parser_output.insert("end", f"✕ 内部错误：{exc}\n", "err")
        else:
            for line in lines:
                tag = "err" if "⚠" in line or "✕" in line else ""
                self.parser_output.insert("end", line + "\n", tag)
        self.parser_output.tag_configure("err", foreground=ERR)
        self.parser_output.configure(state="disabled")

    def _present_dialog(self, win) -> None:
        """把一个 Toplevel 按主窗口居中偏上摆好、置顶并接管模态。

        报文解析弹窗与字段视图弹窗原来各抄了一份 —— 居中算法改一次要改两处。
        """
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

    def _open_parser_dialog(self) -> None:
        if not getattr(self, "_parser_dialog", None) or not self._parser_dialog.winfo_exists():
            self._build_parser_dialog()
        self._toggle_log_panel(True)
        self._present_dialog(self._parser_dialog)

    def _close_parser_dialog(self) -> None:
        dialog = getattr(self, "_parser_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.withdraw()
            try:
                dialog.grab_release()
            except tk.TclError:
                pass

    def _open_view_dialog(self) -> None:
        if not self._view_dialog.winfo_exists():
            self._build_view_dialog()      # 极端情况下窗口被系统销毁过，重建一个
        self._sync_addr_notation()     # 下拉要和实际生效的记法一致
        self._present_dialog(self._view_dialog)

    def _set_addr_text(self, text: str) -> None:
        """写地址框里的文字。

        Entry 没有 Spinbox 的 set()，改文本得分 delete + insert 两步；
        这类动作集中一处，省得换控件时到处改。
        """
        self.addr_var.delete(0, "end")
        self.addr_var.insert(0, text)

    def _select_all_addr(self, _event=None) -> str:
        """地址框全选，并吃掉这次按键（不然 Ctrl+A 还会插一个「a」）。"""
        self.addr_var.selection_range(0, "end")
        self.addr_var.icursor("end")
        return "break"

    def _on_addr_key(self, _event=None) -> None:
        """地址框每次松键后：把 0x 前缀同步到记法下拉，再刷新写入预览。

        用户在框里亲手写下 0x，就是当场声明「这是十六进制地址」。这句声明比
        下拉更具体，界面必须跟着它走 —— 否则停在下拉写着「PLC 地址」时输入
        0x4000，会被当成 PLC 编号 16384，报一句「属于离散输入」，而用户看到的
        只是「0x4000 不能用」。同步只改下拉与内部记法，不动框里的字，所以
        打字过程中值不会被反复换算。
        """
        raw = _normalize_number(self.addr_var.get()).strip().lower()
        if raw.startswith("0x") and not self._addr_hex.get():
            self._addr_hex.set(True)
            self.plc_var.set(False)
            self._sync_addr_notation()
            self._on_display_change()
        self._preview_write()

    def _on_addr_notation(self, _event=None) -> None:
        """换了地址记法：把框里已有的值翻译过去，再整屏重画。

        翻译是必须的 —— 下拉说的是「这串字怎么读」，同一串「10」在十进制下
        是十号寄存器、在十六进制下是十六号。留着原文不换算，用户切一下
        记法，读的地址就悄悄变了；换算成同一处寄存器，切换才是纯显示变化。
        """
        fc = self._current_fc()
        mode = ADDR_NOTATION_KEYS.get(self.addr_notation.get(), "dec")
        try:
            protocol = self._address_field(fc)      # 先按旧记法读成协议地址
        except ValueError:
            protocol = None                        # 框里本来就是半截输入，别动它
        self._addr_hex.set(mode == "hex")
        self.plc_var.set(mode == "plc")
        if protocol is not None:
            self._set_addr_text(self._addr_text(protocol, fc))
        # 刚换完记法，下一步几乎一定是要改地址（0x0000 → 0x4000 这种）。
        # 顺手全选，让第一下按键就是替换而不是追加。
        if self.addr_var.get():
            self.addr_var.selection_range(0, "end")
            self.addr_var.icursor("end")
        self._preview_write()                      # 写入预览里的地址映射跟着变
        self._on_display_change()                  # 表头地址、状态栏回声

    def _sync_addr_notation(self) -> None:
        """把两个内部布尔反过来对齐到工具条下拉。

        这两个布尔是真正的状态（渲染/解析/弹层恢复都读它们），下拉只是它们的
        编辑器；别处改了布尔（弹层恢复、测试直接 set），这里对齐一次，
        免得下拉写着「十进制」而实际已经按十六进制在解析。
        """
        box = getattr(self, "addr_notation", None)
        if box is None:          # 工具条还没建（弹层先于工具条构建）
            return
        if self.plc_var.get():
            mode = "plc"
        elif self._addr_hex.get():
            mode = "hex"
        else:
            mode = "dec"
        box.set({key: label for label, key in ADDR_NOTATIONS}[mode])

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
        cfg_card.grid(row=0, column=0, sticky="nsew", padx=(0, SP_L))

        # 「模式」这一行必须和下面的 _form_row 用同一套 grid 网格：
        # 原来是 pack(side="left") 手排，标签宽度和下拉宽度都不受网格约束，
        # 于是它比下面四个输入框整体左移约 40px 一条线都对齐不了。
        mode_row = ttk.Frame(cfg_body, style="Inner.TFrame")
        mode_row.pack(fill="x")
        mode_row.columnconfigure(1, weight=1)
        self.slave_mode = tk.StringVar(value="tcp")
        ttk.Label(mode_row, text="模式", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=SP_S)
        self.slave_mode_box = ttk.Combobox(mode_row, values=list(SLAVE_MODE_LABELS.values()),
                                           state="readonly", width=18)
        self.slave_mode_box.set(SLAVE_MODE_LABELS["tcp"])
        self.slave_mode_box.bind("<<ComboboxSelected>>", self._on_slave_box_selected)
        self.slave_mode_box.grid(row=0, column=1, sticky="ew", padx=(SP_L, 0), pady=SP_S)

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
        self._install_input_guard(self.slave_port, 65535)
        self._form_row(self.slave_tcp_frame, 1, "端口", self.slave_port)
        # 提示挂在输入框列（column=1），左边缘与输入框对齐 —— 落在标签列会被
        # 当成第三个字段名，而它其实是「监听地址」这个字段的补充说明
        ttk.Label(self.slave_tcp_frame, text="局域网访问请改成 0.0.0.0",
                  style="Muted.TLabel").grid(row=2, column=1, sticky="w",
                                             padx=(SP_L, 0), pady=(SP_XS, 0))

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

        ttk.Separator(cfg_body).pack(fill="x", pady=SP_L)

        common = ttk.Frame(cfg_body, style="Inner.TFrame")
        common.pack(fill="x")
        self.slave_unit = ttk.Spinbox(common, from_=1, to=247, width=8)
        self.slave_unit.set("1")
        self._install_input_guard(self.slave_unit, 247)
        self._form_row(common, 0, "站地址", self.slave_unit)
        self.slave_size = ttk.Spinbox(common, from_=1, to=65536, width=8)
        self.slave_size.set("10000")
        self._install_input_guard(self.slave_size, 65536)
        self._form_row(common, 1, "数据区长度", self.slave_size)

        # 不再整宽铺一条淡红色带：危险操作和主操作同尺寸、同位置，
        # 只靠「红字 + 淡红底」区分语义 —— 全宽色块会把页面上最重的视觉
        # 压在「停止」上，而现场 99% 的时间是在「启动」之后看数据
        self.slave_btn = RoundedButton(cfg_body, text="启动从站", style="Accent.TButton",
                                       command=self._toggle_server)
        self.slave_btn.pack(fill="x", pady=(SP_XL, 0))

        self.demo_var = tk.BooleanVar(value=True)
        RoundCheck(cfg_body, text="模拟数据变化（输入寄存器/线圈/离散输入）",
                        variable=self.demo_var, bg=CARD).pack(anchor="w", pady=(SP_L, 0))

        self.slave_stat = ttk.Label(cfg_body, text="未启动", style="Muted.TLabel",
                                    justify="left", wraplength=250)
        self.slave_stat.pack(anchor="w", pady=(SP_L, 0))

        # -- 故障注入：把「从站突然不回/回了半帧/CRC 坏了」按需造出来，
        #    验证自己写的上位机会不会崩、会不会把异常当 0。真实设备出问题
        #    时往往不给你复现机会，这里是唯一能"造故障"的地方。
        ttk.Separator(cfg_body).pack(fill="x", pady=(SP_L, SP_M))
        ttk.Label(cfg_body, text="故障注入（验证上位机容错）",
                  style="CardTitle.TLabel").pack(anchor="w", pady=(0, 6))
        fault = ttk.Frame(cfg_body, style="Inner.TFrame")
        fault.pack(fill="x")
        fault.columnconfigure(1, weight=1)
        fault.columnconfigure(3, weight=1)
        self._fault_labels = {}           # 字段名 → 标题标签；CRC 字段禁用时要连标题一起变灰

        def fault_field(row: int, col: int, label: str, initial: str,
                        maximum: int, width: int = 6):
            lbl = ttk.Label(fault, text=label, style="Card.TLabel")
            lbl.grid(row=row, column=col, sticky="w", pady=2)
            box = ttk.Spinbox(fault, from_=0, to=maximum, width=width)
            box.set(initial)
            self._install_input_guard(box, maximum)
            box.grid(row=row, column=col + 1, sticky="w", padx=(4, 10), pady=2)
            self._fault_labels[label] = lbl
            return box

        self.fault_drop = fault_field(0, 0, "不响应 %", "0", 100)
        self.fault_delay = fault_field(0, 2, "延迟 ms", "0", 60000)
        self.fault_exc = fault_field(1, 0, "异常 %", "0", 100)
        self.fault_exc_code = fault_field(1, 2, "异常码", "4", 255)
        self.fault_trunc = fault_field(2, 0, "截断 %", "0", 100)
        self.fault_crc = fault_field(2, 2, "坏CRC %", "0", 100)
        # 「坏 CRC 仅 RTU / RTU over TCP 生效」不再用文字说明 —— 帧里有没有
        # CRC 由 _sync_crc_field 直接表态：TCP 模式字段禁用、标题变灰；
        # RTU 系模式恢复正常。状态比常驻说明更准（跟实际模式走）。
        self._sync_crc_field()

        fault_actions = ttk.Frame(cfg_body, style="Inner.TFrame")
        fault_actions.pack(fill="x", pady=(8, 0))
        RoundedButton(fault_actions, text="应用", command=self._apply_faults).pack(side="left")
        RoundedButton(fault_actions, text="清除故障",
                   command=self._clear_faults).pack(side="left", padx=(6, 0))
        self.fault_stat = ttk.Label(cfg_body, text="未注入", style="Muted.TLabel",
                                    justify="left", wraplength=250)
        self.fault_stat.pack(anchor="w", pady=(6, 0))

        # -- 右：寄存器视图 --
        view_card, view_body = self._card(
            container, "寄存器视图",
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
        self._install_input_guard(self.view_start, 65535)
        self.view_start.pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, text="数量", style="Card.TLabel").pack(side="left", padx=(10, 0))
        self.view_count = ttk.Spinbox(toolbar, from_=1, to=500, width=6)
        self.view_count.set("32")
        self._install_input_guard(self.view_count, 500)
        self.view_count.pack(side="left", padx=(6, 0))
        RoundedButton(toolbar, text="刷新", command=self._refresh_slave_table).pack(side="left", padx=(10, 0))
        RoundedButton(toolbar, text="全部清零", command=self._clear_slave).pack(side="left", padx=(6, 0))
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
            ("dec", FMT_VALUE, 120, "e"),
            ("hex", FMT_HEX, 98, "e"),
            ("bin", FMT_BIN, 220, "w"),
        ):
            self.slave_tree.heading(key, text=text, anchor=anchor)
            self.slave_tree.column(key, width=width, anchor=anchor, stretch=(key == "bin"))
        self.slave_tree.grid(row=0, column=0, sticky="nsew")
        sbar = ttk.Scrollbar(self.slave_detail_wrap, orient="vertical",
                             command=self.slave_tree.yview)
        sbar.grid(row=0, column=1, sticky="ns", padx=(SP_XS, 0))
        self.slave_tree.configure(yscrollcommand=sbar.set)
        self.slave_tree.tag_configure("odd", background="#f6f8fb")
        self.slave_tree.bind("<Double-1>", self._begin_cell_edit)
        self.slave_tree.bind("<Motion>", self._slave_hover)
        self.slave_tree.bind("<Leave>", self._slave_hover_leave)

        # -- 概览：行头补零起始地址，每行 N 格，格式跟「格式」下拉走 --
        self.slave_grid_wrap = ttk.Frame(tree_wrap, style="Inner.TFrame")
        self.slave_grid_wrap.rowconfigure(0, weight=1)
        self.slave_grid_wrap.columnconfigure(0, weight=1)
        self.slave_grid = ttk.Treeview(self.slave_grid_wrap, style="Grid.Treeview",
                                       show="headings", height=19)
        self._setup_grid(self.slave_grid, 10, 66, "e")
        self.slave_grid.grid(row=0, column=0, sticky="nsew")
        gsbar = ttk.Scrollbar(self.slave_grid_wrap, orient="vertical",
                              command=self.slave_grid.yview)
        gsbar.grid(row=0, column=1, sticky="ns", padx=(SP_XS, 0))
        self.slave_grid.configure(yscrollcommand=gsbar.set)
        # 列宽会平分，横向滚动条没有用处，只会占掉一行高度
        self.slave_grid.tag_configure("odd", background="#f6f8fb")
        self.slave_grid.bind("<Double-1>", self._begin_grid_edit)
        self.slave_grid.bind("<Motion>", self._slave_hover)
        self.slave_grid.bind("<Leave>", self._slave_hover_leave)

        # 两个视图叠在同一格；默认亮概览
        self.slave_grid_wrap.grid(row=0, column=0, sticky="nsew")
        self.slave_detail_wrap.grid(row=0, column=0, sticky="nsew")
        self.slave_detail_wrap.grid_remove()

    # -------------------------------------------------------------- 日志

    def _build_log(self) -> None:
        """日志做成停靠在窗口底部的面板：默认收起，点开展开后数据区让位。

        以前它是 place 覆盖层 —— 直接盖在数据区底部 180px 上，明细表格
        滚到最后几行正好压在日志底下，「滑到最下面」也看不见（2026-10-24
        用户报障）。改成真实打包的面板后，打开时整个数据区收缩让位，
        表格、滚动条、状态栏全部可见，一个像素都不遮。
        面板归主站页管：切到从站页自动收起，切回来原样恢复（见 _switch_tab）。
        """
        panel = tk.Frame(self.root, bg=CARD, height=232,
                         highlightthickness=1, highlightbackground=BORDER)
        self.log_panel = panel
        self._log_open = False

        head = tk.Frame(panel, bg=CARD)
        head.pack(fill="x", padx=PAD_CARD, pady=(SP_S, SP_S))
        ttk.Label(head, text="通信日志", style="CardTitle.TLabel").pack(side="left")
        # 收起只是关闭，不是一组操作里的一个 —— 用幽灵样式退到背景，
        # 让「清空/导出/解析」这些有副作用的动作留在视觉前台
        RoundedButton(head, text="收起", style="Ghost.TButton", bg=CARD,
                      command=lambda: self._toggle_log_panel(False)).pack(
                          side="right", padx=(SP_M, 0))
        # 不带省略号：这三个字本身就是动作，加上「…」只在暗示「后面还有弹窗」，
        # 而它其实是一个当场打开的面板。
        RoundedButton(head, text="解析", min_width=72,
                   command=self._open_parser_dialog, bg=CARD).pack(side="right", padx=(0, SP_S))
        self.log_pause_btn = RoundedButton(head, text="暂停", min_width=72,
                                        command=self._toggle_log_pause)
        self.log_pause_btn.pack(side="right", padx=(0, SP_S))
        # 导出就一个动作：把日志面板里的文字存成 TXT。曾经这里是分裂按钮，
        # 窄条里揣着「导出 CSV」和「录制数据到 CSV」——前者只是把同样的文字
        # 换个分隔符，后者是另一件事（数值采样），两样摆一块看着就像重复，
        # 而现场真正需要的只有「把这段日志存下来」这一件。
        self.export_btn = RoundedButton(head, text="导出", min_width=72, bg=CARD,
                                        command=self._export_log)
        self.export_btn.pack(side="right", padx=(0, SP_S))
        RoundedButton(head, text="清空", min_width=72,
                   command=self._clear_log, bg=CARD).pack(side="right", padx=(0, SP_S))

        body = tk.Frame(panel, bg=CARD)
        body.pack(fill="both", expand=True, padx=PAD_CARD, pady=(0, SP_M))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.log_text = tk.Text(body, height=8, bg="#f6f8fb", fg=TEXT, font=LOG_FONT,
                                relief="flat", wrap="none", state="disabled",
                                insertbackground=TEXT, padx=SP_M, pady=SP_M,
                                spacing1=1, spacing3=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        bar.grid(row=0, column=1, sticky="ns", padx=(SP_XS, 0))
        self.log_text.configure(yscrollcommand=bar.set)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("time", foreground=MUTED, font=MONO)
                # frame 原来单独用 MONO，中文 fallback 的行高比 Consolas 高，
        # 于是「帧行」和「读 FC03」两种行高来回跳，扫读一直被切断
        self.log_text.tag_configure("frame", foreground=MUTED, font=LOG_FONT)
        self.log_text.tag_configure("fc", foreground=LOG_FC, font=LOG_FONT)
        self.log_text.tag_configure("payload", foreground=LOG_PAYLOAD, font=MONO)
        self.log_text.tag_configure("crc", foreground=LOG_CRC, font=MONO)
        self.log_text.tag_configure("tx", foreground=LOG_DIR_TX, font=LOG_FONT)
        self.log_text.tag_configure("rx", foreground=LOG_DIR_RX, font=LOG_FONT)
        # 高度钉死 210px（与旧覆盖层观感一致），内部行高不随内容涨
        panel.pack_propagate(False)

    def _toggle_log_panel(self, show: bool | None = None) -> None:
        if show is None:
            show = not self._log_open
        self._log_open = bool(show)
        if self._log_open:
            self._show_log_panel()
        else:
            self.log_panel.pack_forget()
        self._update_log_button()

    def _show_log_panel(self) -> None:
        """把日志面板停靠到窗口底部（stack 之下），数据区整体让位。

        after=stack 保证排在数据区之后。参数表单已经收进顶栏的浮层，
        不再参与这里的 pack 顺序。
        """
        self.log_panel.pack(fill="x", after=self.stack)
        self.log_text.see("end")

    def _update_log_button(self) -> None:
        button = getattr(self, "log_toggle_btn", None)
        if button is None:
            return
        arrow = "▾" if self._log_open else "▴"
        button.configure(text=f"{arrow} 日志 {self._log_lines}")

    def _toggle_log_pause(self) -> None:
        """暂停只停「往界面上画」，条目缓存起来，「继续」后原样补回。

        早期实现把暂停期间的条目直接丢掉只留条数 —— 名字叫「暂停」，
        行为却是「丢弃」，联调时翻旧帧恰恰最容易漏掉暂停那几秒的报文。
        """
        if not self._log_paused:
            self._append_log("日志已暂停（新条目缓存，点「继续」补回）")
            self._log_paused = True
            self.log_pause_btn.configure(text="继续")
            return
        self._log_paused = False
        self.log_pause_btn.configure(text="暂停")
        held, self._log_held = self._log_held, []
        # 批量灌回去：逐条走 _append_log 的话，每条都要 see("end") 一次，
        # 几千条就是几十秒的白屏。这里只插内容，滚动和计数在循环后统一处理。
        for text, tag in held:
            self._emit_log(text, tag)
        if held:
            self._append_log(f"（暂停结束，补记 {len(held)} 条）")
        self.log_text.see("end")
        self._update_log_button()

    def _export_log(self) -> None:
        content = self.log_text.get("1.0", "end").strip("\n")
        if not content:
            self._notify("日志是空的，没有可导出的内容", error=True)
            return
        path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            initialfile=f"modbus-log-{time.strftime('%Y%m%d-%H%M%S')}.txt",
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
        # 镜像进滚动文件。挂在 _post 之前：界面「暂停」只停画图，
        # 文件侧一条不少 —— 事后追查要的正是被暂停遮住的那段。
        if self.logger is not None:
            try:
                self.logger.info("%s", text)
            except Exception:
                pass
        self._post("log", text, tag)

    def _notify(self, text: str, error: bool = False) -> None:
        """主站页的提示统一走状态栏 + 日志。

        刻意不用 messagebox：现场调试时模态框会打断连续操作，
        而且脚本化驱动界面时弹窗会直接把进程卡住。
        """
        self.master_status.configure(text=text, foreground=ERR if error else MUTED)
        self._log(text, "err" if error else "")

    def _append_log(self, text: str, tag: str = "") -> None:
        if self._log_paused:
            # 暂停 = 缓存，不是丢弃：恢复时按原顺序补回。
            # 缓存本身也得有上限 —— 挂着暂停过一夜再点「继续」，几十万条一次性
            # 补进去会把界面冻住；和主窗口同尺度、按批丢掉最旧的那部分。
            self._log_held.append((text, tag))
            if len(self._log_held) > LOG_MAX_LINES:
                del self._log_held[:LOG_TRIM_STEP]
            return
        # 只有本来就贴在底部时才跟随。否则用户往上翻旧帧，每条新报文都会把他拽回底。
        try:
            at_bottom = self.log_text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        self._emit_log(text, tag)
        if at_bottom:
            self.log_text.see("end")
        self._update_log_button()

    def _emit_log(self, text: str, tag: str = "") -> None:
        """往 Text 里落一条，不负责跟随滚动和按钮计数。

        单独拆出来是为了「补记」：暂停期间攒下的几千条要一次性灌回去，
        每条都 see("end") + 刷按钮的话，光滚动定位就够卡上十几秒。
        """
        # 毫秒级时间戳：相邻报文的间隔本来就在 ms 量级，秒级看不出节奏；
        # 文件侧（applog）的 .mmm 与这里格式一致，事后对帧能逐条对上。
        # 用 datetime 的整数微秒除下来，不走 float 取整，避免毫秒进位错秒。
        now = datetime.now()
        stamp = f"{now:%H:%M:%S}.{now.microsecond // 1000:03d}"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] ", "time")
        # 方向词单独上色：一屏几十帧，最先要看的是「这行是我发的还是它回的」
        head = text[:2]
        if head in ("TX", "RX"):
            self.log_text.insert("end", head, "tx" if head == "TX" else "rx")
            self.log_text.insert("end", text[2:] + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n", tag)
        # 按真实行数记：一条日志可能自带换行（异常栈、多行提示），只按「条」
        # 累加的话，窗口里实际行数会比上限大出好几倍。
        self._log_lines += text.count("\n") + 1
        if self._log_lines > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{LOG_TRIM_STEP + 1}.0")
            # 一条日志自带换行时（异常栈、多行提示），上面的计数会小于真实
            # 行数，攒久了窗口就会比上限大。裁剪时按真实行号校准一次。
            self._log_lines = int(self.log_text.index("end-1c").split(".")[0])
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_lines = 0
        self._log_held.clear()      # 「清空」是全清：暂停期间还没补回的缓存也一并丢
        self._update_log_button()   # 计数归零要落到按钮上，不然显示的是清空前的旧数

    # ------------------------------------------------------------ 主站逻辑

    def _on_link_box_selected(self, _event=None) -> None:
        """下拉框选中 → 先把显示标签换算回内部键，再走统一的切换逻辑。"""
        self.link_mode.set(LINK_KEYS.get(self.link_box.get(), "tcp"))
        self._on_link_mode_change()

    def _on_link_mode_change(self) -> None:
        mode = LINK_KEYS.get(self.link_mode.get(), self.link_mode.get())
        if mode != self.link_mode.get():      # 手滑把显示标签塞进了变量也能归位到键
            self.link_mode.set(mode)
        self.link_box.set(LINK_LABELS.get(mode, "TCP"))
        self.tcp_frame.pack_forget()
        self.rtu_frame.pack_forget()
        # RTU over TCP 用的是 TCP 链路参数（主机/端口），复用同一组输入框；
        # 差别只在流里跑的帧格式，由 _build_transport 按模式区分。
        if mode in ("tcp", "rtutcp"):
            self.tcp_frame.pack(fill="x")
        else:
            self.rtu_frame.pack(fill="x")

    def _on_slave_box_selected(self, _event=None) -> None:
        self.slave_mode.set(SLAVE_MODE_KEYS.get(self.slave_mode_box.get(), "tcp"))
        self._on_slave_mode_change()

    def _on_slave_mode_change(self) -> None:
        mode = SLAVE_MODE_KEYS.get(self.slave_mode.get(), self.slave_mode.get())
        if mode != self.slave_mode.get():
            self.slave_mode.set(mode)
        self.slave_mode_box.set(SLAVE_MODE_LABELS.get(mode, "TCP 监听"))
        self.slave_tcp_frame.pack_forget()
        self.slave_rtu_frame.pack_forget()
        # RTU over TCP 透传同样监听 TCP 端口，复用地址/端口输入框
        if mode in ("tcp", "rtutcp"):
            self.slave_tcp_frame.pack(fill="x")
        else:
            self.slave_rtu_frame.pack(fill="x")
        self._sync_crc_field()      # 坏CRC 字段跟模式表态（TCP 帧没有 CRC）

    def _on_fc_change(self) -> None:
        fc = FC_OPTIONS[self.fc_box.current()][1]
        is_read = fc in (1, 2, 3, 4)
        if self._polling and not is_read:
            # 轮询只对读码有意义：切到写码立刻停，别等 5 次失败计数慢慢凑。
            # 记一笔「是被功能码停掉的」，切回读码时好自己接上
            self._poll_resume_on_read = True
            self._stop_poll()
        self._sync_request_controls()
        self._prev_cells = None
        if is_read:
            self.count_var.configure(state="normal")
            self.write_label.pack_forget()
            self.write_var.pack_forget()
            self.write_btn.pack_forget()
            self.write_hint_row.grid_remove()
            # 参数区后面接回轮询区（写码下整区折叠过）
            self._poll_group.pack(side="left", after=self._param_group)
            if (self._poll_resume_on_read and self.client is not None
                    and not self._polling):
                self._poll_resume_on_read = False
                self._autostart_poll()
        else:
            self.write_label.pack(side="left", padx=(0, 4))
            self.write_var.pack(side="left", padx=(0, 4))
            self.write_btn.pack(side="left")
            # 轮询区整区收起：写码下它一个控件都用不上，留着只是占宽
            self._poll_group.pack_forget()
            self.write_hint_row.grid(row=1, column=0, sticky="ew")
            limit = write_limit(fc) or 123
            self.count_var.configure(state="disabled")
            if fc == 22:
                self._log("掩码写：值填 AND,OR 两个掩码（支持 0x 十六进制），"
                          "只改 AND 为 0 的那些位")
            else:
                self._log(f"功能码 {fc:02d} 为写操作，数量由写入值的个数决定（上限 {limit}）")
        self._preview_write()

    def _preview_write(self) -> None:
        """在工具条下方的预览行实时告诉你：这串值占几个寄存器、落在哪个地址。

        FC16 里「填了几个值」和「写了几个寄存器」不是一回事 —— float32 一个值
        占 2 个、float64 占 4 个，这个数字以前要等发完帧才知道。而「哪个地址
        写的是哪个值」从来没人说 —— Modbus Poll / QModMaster 都把地址钉在
        每一格上，这里的批量写是一根输入条，就得把映射写在提示里。
        """
        label = getattr(self, "write_hint", None)
        if label is None:
            return
        fc = self._current_fc()
        if fc not in (5, 6, 15, 16, 22):
            label.configure(text="")
            return
        raw = _normalize_number(self.write_var.get())
        if fc == 22:
            items = _split_values(raw)
            label.configure(text="→ 掩码写 1 个寄存器" if len(items) == 2
                            else "→ 需要两个掩码（AND,OR）")
            return
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
            tail = self._write_map_ascii(fc, count, raw)
            label.configure(text=f"→ {count} 个寄存器（{count * 2} 字符）{tail}")
            return
        # 个数统计必须和真正写入时的解析口径一致（parsing.split_values）。
        # 以前这里只替换全角逗号/分号，漏了顿号和半角分号 —— 填 `1、2、3`
        # 提示「1 个寄存器」，实际写进去 3 个。
        items = _split_values(self.write_var.get())
        if not items:
            label.configure(text="—")
            return
        if fc == 6:
            # 口径跟写入路径一致：先查个数再查宽度，报的错才对得上
            if len(items) != 1:
                label.configure(text="→ 单点写只能填一个值")
                return
            sp = span(self._current_dtype())
            if sp == 1:
                label.configure(text=f"→ 1 个寄存器{self._write_map_text(fc, items, 1)}")
            else:
                label.configure(text=f"→ 占 {sp} 个寄存器，单点写放不下")
            return
        if fc == 5:
            label.configure(text=f"→ 1 个线圈{self._write_map_text(fc, items, 1)}")
            return
        if fc == 15:
            label.configure(
                text=f"→ {len(items)} 个线圈{self._write_map_text(fc, items, 1)}")
            return
        sp = span(self._current_dtype())
        count = len(items) * sp
        label.configure(
            text=f"→ {count} 个寄存器{self._write_map_text(fc, items, sp)}")

    def _write_map_text(self, fc: int, items: list, sp: int) -> str:
        """「：[100]=1 [101]=2 [102-103]=220.5」—— 每个值落在哪个地址。

        地址解析失败（还没填、记法不对）就退回不带映射，只报数量。
        映射用表格里显示的地址记法，跟读回来的表能直接对上。
        """
        try:
            addr = self._address_field(fc)
        except (ValueError, AddressError):
            return ""
        base = self._display_address(addr, fc)
        text = self._register_map(base, items, sp)
        return f"：{text}" if text else ""

    def _write_map_ascii(self, fc: int, count: int, raw: str) -> str:
        """ASCII 写入的地址映射：整段字符串落在 [起~止]。"""
        try:
            addr = self._address_field(fc)
        except (ValueError, AddressError):
            return ""
        base = self._display_address(addr, fc)
        shown = raw if len(raw) <= 8 else raw[:7] + "…"
        return f"：[{base}~{base + count - 1}]=\"{shown}\""

    @staticmethod
    def _register_map(address: int, items: list, sp: int = 1,
                      limit: int = 4) -> str:
        """「[100]=1 [101]=2 [102-103]=220.5」—— 地址到值的映射串。

        sp 是一个值占的寄存器数；超过 limit 个只列前几个，防止提示行被
        一长串地址占满。
        """
        parts = []
        for i, item in enumerate(items):
            a = address + i * sp
            rng = f"{a}" if sp == 1 else f"{a}-{a + sp - 1}"
            text = f"{item:g}" if isinstance(item, float) else str(item)
            if len(text) > 10:
                text = text[:9] + "…"
            parts.append(f"[{rng}]={text}")
            if len(parts) >= limit and i + 1 < len(items):
                parts.append(f"…（共 {len(items)} 个）")
                break
        return " ".join(parts)

    def _build_transport(self):
        mode = self.link_mode.get()
        try:
            timeout = float(self.timeout_var.get())
        except ValueError:
            raise ValueError("超时时间必须是数字") from None
        # 按键护栏只拦 >30 和非数字，手敲 0 也进得来 —— 超时 0 秒等于
        # 永远连不上/读不上，必须在参数层拦下并给出台阶
        if not 0.1 <= timeout <= 30:
            raise ValueError("超时时间要在 0.1~30 秒之间")
        if mode in ("tcp", "rtutcp"):
            host = self.tcp_host.get().strip() or "127.0.0.1"
            try:
                port = int(self.tcp_port.get())
            except ValueError:
                raise ValueError("端口必须是整数") from None
            if mode == "tcp":
                return TcpTransport(host, port, timeout=timeout)
            return RtuOverTcpTransport(host, port, timeout=timeout)
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
            unit = self._int_field(self.unit_var, "设备地址", 1, 247)
            retries = self._int_field(self.retry_var, "重试次数", 0, 5)
        except (ValueError, RuntimeError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return

        self._busy = True
        # 文案取「连接中」而不是「连接中…」：定宽 96 正好容下三种状态，
        # 按钮不会在连接瞬间抽一下，右边的状态胶囊也就不会跟着跳
        self.connect_btn.configure(state="disabled", text="连接中")
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
                reason = self._friendly_error(error)
                self.connect_btn.configure(text="连接")
                self._set_link_state(False, "连接失败")
                # 失败原因必须进状态栏 —— 只写日志的话，用户看到的就是
                # 「点了没反应」：按钮闪一下回到「连接」，别的什么都没有
                self.master_status.configure(text=f"连接失败：{reason}", foreground=ERR)
                self._log(f"连接失败：{reason}", "err")
                return
            self.client = result
            self._set_link_state(True, transport.label)
            self._refresh_stats()
            self._log(f"已连接 {transport.label}，设备地址 {unit}", "ok")
            # 连成了才记：这组参数已被证明能用，值得成为下次的默认值
            self._save_link_settings()
            # 「我没起从站怎么也连上了」最常见的一个答案：连到了本页自己
            # 启动的从站（本机自环联调）。自家这个能精确对上，点一句；
            # 另一个实例的从站这里看不到 —— 但 TCP 能连上就说明那头真有
            # 人在监听，本机 502 被占时先想这一层。
            host = getattr(transport, "host", None)
            unit_mismatch = False
            if (host in ("127.0.0.1", "localhost", "::1")
                    and self.server is not None
                    and getattr(transport, "port", None) == self.server.port):
                self._log(f"自环联调：{host}:{transport.port} 是本页启动的从站", "ok")
                # 站地址对不上时趁连接这一刻就说破，别等用户读到超时才猜原因
                try:
                    if int(unit) != int(self.server.unit):
                        unit_mismatch = True
                        self._log(f"注意：主站设备地址 {unit} 与从站站地址 "
                                  f"{self.server.unit} 不一致 —— 从站不会应答，"
                                  f"读取会超时，把主站「设备地址」改成 "
                                  f"{self.server.unit}", "err")
                except (AttributeError, TypeError, ValueError):
                    pass
            # 上面那些一次性提示都说完之后再开轮询：轮询一跑，状态栏就被
            # 「轮询中 · 地址 …」长期占住，连接瞬间最该看的那句话会被顶掉。
            # 已知站地址对不上就别开了 —— 五次超时只会把这句提示刷出视野。
            if unit_mismatch:
                self._log("设备地址不一致，已跳过自动轮询", "err")
            else:
                # 先静默问一句「对面是谁」，再进入轮询
                self._probe_device_then_poll()

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
            # 断开就清掉，否则下次连另一台设备时胶囊还挂着上一台的型号
            self._device_note = ""
        self._render_link()
        connected_now = self.client is not None
        self.connect_btn.configure(text="断开" if connected else "连接")
        # 浮层底部那颗按钮跟着状态走：连着的时候点它就是断开，
        # 文案还写「连接」会让人以为要重连
        self.conn_go.configure(
            text="断开" if connected_now else "连接",
            style="Danger.TButton" if connected_now else "Accent.TButton")
        # 连接按钮/状态灯/浮层的去留统一交给 _sync_master_chrome：
        # 它同时看「在哪个页」和「连没连」，在从站页断开也不会把主站
        # 控件弹到从站页上
        self._sync_master_chrome()

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

    def _unit_mismatch_hint(self) -> str | None:
        """本机自环下主站站地址与从站不一致的人话提示；其余场景返回 None。

        TCP 从站对站地址不符的请求是「静默丢弃」（协议标准行为）——
        TCP 能连上只说明端口有人在听，读却只能等超时。这是「连接成功
        但读取不行」最常见的原因。只有连的是本页自己启动的从站时才能
        精确对出这个错，连别的设备时不乱猜。
        """
        client, server = self.client, self.server
        if client is None or server is None:
            return None
        host = getattr(client.transport, "host", None)
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        if getattr(client.transport, "port", None) != server.port:
            return None
        try:
            if int(client.unit) == int(server.unit):
                return None
        except (AttributeError, TypeError, ValueError):
            return None
        return (f"主站设备地址 {client.unit} 与本从站站地址 {server.unit} 不一致，"
                f"从站不会应答 —— 把主站「设备地址」改成 {server.unit}")

    def _friendly_error(self, exc: BaseException) -> str:
        """给用户看的错误一句话；英文原文（Python 内部错误）只写日志。"""
        text = describe_error(exc).strip() or exc.__class__.__name__
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            self._log(f"内部错误：{exc.__class__.__name__}: {text}", "err")
            return "操作失败（内部错误），细节已写入日志"
        # 超时类失败 + 本机自环 + 站地址不一致：把最可能的原因直接递到眼前，
        # 不让用户在「连接成功却读不通」里干瞪眼
        hint = self._unit_mismatch_hint()
        if hint and any(k in text for k in ("超时", "无响应", "timed out")):
            return f"{text}（{hint}）"
        return text

    def _parse_write_values(self, fc: int, dtype: str | None = None) -> list | None:
        """解析「写入值」输入框。切分与类型转换见 parsing.parse_write_values。

        GUI 只负责把错误变成状态栏提示 —— 早期这里自己切分，分隔符集合
        与 CLI 不一致（少了中文顿号），两边对同一串输入的解释会不一样。
        dtype 可强制覆盖「数据定义」弹层里选的类型（掩码写固定按整数解析）。
        """
        raw = self.write_var.get().strip()
        if not raw:
            self._notify("请先在「写入值」里填写数据", error=True)
            return None
        try:
            return parse_write_values(raw, fc, dtype or self._current_dtype())
        except ValueError as exc:
            self._notify(str(exc), error=True)
            return None

    def _read_once(self, note: str = "") -> None:
        """读一次，不改变轮询状态。

        界面上已经没有「读取」按钮 —— 数据由轮询持续刷新。这个原语仍要留着，
        两个用途：① 写入后的回读确认（不校验就等于「发出去了」而不是「写成了」）；
        ② F5 在轮询停止的情况下手动补读一次。
        note 非空时，读完后状态栏先显示它（写后回读用）。

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
        if self._validate_view_inputs(notify=True) is None:
            return
        try:
            address = self._address_field(fc)
            count = self._int_field(self.count_var, "数量", 1, limit)
        except (ValueError, AddressError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return

        request_id = self._begin_request(
            f"读取中…（超时 {self.timeout_var.get()} s，重试 {self.retry_var.get()} 次）")
        if request_id is None:
            return
        callback = lambda values, error: self._on_read_done(
            values, error, address, fc, note, request_id)
        self._async(lambda: client.read(fc, address, count),
                    self._request_callback(request_id, callback))

    def _begin_request(self, text: str) -> int | None:
        """开启唯一主站请求，返回请求编号；按钮和快捷键共用这道闸。"""
        if self._request_active:
            self.master_status.configure(text="上一请求尚未完成，请等待或先停止轮询",
                                         foreground=WARN)
            return None
        self._request_active = True
        self._request_seq += 1
        self._sync_request_controls()
        self.master_status.configure(text=text, foreground=MUTED)
        return self._request_seq

    def _sync_request_controls(self) -> None:
        active = self._request_active
        is_read = self._current_fc() in (1, 2, 3, 4)
        self.write_btn.configure(state="disabled" if is_read or active else "normal")
        self.poll_btn.configure(state="disabled" if active else "normal")
        self.fc_box.configure(state="disabled" if active else "readonly")
        self.connect_btn.configure(state="disabled" if active else "normal")

    def _end_request(self, request_id: int | None = None) -> None:
        if request_id is not None and request_id != self._request_seq:
            return
        self._request_active = False
        self._sync_request_controls()

    def _request_callback(self, request_id: int, callback):
        """丢弃已经过期或属于旧链路的回调，避免旧结果覆盖新状态。"""
        def done(result, error):
            if request_id != self._request_seq:
                return
            callback(result, error)
        return done

    def _on_read_done(self, values, error, address: int, fc: int, note: str = "",
                      request_id: int | None = None) -> None:
        self._end_request(request_id)
        if error:
            reason = self._friendly_error(error)
            self.master_status.configure(text=f"读取失败：{reason}", foreground=ERR)
            self._log(f"读取失败：{reason}", "err")
            self._mark_link(False, replied=self._replied(error))
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

    # -- 设备识别（FC2B）---------------------------------------------------

    def _identify_device_async(self) -> None:
        """主动读一次设备标识（右键菜单 / Ctrl+I）。

        与连上时那次静默探测分工不同：这里是用户明确要问，所以用正常超时
        与重试，失败也一定在状态栏说明原因 —— 点了没反应比报错更让人困惑。

        注意 client.read_device_id() 返回的是 {对象号: 值}，名字映射在这边补
        （core.read_device_id 才返回带 name 字段的结构）。
        """
        client = self._require_client()
        if client is None:
            return
        request_id = self._begin_request("正在读设备标识…")
        if request_id is None:
            return

        def done(result, error):
            self._end_request(request_id)
            if error:
                self._notify(f"设备识别失败：{self._friendly_error(error)}", error=True)
                self._mark_link(False, replied=self._replied(error))
                self._refresh_stats()
                return
            # 先落到胶囊再 mark_link（后者会重画胶囊），主动查到的名字同样该贴上
            self._device_note = self._device_summary(result)
            self._mark_link(True)
            self._refresh_stats()
            items = self._device_items(result)
            latency = client.last_latency_ms
            extra = f" · {latency:.1f} ms" if latency is not None else ""
            self.master_status.configure(text=f"设备标识：{items}{extra}", foreground=MUTED)
            self._log(f"设备识别（FC2B）：{items}{extra}", "ok")

        self._async(lambda: client.read_device_id(), done)

    # -- 类型与地址 --------------------------------------------------------

    def _current_fc(self) -> int:
        index = self.fc_box.current()
        return FC_OPTIONS[index if index >= 0 else 0][1]

    def _current_dtype(self) -> str:
        index = self.dtype_box.current()
        return DTYPE_OPTIONS[index if index >= 0 else 0][1]

    @staticmethod
    def _finite_number(raw: str, label: str, *, zero: bool) -> float:
        text = _normalize_number(raw).strip()
        try:
            value = float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{label}必须是数字（当前为「{text or '空'}」）") from None
        if not math.isfinite(value):
            raise ValueError(f"{label}必须是有限数，不能为 NaN 或无穷大")
        if not zero and value == 0:
            raise ValueError(f"{label}不能为 0，否则无法正确换算物理量")
        return value

    def _validate_view_inputs(self, *, notify: bool = False) -> tuple[float, float] | None:
        """读写前校验换算参数；输入过程只显示字段错误，不弹模态框。"""
        try:
            scale = self._finite_number(self.scale_var.get(), "系数", zero=False)
            bias = self._finite_number(self.bias_var.get(), "偏移", zero=True)
        except ValueError as exc:
            message = str(exc)
            label = getattr(self, "view_error", None)
            if label is not None:
                label.configure(text=message)
            if notify:
                self._notify(f"数据定义有误：{message}", error=True)
            return None
        label = getattr(self, "view_error", None)
        if label is not None:
            label.configure(text="")
        return scale, bias

    def _current_scale(self) -> float:
        """显示层容错值；任何实际读写都必须先过 _validate_view_inputs。"""
        try:
            return self._finite_number(self.scale_var.get(), "系数", zero=False)
        except ValueError:
            return 1.0

    def _current_bias(self) -> float:
        """显示层容错值；任何实际读写都必须先过 _validate_view_inputs。"""
        try:
            return self._finite_number(self.bias_var.get(), "偏移", zero=True)
        except ValueError:
            return 0.0

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

    def _address_field(self, fc: int) -> int:
        """按当前记法解析地址，最终统一检查协议地址范围。

        带 0x / 0b 前缀的字面量自带进制，也自带含义：它是**协议地址**，不是
        PLC 编号（PLC 编号按手册的写法是 40001 这种十进制）。所以前缀优先于
        下拉 —— 不管下拉停在哪一档，0x4000 读的都是 16384 号寄存器。不这样，
        同一串 0x4000 在十进制记法下是 16384、切到 PLC 记法却成了「PLC 编号
        16384」，报一句「属于离散输入」，而用户只看到「0x4000 不能用」。
        """
        raw = _normalize_number(self.addr_var.get()).strip()
        literal = raw.lower().startswith(("0x", "0b"))
        if literal and not self._addr_hex.get():
            # 框里写着 0x，就是用户当场声明「这是十六进制地址」——比下拉更具体。
            # 这里只同步记法与下拉文字，不动框里的字。放在解析函数里而不是只挂
            # KeyRelease，是因为读、写、预览、表头都从这条路走：粘贴、程序化赋值
            # 不产生松键事件，靠事件会漏，一漏就是「框里 0x4000、表头 416385」。
            self._addr_hex.set(True)
            self.plc_var.set(False)
            self._sync_addr_notation()
        try:
            if literal:
                value = _parse_int(raw)
            elif self._addr_hex.get() and not self.plc_var.get():
                # 十六进制记法下裸写的「1A」就是 0x1A：选了十六进制还要人再补
                # 一个 0x，等于把这个下拉白设。带 0x 的写法照样认。
                value = int(raw, 16)
            else:
                value = _parse_int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"起始地址格式无效：「{raw or '空'}」") from None
        if literal or not self.plc_var.get():
            if not 0 <= value <= 65535:
                raise ValueError("协议地址必须在 0 ~ 65535（或 0x0000 ~ 0xFFFF）之间")
        elif value < 1:
            raise ValueError("PLC 地址必须大于 0")
        protocol = value if literal else self._resolve_address(value, fc)
        if not 0 <= protocol <= 65535:
            raise ValueError("换算后的协议地址超出 0 ~ 65535")
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
            self._set_status_extra(label, "")
            return
        values, _ = self._last_read
        text = "".join(decode(values, "ascii", self.byteorder_box.get() or "ABCD"))
        text = text.rstrip(NONPRINT)
        self._set_status_extra(label, f'"{text}"' if text else "")

    def _set_status_extra(self, label, text: str) -> None:
        """状态栏里那两段可选提示（光标定位 / ASCII 预览）：有内容才露出来。

        空 Label 也占着几像素白底，和左边的连接状态并排看就像三道没有任何
        含义的白条 —— 用户确实问过「左下角这三个白条是干什么的」。
        没内容时收起，有内容时按固定顺序（状态 → 光标 → ASCII）补回去。
        """
        label.configure(text=text)
        if text:
            if not label.winfo_manager():
                if label is getattr(self, "cursor_label", None) and \
                        getattr(self, "ascii_label", None) is not None and \
                        self.ascii_label.winfo_manager():
                    label.pack(side="left", padx=(16, 0), before=self.ascii_label)
                else:
                    label.pack(side="left", padx=(16, 0))
        elif label.winfo_manager():
            label.pack_forget()

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

        格式（数值/十六进制/二进制）只有概览视图有语义；明细视图里这一对控件
        整组收起，但 `get()` 仍然返回上次选的十六进制/二进制 —— 直接读控件会让
        `_cell_step()` 以为「一格一个寄存器」，进而把就地写的地址算成
        `address + index * 1`（正确值是一格 span 个寄存器），
        写错地址的同时还按十六进制解析用户输入。所有需要格式的地方
        必须走这个函数。
        """
        return self.master_fmt_box.get() if self._master_grid else FMT_VALUE

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
        if fc in (1, 2) and len(values) > MAX_DETAIL_ROWS:
            # 状态栏照报总点数，表里只画前 MAX_DETAIL_ROWS 行 —— 让界面
            # 卡死的「功能」比少渲染更糟。这条提示只在点数变化时记一次：
            # 轮询每 500 ms 重画一遍，每次都记就是每秒两条把日志刷满。
            if self._detail_trunc != len(values):
                self._detail_trunc = len(values)
                self._log(f"明细视图只渲染前 {MAX_DETAIL_ROWS} 行（本次读到 {len(values)} 点），"
                          "大范围请用概览视图或缩小数量", "err")
            values = values[:MAX_DETAIL_ROWS]
        else:
            self._detail_trunc = None
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
            scale = self._current_scale()
            step = span(dtype)
            decoded = decode(values, dtype, self.byteorder_box.get() or "ABCD",
                             scale, self._current_bias())
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
        if fmt == FMT_HEX:
            # 带 0x 前缀：这是 C / Python / 汇编里通用的十六进制写法，
            # 一眼就能和十进制单元格区分开，复制出去也能直接被解析。
            self._emit_cells(tree, values, address, fc, per, 1, 82, "e",
                             lambda r: f"0x{r:04X}")
            return
        if fmt == FMT_BIN:
            # 分组后一格 19 个字符，每行 4 格仍不必横向滚动
            self._emit_cells(tree, values, address, fc, 4, 1, 150, "w",
                             self._bin_text)
            return

        # 值：按类型解码；概览里不放单位，纯数字更像仪表
        dtype = self._current_dtype()
        step = span(dtype)
        decoded = decode(values, dtype, self.byteorder_box.get() or "ABCD",
                         self._current_scale(), self._current_bias())
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
        self._diff_fill_grid(tree, per, cells, labels, structure_changed,
                             mark_changes=True)

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

    def _diff_fill_grid(self, tree, per: int, cells: list, labels: list,
                        structure_changed: bool = False,
                        mark_changes: bool = False) -> None:
        """主站/从站网格共用的落表算法：一维 cell 序列切成每行 per 格。

        行集合没变时只改变化的格子（`tree.set`）：轮询时整表 delete+insert
        会把选中行、滚动位置和列宽一起丢掉，值没变也是满屏重排。
        mark_changes=True（主站侧）时对比上一轮的值，变过的格子打 chg 标签
        并做高亮渐隐；从站网格没有「上一轮」可比，恒为 False。
        落表手法原来两边各抄一份 —— 只要有一边改动，两个网格的表现就会
        慢慢不一样，所以收敛到这里。
        """
        rows = (len(cells) + per - 1) // per
        changed = self._change_flags([str(c) for c in cells]) if mark_changes else []
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
                if not mark_changes:
                    continue
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
                if mark_changes and any(changed[r * per:(r + 1) * per]):
                    tags.append("chg")
                if r % 2:
                    tags.append("odd")
                tree.insert("", "end", iid=str(r), values=(labels[r], *chunk),
                            tags=tuple(tags))
            tree.yview_moveto(keep_y)
            tree.xview_moveto(keep_x)
        if mark_changes and any(changed):
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
            canvas.create_text(width / 2, height / 2, text="点选格子看趋势",
                               fill=MUTED, font=("Microsoft YaHei UI", 8))
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
        """表格里一格代表几个寄存器：十六进制/二进制/位区一格就是一个。"""
        fc = self._current_fc()
        if fc in (1, 2) or self._grid_fmt() in (FMT_HEX, FMT_BIN):
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
        self._set_status_extra(label, text or "")

    # -- 右键复制 ---------------------------------------------------------

    def _install_copy_menu(self, tree) -> None:
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="复制该格", command=lambda: self._copy_cell(tree))
        menu.add_command(label="复制整表（TSV）", command=lambda: self._copy_table(tree))
        # 设备标识是「偶尔要看一眼」的信息，常驻工具条太占地方，放右键刚好：
        # 不占位、入口可发现（对着表格右键是最自然的动作），而且这里失败会
        # 明确报错 —— 和连上时那次静默探测正好分工，一个不打扰，一个有回音。
        menu.add_separator()
        menu.add_command(label="读取设备标识", command=self._identify_device_async)
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
            if fmt == FMT_HEX:
                raw = _parse_int(text)          # 0x 前缀可有可无，parse_int 都认
                if not 0 <= raw <= 0xFFFF:
                    raise ValueError("超出 0 ~ 0xFFFF")
                payload, write_fc = [raw], 6
            elif fmt == FMT_BIN:
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
                    raise ValueError("这格不在当前轮询范围内，请先按 F5 补读一次再改")
                payload = [patch_ascii(text, original,
                                       self.byteorder_box.get() or "ABCD")]
                write_fc = 6
            else:
                dtype = self._current_dtype()
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
                                 self.byteorder_box.get() or "ABCD",
                                 self._current_scale(), self._current_bias())
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
                self._mark_link(False, replied=self._replied(error))
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
            self._read_once(f"已写入 地址 {address} = {text} · {counted} {noun}")

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
                self._mark_link(False, replied=self._replied(error))
                self._refresh_stats()
                return
            self._mark_link(True)
            self._notify(f"线圈 地址 {address} 已置 {value}，正在回读确认")
            self._refresh_stats()
            self._read_once()          # 整屏回读，比只读一个点有用

        self._async(lambda: client.write(5, address, [value]), done)


    # -- 视图切换（概览 ↔ 明细），tests 也走这两个入口 -------------------

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
        scale = self._current_scale()
        if scale != 1:
            parts.append(f"×{scale:g}")
        bias = self._current_bias()
        if bias:
            parts.append(("+" if bias > 0 else "-") + f"{abs(bias):g}")
        unit = self.unit_text_var.get().strip()
        if unit:
            parts.append(unit)
        if self.plc_var.get():
            parts.append("PLC 址")
        return " ".join(parts)

    def _sync_view_state(self) -> None:
        """把「数据定义」的当前取值存下来，弹层重建时恢复。

        单独一个方法：以前这段写在 `_view_summary()` 里，于是一个叫
        summary 的纯读取函数顺带改状态 —— 任何新增的「读摘要」调用点
        都会悄悄刷新快照，改起来没人敢动。
        """
        self._view_state = {
            "addr_hex": bool(self._addr_hex.get()),
            "dtype": self.dtype_box.get(),
            "byteorder": self.byteorder_box.get(),
            "scale": self.scale_var.get(),
            "bias": self.bias_var.get(),
            "unit": self.unit_text_var.get(),
            "plc": self.plc_var.get(),
        }

    def _update_empty_hint(self) -> None:
        """数据区空着的时候说清楚在等什么。"""
        try:
            tree = self.result_grid if self._master_grid else self.result_tree
            rows = len(tree.get_children())
        except (AttributeError, tk.TclError):
            return
        if rows:
            self.empty_hint.place_forget()
            return
        if self.client is None:
            title = "尚未连接"
            # 主键是「直接连」，改参数才走右边那一格。这里不用 ▾ 这个字形
            # 指路：9pt 的中文字体里它只画出一个小点，看着像标点错误。
            desc = "点右上角「连接」直接连；要改 IP / 串口点它右边的小箭头"
        else:
            title = "等待数据"
            desc = "轮询进行中；也可以按 F5 手动读取一次"
        if self.empty_title.cget("text") != title:
            self.empty_title.configure(text=title)
        self.empty_desc.configure(text=desc)
        self.empty_hint.place(relx=0.5, rely=0.42, anchor="center")

    def _refresh_stats(self) -> None:
        self._update_empty_hint()
        summary = self._view_summary()
        client = self.client
        if client is None:
            self.stats_label.configure(text=summary)
            return
        # request_count 只记「收到应答」的次数，error_count 记失败尝试（含重试），
        # 所以失败数可能比成功数大 —— 那说明链路在抖，正是要看见的信息。
        # 耗时不在这一侧重复：左侧过程描述里已经报了这次请求的往返时间。
        parts = [summary, f"成功 {client.request_count}", f"失败 {client.error_count}"]
        self.stats_label.configure(text=" · ".join(parts))

    def _toggle_master_view(self) -> None:
        self.set_master_view(not self._master_grid)

    def _sync_master_view_switch(self) -> None:
        self._sync_view_button(self.master_view_btn, self._master_grid)

    def set_master_view(self, grid: bool) -> None:
        self._master_grid = grid
        self._prev_cells = None
        # 光标提示说的是「刚才那个视图里那一格」，切过去就过期了：
        # 概览切明细后，状态栏还挂着「0x0007 = 0x0A0B」，可那个格子已经不在了
        self._set_cursor_hint(None)
        if grid:
            self.detail_wrap.grid_remove()
            self.grid_wrap.grid(row=0, column=0, sticky="nsew")
            # 按原位置把「格式」这一对插回：标签在左、下拉在右、切换按钮在最右
            self.master_fmt_box.pack(side="left", padx=(0, 10), before=self.master_view_btn)
            self.fmt_label.pack(side="left", padx=(0, 4), before=self.master_fmt_box)
            self.master_fmt_box.configure(state="readonly")
        else:
            self.grid_wrap.grid_remove()
            self.detail_wrap.grid(row=0, column=0, sticky="nsew")
            # 明细视图本身就是「数值 / 十六进制 / 二进制三列并列」，格式下拉
            # 在这里没有事可做。以前是置灰：一个灰控件常驻最右边，既占宽度
            # 又要用户去猜「它为什么是灰的」；整组收起，原因就不言自明。
            # 收起的只是控件，用户选的值留着：回到概览还是他原来选的十六进制。
            # 这里以前顺手 set(FMT_VALUE)，于是「切明细看一眼再回来」就把
            # 选好的格式悄悄改回数值 —— 而 _grid_fmt() 本来就只在概览视图
            # 认这个控件，不需要靠重置来防它带偏。
            self.master_fmt_box.pack_forget()
            self.fmt_label.pack_forget()
        self._sync_master_view_switch()
        self._render_master_data()

    def _build_slave_view_actions(self, head) -> None:
        """「格式」下拉：同一个 16 位值怎么显示、就地编辑时按什么解析。

        以前叫「进制」—— 但「有符号」根本不是进制（进制只有十/十六/二），
        它改的是符号解释；而且「十进制」和「有符号」都是十进制显示，
        真正的区别是无符号/有符号。按主站页的叫法统一成「格式」，
        选项改成「无符号」让两者的对立一目了然。
        """
        # 与主站页同一套切换构件：卡片里从右往左依次是「格式」标签、下拉、
        # 视图切换按钮；都按 side="left" 排，顺序即视觉顺序
        box = ttk.Frame(head, style="Inner.TFrame")
        box.pack(side="right")
        ttk.Label(box, text="格式", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.slave_fmt_box = ttk.Combobox(box, values=("无符号", "有符号", FMT_HEX, FMT_BIN),
                                          state="readonly", width=8)
        self.slave_fmt_box.set("无符号")
        self.slave_fmt_box.pack(side="left", padx=(0, 10))
        self.slave_view_btn = self._build_view_button(box, self._toggle_slave_view)
        self._sync_slave_view_switch()
        self.slave_fmt_box.bind("<<ComboboxSelected>>",
                                lambda e: self._refresh_slave_table())

    def _toggle_slave_view(self) -> None:
        self.set_slave_view(not self._slave_grid)

    def _sync_slave_view_switch(self) -> None:
        self._sync_view_button(self.slave_view_btn, self._slave_grid)

    def set_slave_view(self, grid: bool) -> None:
        self._slave_grid = grid
        if grid:
            self.slave_detail_wrap.grid_remove()
            self.slave_grid_wrap.grid(row=0, column=0, sticky="nsew")
        else:
            self.slave_grid_wrap.grid_remove()
            self.slave_detail_wrap.grid(row=0, column=0, sticky="nsew")
        self._sync_slave_view_switch()
        self._slave_sig = None            # 强制按新视图/格式重建
        self._refresh_slave_table()

    def _do_write_async(self) -> None:
        client = self._require_client()
        if client is None:
            return
        fc = self._current_fc()
        if fc not in (5, 6, 15, 16, 22):
            self._notify("当前功能码是读操作，写入请先切换到写功能码", error=True)
            return
        if fc == 22:
            # 掩码写的两个值是位掩码，固定按整数解析（支持 0x），
            # 不跟着「数据定义」弹层里选的 float 类型漂
            values = self._parse_write_values(22, dtype="uint16")
            if values is None:
                return
            if len(values) != 2:
                self._notify("掩码写需要 AND 和 OR 两个掩码，逗号分隔（如 0xFFF0,0x000F）",
                             error=True)
                return
            payload = [int(v) & 0xFFFF for v in values]
        else:
            values = self._parse_write_values(fc)
            if values is None:
                return
            if fc in (5, 6) and len(values) != 1:
                self._notify("单点写入只能填一个值", error=True)
                return
            if fc in (5, 15):
                payload = values
            else:
                # 用户填的是物理量（比如 220.5），这里负责编码成寄存器序列
                try:
                    payload = encode(values, self._current_dtype(),
                                     self.byteorder_box.get() or "ABCD",
                                     self._current_scale(), self._current_bias())
                except ValueError as exc:
                    self._notify(str(exc), error=True)
                    return
                if fc == 6 and len(payload) != 1:
                    self._notify(f"{self._current_dtype()} 要占 {len(payload)} 个寄存器，"
                                 f"单点写放不下，请改用「写多寄存器」", error=True)
                    return
        try:
            address = self._address_field(fc)
        except (ValueError, AddressError) as exc:
            self._notify(f"参数有误：{exc}", error=True)
            return

        def done(result, error):
            self._end_request()
            if error:
                self._notify(f"写入失败：{self._friendly_error(error)}", error=True)
                self._mark_link(False, replied=self._replied(error))
                self._refresh_stats()
                return
            self._mark_link(True)
            counted, noun, bytes_ = self._write_extent(fc, len(payload))
            # 「写的是哪一个」要明确到地址：单寄存器照旧，多寄存器给范围
            # （状态栏）和逐地址映射（日志），记法跟表格显示保持一致
            base = self._display_address(address, fc)
            if counted == 1:
                addr_text = f"地址 {base}"
            else:
                addr_text = f"地址 {base}~{base + counted - 1}"
            self.master_status.configure(
                text=(f"已写入 FC{fc:02d} {addr_text} · "
                      f"{counted} {noun}（{bytes_} 字节）"), foreground=OK)
            if fc == 22:
                head = addr_text
            else:
                sp = max(1, len(payload) // max(1, len(values)))
                head = self._register_map(base, values, sp)
            self._log(f"写入成功：{head} ← {self._write_brief(values)} "
                      f"→ {counted} {noun} {self._hex_list(payload)}", "ok")
            self._refresh_stats()
            self._mark_written(address, 1 if fc == 22 else len(payload))
            # 回读完成的提示会用 note 覆盖状态栏 —— 地址范围必须带进去，
            # 不然「写的是哪一个」又在最后一行丢了
            self._readback(fc, address, 1 if fc == 22 else len(payload),
                           f"已写入 FC{fc:02d} {addr_text} · "
                           f"{counted} {noun}（{bytes_} 字节）")

        counted, noun, _ = self._write_extent(fc, len(payload))
        self._begin_request(f"写入中… {counted} {noun}")
        self._async(lambda: client.write(fc, address, payload), done)

    @staticmethod
    def _write_extent(fc: int, payload_len: int) -> tuple[int, str, int]:
        """这次写到底占多少：(数量, 量词, 字节数)。

        线圈按位算字节（8 个一字节），寄存器按 2 字节算 —— 这是 Modbus 帧里
        「字节数」字段的定义，界面上就该用同一套口径说话。
        掩码写虽然发 AND/OR 两个掩码，落盘的只有 1 个寄存器。
        """
        if fc == 22:
            return 1, "个寄存器", 2
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
        extra = f" {kind}"
        if span(dtype) > 1:
            extra += f" {self.byteorder_box.get() or 'ABCD'}"
        scale = self._current_scale()
        if scale != 1:
            extra += f" ×{scale:g}"
        bias = self._current_bias()
        if bias:
            extra += (" +" if bias > 0 else " -") + f"{abs(bias):g}"
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
                self._mark_link(False, replied=self._replied(error))
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
            self._stop_poll(manual=True)
            return
        self._start_poll()

    def _start_poll(self, auto: bool = False) -> bool:
        """开轮询。auto=True 表示连接成功后自动触发，只在日志上区分来源。

        手动和自动走同一条路：自动触发的参数不合法时不能静默什么都不做，
        否则用户看到的就是「连上了但没在轮询」，还以为功能坏了。
        """
        client = self._require_client()
        if client is None:
            return False
        fc = self._current_fc()
        if fc not in (1, 2, 3, 4):
            if auto:
                # 自动触发不弹错误打扰刚连上的那一下，但要在日志里说清为什么没动
                self._log(f"当前功能码 FC{fc:02d} 是写操作，已跳过自动轮询")
            else:
                self._notify("轮询只能用于读操作", error=True)
            return False
        self._polling = True
        self._poll_failures = 0
        self.poll_btn.configure(text="停止轮询")
        if auto:
            self._log(f"已连接，自动开始轮询（间隔 {self.interval_var.get().strip()} ms）")
        else:
            self._log("开始轮询")
        self._poll_tick()
        return True

    def _probe_device_then_poll(self) -> None:
        """连上先静默问一句「对面是谁」，再进入轮询。

        FC 0x2B 是协议里的可选项，廉价变频器/温控表多半回异常码甚至不应答，
        所以这一帧压到短超时、不重试、失败完全静默：成功就白捡一个型号写进
        链路胶囊，失败则当作没问过 —— 用户不该因为设备不支持一个可选功能，
        就怀疑自己的连接出了问题。

        排在轮询之前是有意的：轮询一开就长期占住请求闸门，这一帧再想插进去
        只能干等；而它本身就是「认识对面」的第一步，先认识再看数据，也正好
        是现场联调的顺序。
        """
        client = self.client
        if client is None or self._request_active:
            self._autostart_poll()
            return
        request_id = self._begin_request("正在确认设备…")
        if request_id is None:
            self._autostart_poll()
            return

        def done(result, error):
            self._end_request(request_id)
            if not error:
                self._apply_device_id(result)
            self._autostart_poll()

        self._async(lambda: client.probe_device_id(),
                    self._request_callback(request_id, done))

    def _apply_device_id(self, result: dict) -> None:
        """把 FC2B 的对象表落到界面：胶囊显示短名，日志记全表。"""
        items = self._device_items(result)
        if not items:
            return
        # 胶囊只有一行的位置，塞全表会把它撑成两行；型号名够认出对面是谁了，
        # 厂商、版本的完整信息留给日志
        self._device_note = self._device_summary(result)
        self._render_link()
        self._log(f"设备标识（FC2B）：{items}", "ok")

    @staticmethod
    def _device_items(result: dict) -> str:
        """「厂商名=xx · 产品代码=yy」这样一行，日志与状态栏共用。"""
        return " · ".join(
            f"{DEVICE_ID_OBJECTS.get(oid, f'对象 {oid}')}={value}"
            for oid, value in sorted(result.items()))

    @staticmethod
    def _device_summary(result: dict) -> str:
        """从设备标识里挑一个够短、够认得出的名字给链路胶囊。

        优先型号名（0x05）与产品名称（0x04）—— 同一家厂商的一排设备，
        认出「ModNova」远不如认出「ND-100」有用。两者都没有时退回
        「厂商 + 产品代码」：只报一个 "SIM" 或只报一个厂商名，都没法
        让人确认自己连的是哪台。
        """
        def get(oid: int) -> str:
            return str(result.get(oid) or "").strip()

        model = get(0x05) or get(0x04)
        if model:
            return model
        vendor, code = get(0x00), get(0x01)
        if vendor and code:
            return f"{vendor} {code}"
        return vendor or code

    def _autostart_poll(self) -> None:
        """连接成功后自动开轮询（不做开关，连上即进入轮询态）。

        挂在连接成功回调的最后：先让「已连接 / 自环联调 / 站地址不一致」
        这些一次性提示落地，再开轮询 —— 否则轮询的读数会立刻把状态栏
        占住，刚连上时最该看的那句话反而一闪而过。
        想停就在工具条按「停止轮询」，想手动补一次数据按 F5。
        """
        if self.client is None or self._polling:
            return
        if self._request_active:
            # 连上瞬间还有别的请求在跑（例如刚点过「写入」）：等它结束再来，
            # 不能抢同一个请求闸门，否则 _begin_request 会把这次轮询吞掉
            self.root.after(120, self._autostart_poll)
            return
        self._start_poll(auto=True)

    def _stop_poll(self, manual: bool = False) -> None:
        """停轮询。manual=True 表示这是用户按的（按钮 / Esc）。

        区分这两种来源是因为「切到写功能码导致的停」和「用户不想轮询了」
        是两回事：前者切回读码要能自己接上，后者必须一直停着 —— 用户明确
        说了不要，再自动重启就是在跟人抢方向盘。
        """
        if manual:
            self._poll_resume_on_read = False
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
            address = self._address_field(fc)
            # 上限按功能码走：FC03 是 125，FC01 才是 2000。
            # 原来一律按 2000 放行，填 200 个寄存器开始轮询后每 500ms
            # 报一次同样的「数量必须在 1~125 之间」，还得手动按 Esc 才停。
            count = self._int_field(self.count_var, "数量", 1, read_limit(fc) or 125)
            interval = max(50, self._int_field(self.interval_var, "轮询间隔", 50, 60000))
        except (ValueError, AddressError) as exc:
            self._stop_poll()
            # 静默停会让人以为轮询自己坏了 —— 参数哪里不对，说清楚
            self.master_status.configure(text=f"轮询已停止：{exc}", foreground=ERR)
            return

        # 失败提示要带「读的是哪一段」。只说「地址或数量超出从站范围」，用户
        # 还得自己回想地址框里那串是 16384 还是 4000 —— 而 0x4000 恰好等于
        # 16384，这两个数最容易混。地址按当前记法显示，与框里、表头对得上。
        target = f"地址 {self._addr_text(address, fc)} 起 {count} 点"

        def done(values, error):
            if error:
                self._poll_failures += 1
                reason = self._friendly_error(error)
                # 从站回的异常码单列出来：那是「设备说不行」，和本机校验失败
                # 的排查方向完全不同；不写码，就只剩一句含糊的「失败」
                code = getattr(error, "code", None)
                why = f"从站异常 {code:02X} · {reason}" if code is not None else reason
                self._mark_link(False, replied=self._replied(error))
                if self._poll_failures >= 5:
                    # 连续 5 次同样的失败（多为参数错误）再轮下去只是刷屏，
                    # 停掉并把原因留在状态栏，比让用户自己按 Esc 更明确。
                    self._stop_poll()
                    self.master_status.configure(
                        text=f"轮询已自动停止（连续 {self._poll_failures} 次失败）："
                             f"{target} · {why}", foreground=ERR)
                    return
                self.master_status.configure(
                    text=f"轮询失败 {self._poll_failures}/5 · {target} · {why}",
                    foreground=ERR)
            else:
                self._poll_failures = 0
                self._mark_link(True)
                if not self._editing:      # 正在就地编辑时别重画，否则输入框会被顶走
                    self._render_values(values, address)
                latency = self.client.last_latency_ms if self.client else None
                extra = f" · {latency:.1f} ms" if latency is not None else ""
                shown = f"地址 {self._addr_text(address, fc)} 起 {len(values)} 点"
                self.master_status.configure(
                    text=f"轮询中 · {shown}{extra}", foreground=MUTED)
            self._refresh_stats()
            if self._polling:
                self.root.after(interval, self._poll_tick)

        self._async(lambda: self.client.read(fc, address, count), done)

    # ------------------------------------------------------------ 从站逻辑

    def _set_slave_param_state(self, running: bool) -> None:
        """从站运行中锁住启动参数（下拉转 disabled，其余转 normal/readonly）。

        模式/端口/站地址/数据区长度只在启动那一刻被读取，运行中改了也不
        生效 —— 与其让人改完以为生效了，不如直接锁住。演示数据开关和
        故障注入是运行时特性，不在锁的范围内。
        """
        for box in (self.slave_mode_box, self.slave_baud, self.slave_databits,
                    self.slave_parity, self.slave_stopbits):
            box.configure(state="disabled" if running else "readonly")
        for widget in (self.slave_host, self.slave_port, self.slave_serial,
                       self.slave_unit, self.slave_size):
            widget.configure(state="disabled" if running else "normal")

    def _toggle_server(self) -> None:
        if self.server is not None and self.server.running:
            # 停止同样是异步的：完成前锁住按钮和 _busy。不然旧 socket 还没
            # 关完就按同一端口再启动，会撞出误导性的「端口被占——可能另一个
            # 实例占着」，而占着的是你自己刚点停止的那个
            self._busy = True
            server, self.server = self.server, None
            self.slave_btn.configure(state="disabled")
            self.slave_stat.configure(text="正在停止…")
            self.tabs.set_badge(1, False)
            self._sync_crc_field()

            def stopped(_result, _error):
                self._busy = False
                self._set_slave_param_state(False)
                self.slave_btn.configure(state="normal", text="启动从站",
                                         style="Accent.TButton")
                self.slave_stat.configure(text="已停止", foreground=MUTED)
                # 「从站已停止」由 server.stop 自己记（TCP/RTU 文案各自贴切），
                # 这里不重复；只有停止本身出错才需要补一条
                if _error is not None:
                    self._log(f"停止从站时出错：{_error}", "err")

            self._async(server.stop, stopped)
            return

        if self._busy:
            return
        try:
            unit = self._int_field(self.slave_unit, "从站站号", 1, 247)
            size = self._int_field(self.slave_size, "数据区长度", 1, 65536)
            store = DataStore(size=size)
            if self.slave_mode.get() in ("tcp", "rtutcp"):
                # 清空输入框时只听本机，别把寄存器区静默暴露到网段
                host = self.slave_host.get().strip() or "127.0.0.1"
                blocked = self._probe_listen_host(host)
                if blocked:
                    # 点击瞬间就拦下并说清「本机有哪些地址可填」，
                    # 不让用户等一轮异步失败后对着一行小字猜
                    raise ValueError(blocked)
                server_cls = TcpServer if self.slave_mode.get() == "tcp" else RtuOverTcpServer
                server = server_cls(
                    host=host,
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
            # 启动失败用错误色强调：参数卡里一行灰字容易被当成「点了没反应」
            self.slave_stat.configure(text=f"启动失败：{exc}", foreground=ERR)
            self._log(f"无法启动从站：{exc}", "err")
            return

        self._busy = True
        self.slave_btn.configure(state="disabled", text="启动中…")

        def done(result, error):
            self._busy = False
            self.slave_btn.configure(state="normal")
            if error:
                reason = self._explain_server_error(error)
                self.slave_btn.configure(text="启动从站")
                self.slave_stat.configure(text=f"启动失败：{reason}", foreground=ERR)
                self._log(f"从站启动失败：{reason}", "err")
                return
            self.server = server
            self.slave_btn.configure(text="停止从站", style="Danger.TButton")
            self._set_slave_param_state(True)   # 启动参数锁住：改了不生效就别让改
            self.slave_stat.configure(text=f"运行中 · 端口 {server.port} · 站 {unit}",
                                      foreground=MUTED)
            self._log(f"从站已启动（{server.port}）", "ok")
            self.tabs.set_badge(1, True)      # 在「从站」上点一颗绿灯，主站页也看得见它在跑
            self._clear_faults()              # 新从站的故障注入从零开始，面板跟着归零
            self._sync_crc_field()            # CRC 字段按实际 server 类型表态
            self._refresh_slave_table()

        self._async(server.start, done)

    @staticmethod
    def _explain_server_error(error: object) -> str:
        """把从站启动失败的系统原文翻译成现场人话。

        典型场景：本机还挂着另一个本工具实例（或别的程序）占着同一端口，
        WinError 10048 的原文一大串，现场只想要「端口被占了，换一个或停掉它」。
        """
        text = str(error)
        if "10048" in text or "Only one usage" in text or "只允许使用一次" in text:
            return ("监听端口已被占用 —— 可能另一个本工具实例的从站或别的程序"
                    "正占着它；换个端口，或先停掉占用者")
        if "10049" in text:
            return "监听地址不可用 —— 本机上没有这个地址，检查「监听地址」的填法"
        return text

    @staticmethod
    def _local_ips() -> list[str]:
        """给报错文案列本机可用地址；枚举不到就只剩 127.0.0.1。"""
        ips = {"127.0.0.1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        return sorted(ips)

    @staticmethod
    def _probe_listen_host(host: str) -> str | None:
        """启动前同步探测监听地址是否可绑定；不可用就返回一句能照做的话。

        背景：用户把现场设备的 IP（如 192.168.13.10）填进监听地址，以前
        要等一轮异步 bind 失败，才在状态栏看到一行小字 —— 看起来就像
        「点了没反应」。这里在点击瞬间用「绑 0 号端口」探测（不占端口、
        不碰真实端口，端口冲突仍由异步启动按 10048 报告）。

        只对 IP 字面量探测：主机名要走 DNS，可能阻塞 UI 线程数秒，
        那种情况维持原异步路径，失败原文由 _explain_server_error 翻译。
        """
        try:
            version = ipaddress.ip_address(host).version
        except ValueError:
            return None
        family = socket.AF_INET6 if version == 6 else socket.AF_INET
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            return None
        try:
            sock.bind((host, 0))
            return None
        except OSError:
            ips = ModbusApp._local_ips()
            return (f"监听地址 {host} 不是本机的 IP —— 本机可用："
                    f"{'、'.join(ips)}；要给局域网访问请填 0.0.0.0")
        finally:
            sock.close()

    # -- 故障注入 ----------------------------------------------------------

    _FAULT_FIELDS = ("fault_drop", "fault_delay", "fault_exc",
                     "fault_exc_code", "fault_trunc", "fault_crc")

    def _crc_capable(self) -> bool:
        """坏 CRC 只对帧尾带 CRC 的从站形态有意义：串口 RTU、RTU over TCP 透传。

        Modbus TCP 帧没有 CRC 校验域，注入了也无处安放。运行中以实际
        server 类型为准（运行中切模式下拉不改已启动的从站）；未启动时
        按当前模式预示将要启动的形态。
        """
        if self.server is not None:
            # RtuOverTcpServer 继承 TcpServer 但帧尾带 CRC，不能 isinstance 一刀切
            return type(self.server) is not TcpServer
        return self.slave_mode.get() in ("rtutcp", "rtu")

    def _sync_crc_field(self) -> None:
        """坏CRC 字段的可用性 = 帧里到底有没有 CRC。

        替代原来那行常驻说明文字：TCP 模式字段禁用、标题变灰（不可用）；
        RTU 系模式恢复正常、标题回到正文色。模式切换、从站启停都会走到这里。
        """
        capable = self._crc_capable()
        self.fault_crc.state(["!disabled"] if capable else ["disabled"])
        self._fault_labels["坏CRC %"].configure(
            foreground=TEXT if capable else MUTED)

    def _fault_params(self) -> dict | None:
        """把面板上的输入换成 FaultConfig 字典；出错返回 None 并提示。"""
        try:
            code = int(self.fault_exc_code.get())
            params = {
                "drop_rate": float(self.fault_drop.get()) / 100,
                "delay": max(0.0, float(self.fault_delay.get())) / 1000,
                "exception_rate": float(self.fault_exc.get()) / 100,
                "exception_code": code,
                "truncate_rate": float(self.fault_trunc.get()) / 100,
                "bad_crc_rate": float(self.fault_crc.get()) / 100,
            }
        except ValueError:
            self.fault_stat.configure(text="故障注入参数必须是数字", foreground=ERR)
            return None
        if not 0 <= code <= 255:
            self.fault_stat.configure(text="异常码必须在 0~255 之间", foreground=ERR)
            return None
        # 字段在 TCP 模式已禁用变灰，但 RTU 模式填的值还留在框里 —— 兜一层，
        # 别让残留值被 Apply 进一个帧里根本没有 CRC 的从站
        if not self._crc_capable():
            params["bad_crc_rate"] = 0.0
        return params

    def _apply_faults(self) -> None:
        if self.server is None:
            self.fault_stat.configure(text="从站未启动，先启动从站", foreground=ERR)
            return
        params = self._fault_params()
        if params is None:
            return
        self.server.core.faults = FaultConfig.from_dict(params)
        self._render_fault_stat()

    def _clear_faults(self) -> None:
        if self.server is not None:
            self.server.core.faults = FaultConfig()
        for name in self._FAULT_FIELDS:
            getattr(self, name).set("0")
        self._render_fault_stat()

    def _render_fault_stat(self) -> None:
        if self.server is None:
            self.fault_stat.configure(text="未注入", foreground=MUTED)
            return
        faults = self.server.core.faults
        if not faults.active:
            self.fault_stat.configure(text="未注入", foreground=MUTED)
            return
        parts = []
        if faults.drop_rate:
            parts.append(f"不响应 {faults.drop_rate * 100:g}%")
        if faults.delay:
            parts.append(f"延迟 {faults.delay * 1000:g}ms")
        if faults.exception_rate:
            parts.append(f"异常 {faults.exception_rate * 100:g}%/码0x{faults.exception_code:02X}")
        if faults.truncate_rate:
            parts.append(f"截断 {faults.truncate_rate * 100:g}%")
        if faults.bad_crc_rate:
            parts.append(f"坏CRC {faults.bad_crc_rate * 100:g}%")
        self.fault_stat.configure(text="注入中：" + " · ".join(parts), foreground=WARN)
        self._log("故障注入已更新：" + " · ".join(parts), "ok")

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
        is_bit = area in BIT_AREAS
        # 格式只在概览视图有意义；位区 ●/○ 已经是最佳显示，也不参与
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
        elif fmt == FMT_HEX:
            per, width, anchor = 10, 82, "e"
            # 与主站一致带 0x 前缀；就地编辑时 parse_int 也认这个前缀
            cells = [hex_of([v]) for v in values]
        elif fmt == FMT_BIN:
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
        self._diff_fill_grid(tree, per, cells,
                             [str(base + r * per) for r in range(rows)])

    def _tick_slave_refresh(self) -> None:
        # 没起从站就什么都不用采；页签不在前台也不必刷新。
        if self.server is None:
            self.root.after(800, self._tick_slave_refresh)
            return
        if not self.server.running:
            # server 侧线程循环结束时会自行清掉 _running（server.py 的 finally），
            # 但没人点「停止」—— 界面若还挂着「运行中」和绿灯，用户会以为
            # 它仍在应答。这里当看门狗：发现灯灭了就按手动停止的口径复位。
            self._on_server_died()
            self.root.after(800, self._tick_slave_refresh)
            return
        if self.slave_grid.winfo_ismapped() or self.slave_tree.winfo_ismapped():
            try:
                self._refresh_slave_table()
            except Exception as exc:
                # 静默吞掉会把真实缺陷变成「从站页永远不刷新」且无人察觉，
                # 与 applog「崩溃必须留痕」的项目纪律相悖 —— 至少留一行日志。
                self._log(f"从站数据刷新失败：{exc}", "err")
        self.root.after(800, self._tick_slave_refresh)

    def _on_server_died(self) -> None:
        """从站没被点停止、循环自己结束了：监听挂掉、串口被拔、线程异常退出。

        按手动停止（_toggle_server 停止分支）的同一套口径复位界面，
        状态栏用错误色并写明「异常退出」，跟主动停止区分开。
        """
        server, self.server = self.server, None
        self._set_slave_param_state(False)
        self.slave_btn.configure(state="normal", text="启动从站",
                                 style="Accent.TButton")
        self.slave_stat.configure(text="已停止（异常退出）", foreground=ERR)
        self.tabs.set_badge(1, False)
        self._sync_crc_field()
        self._log(f"从站异常退出（{type(server).__name__} 循环已结束），界面已复位",
                  "err")

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
            except Exception as exc:
                # 演示数据写不进去属于意外缺陷，静默循环吞掉的话从站页会
                # 停在旧值上，看起来像「模拟器坏了」却查不到原因
                self._log(f"演示数据写入失败：{exc}", "err")
        self.root.after(250, self._tick_demo)

    def _slave_hover(self, event) -> None:
        """悬停在可编辑格子上光标变手型。

        「双击数值可直接改」这句话撤掉之后，可编辑性改由两个状态信号表达：
        手型光标（悬停即知）+ 编辑反馈行（改完即现）。可编辑范围与双击
        处理一致：明细只有「值」列，概览除行头地址列外全是数据格。
        """
        tree = event.widget
        if tree.identify_region(event.x, event.y) != "cell":
            tree.configure(cursor="")
            return
        column = tree.identify_column(event.x)
        editable = (column == "#2") if tree is self.slave_tree else (column != "#1")
        tree.configure(cursor="hand2" if editable else "")

    def _slave_hover_leave(self, event) -> None:
        event.widget.configure(cursor="")

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
        """概览视图的双击编辑：列偏移 + 行头起始地址 = 寄存器地址。"""
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
        if area in BIT_AREAS:
            initial = "1" if tree.set(row_id, f"c{col}") == "●" else "0"
            fmt = "dec"
        else:
            initial = tree.set(row_id, f"c{col}")
            shown = self.slave_fmt_box.get()
            fmt = "dec" if shown in ("无符号", "有符号") else shown
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
            # 只提交「触发事件的这个编辑器」的内容。以前读 self._editor——
            # 正常操作下两者总是一个人（回车/失焦时编辑器立刻销毁），但
            # 失焦事件是异步投递的，若销毁前新编辑器已经顶上，旧编辑器的
            # 失焦提交就会把新编辑器里的内容写到旧地址上（合成事件下实测复现）。
            if self._editor is not editor:
                return
            text = editor.get().strip()
            editor.destroy()
            self._editor = None
            self._editing = False
            if not text:
                return
            if on_commit is not None:
                on_commit(index, text, fmt)
            else:
                self._apply_cell_edit(index, text, fmt)

        editor.bind("<Return>", commit)

        def escape(_event=None):
            self._cancel_edit()
            # return "break"：编辑器的 Esc 只取消编辑，别把事件再传给全局
            # <Escape> 绑定 —— 那里挂着「停止轮询」，按一下 Esc 做两件事太惊悚
            return "break"

        editor.bind("<Escape>", escape)
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
        if self.server is None:
            # 编辑器开着的时候从站被停掉，回车提交会走到这里 —— 直接解引用
            # self.server.store 会抛 AttributeError，被 Tk 回调钩子当成
            # 「程序内部错误」报给用户。如实告知即可。
            self.slave_edit_hint.configure(text="从站已停止，修改未写入", foreground=ERR)
            return
        area = self._current_area()
        store = self.server.store
        try:
            if area in BIT_AREAS:
                value = text.lower() in ("1", "on", "true", "yes", "●")
            elif fmt == FMT_HEX:
                value = int(text, 16)          # int(..., 16) 自带 0x 前缀容错
            elif fmt == FMT_BIN:
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
        # 一键抹掉的不只是几个数 —— 联调时手工构造的整片模拟数据都在这块
        # 区里，误点一下全没了且无法撤销。清空前问一句。
        if not messagebox.askokcancel(
                "清空从站数据",
                "四个数据区将全部归零，手工填的模拟数据无法恢复。",
                parent=self.root):
            self._log("已取消清零")
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

    # 文件日志默认关闭；崩溃堆栈仍会经 report_callback_exception 提示到界面，
    # get_logger 每次现取 —— 将来若恢复落盘开关，之后的崩溃也能直接落进文件
    root = tk.Tk()
    app = ModbusApp(root)
    install_excepthook(root=root, get_logger=lambda: app.logger,
                       on_crash=lambda message, _detail:
                       app._notify(f"{message}，详情已打印到控制台",
                                   error=True))
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
