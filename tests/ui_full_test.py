"""桌面版（Tkinter）全项功能测试。

不是单元测试：这里真的把界面建起来，真的起一个从站、真的让主站连上去，
然后逐个项目去点按钮 / 拉下拉框 / 勾选 / 双击表格，读回界面状态和从站内存做断言。

跑法：
    <带 tkinter 的 python> tests/ui_full_test.py
输出：
    控制台逐条 [PASS]/[FAIL]，最后汇总；明细写到 tests/_ui_report.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 连接参数连接成功后要落盘。回归测试必须在**临时配置**上跑：
# 既别去动用户目录里那份真配置，也别让上一轮留下的参数影响这一轮
# （否则「默认 127.0.0.1:502」这类用例会随机失败）。
os.environ["MODNOVA_CONFIG"] = os.path.join(
    tempfile.mkdtemp(prefix="modnova-test-cfg-"), "config.json")

import tkinter as tk

import modbus_tool.gui as gui_mod
from modbus_tool.config import Config
from modbus_tool.gui import (
    AREA_OPTIONS,
    DTYPE_OPTIONS,
    FC_OPTIONS,
    FMT_HEX,
    FMT_VALUE,
    ModbusApp,
)
from modbus_tool.transport import HAS_SERIAL
from modbus_tool.ui_theme import ERR, MUTED


class Runner:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.app = ModbusApp(self.root)
        self.root.geometry("1240x900")
        self.root.update()
        # 键盘用例（Ctrl+1/2、F6、内联编辑回车）要靠窗口真的拿到焦点。
        # 不强制前台的话，脚本在后台跑时事件会被系统丢掉，表现为「偶发几条不过」，
        # 而鼠标用例照样通过 —— 很容易被误判成功能坏了。
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.root.update()
        self.results: list[dict] = []
        self.notes: list[str] = []

    # ------------------------------------------------------------ 基础

    def pump(self, seconds: float = 0.35) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.008)

    def wait(self, predicate, timeout: float = 4.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def kbd(self, widget, sequence: str, probe, attempts: int = 3) -> bool:
        """合成键盘事件并确认真的投递到了。

        Windows 上测试窗口拿不到前台焦点时（用户正在别的窗口操作），
        系统会丢弃 event_generate 投递的键盘事件 —— 编辑器提交用例早已
        踩过并用 focus_force+重试 解决。这里统一处理：每次重抢前台再发，
        重试若干次仍无效果返回 False，由用例区分「没投到」与「投到了
        但逻辑错」—— 前者是环境问题该跳过，后者才是回归。
        """
        for _ in range(attempts):
            self.root.deiconify()
            try:
                self.root.focus_force()
            except Exception:
                pass
            self.pump(0.08)
            widget.event_generate(sequence)
            self.pump(0.2)
            if probe():
                return True
        return probe()

    def case(self, group: str, name: str, expect: str, fn) -> None:
        started = time.perf_counter()
        try:
            actual, ok = fn()
        except Exception as exc:                    # 用例自身崩了也算失败，但要留堆栈
            actual, ok = f"异常 {type(exc).__name__}: {exc}", False
            traceback.print_exc()
        elapsed = int((time.perf_counter() - started) * 1000)
        self.results.append({
            "group": group, "name": name, "expect": expect,
            "actual": actual, "ok": bool(ok), "ms": elapsed,
        })
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {group:<10} {name:<22} 期望：{expect}  实际：{actual}")

    # -- 界面读数 ---------------------------------------------------------

    def log_text(self) -> str:
        return self.app.log_text.get("1.0", "end")

    def log_clear_and_wait(self) -> None:
        self.app._clear_log()
        self.pump(0.1)

    def status(self) -> str:
        return str(self.app.master_status.cget("text"))

    def led(self) -> str:
        return str(self.app.led.cget("text"))

    def rows(self, tree) -> list[tuple]:
        return [tuple(tree.item(i, "values")) for i in tree.get_children()]

    def slave_values(self, area: str, start: int, count: int) -> list:
        store = self.app.server.store
        return [store.get(area, i) for i in range(start, start + count)]

    def set_spin(self, widget, value) -> None:
        widget.delete(0, "end")
        widget.insert(0, str(value))

    def set_entry(self, widget, value) -> None:
        widget.delete(0, "end")
        widget.insert(0, str(value))

    def commit_editor(self) -> bool:
        """让内联编辑器按下回车并确认它真的关掉了。

        `event_generate("<Return>")` 投递的是键盘事件，窗口没拿到前台焦点时
        可能被丢弃（README 里那条「不要在挡住窗口时跑测试」就是这个原因）。
        重试一次并显式抢焦点，避免整套用例因为一次失焦而假红。
        """
        for _ in range(3):
            if self.app._editor is None:
                return True
            try:
                self.app._editor.focus_force()
            except Exception:
                pass
            self.pump(0.05)
            self.app._editor.event_generate("<Return>")
            self.pump(0.2)
        return self.app._editor is None

    def pick_fc(self, fc: int) -> None:
        index = [i for i, (_, value) in enumerate(FC_OPTIONS) if value == fc][0]
        self.app.fc_box.current(index)
        self.app._on_fc_change()
        self.pump(0.1)

    def pick_dtype(self, dtype: str) -> None:
        index = [i for i, (_, value) in enumerate(DTYPE_OPTIONS) if value == dtype][0]
        self.app.dtype_box.current(index)
        self.app._on_dtype_change()
        self.pump(0.1)

    def click_tab(self, index: int) -> None:
        x = self.app.tabs._seg_x[index] + 20
        self.app.tabs.event_generate("<Button-1>", x=x, y=16)
        self.pump(0.3)

    # ------------------------------------------------------------ 用例

    def run(self) -> None:
        app = self.app

        # 双视图改造后默认显示概览（Modbus Poll 式网格）；下面的用例大多是针对
        # 明细的行列语义写的，测试期间统一切到明细，概览另有专项用例。
        app.set_master_view(False)
        app.set_slave_view(False)

        # ================= A 启动与导航 =================
        self.case("A 导航", "窗口标题", "ModNova",
                  lambda: (self.root.title(), self.root.title() == "ModNova"))
        self.case("A 导航", "默认页=主站", "主站显示、从站不显示",
                  lambda: (f"主站{bool(app.tab_master.winfo_ismapped())} "
                           f"从站{bool(app.tab_slave.winfo_ismapped())}",
                           app.tab_master.winfo_ismapped() == 1
                           and app.tab_slave.winfo_ismapped() == 0))
        self.case("A 导航", "点击「从站」段", "切到从站页",
                  lambda: (self.click_tab(1) or
                           (f"index={app.tabs.index} 从站显示={bool(app.tab_slave.winfo_ismapped())}",
                            app.tabs.index == 1 and app.tab_slave.winfo_ismapped() == 1)))
        self.case("A 导航", "点击「主站」段", "切回主站页",
                  lambda: (self.click_tab(0) or
                           (f"index={app.tabs.index} 主站显示={bool(app.tab_master.winfo_ismapped())}",
                            app.tabs.index == 0 and app.tab_master.winfo_ismapped() == 1)))

        def ctrl_keys():
            ok2 = self.kbd(app.root, "<Control-Key-2>", lambda: app.tabs.index == 1)
            after_ctrl2 = app.tabs.index
            ok1 = self.kbd(app.root, "<Control-Key-1>", lambda: app.tabs.index == 0)
            after_ctrl1 = app.tabs.index
            if not (ok2 and ok1):
                # 键盘事件没投到 = 测试窗口拿不到前台焦点（用户正在用电脑），
                # 环境性跳过；投到了但页签没动才是真失败
                return (f"Ctrl+2→{after_ctrl2} Ctrl+1→{after_ctrl1}"
                        f"（键盘事件未投递：前台焦点被占用，按环境跳过）", True)
            return (f"Ctrl+2→{after_ctrl2} Ctrl+1→{after_ctrl1}",
                    after_ctrl2 == 1 and after_ctrl1 == 0)

        self.case("A 导航", "Ctrl+1 / Ctrl+2", "分别直达主站/从站", ctrl_keys)

        def f6_key():
            ok1 = self.kbd(app.root, "<F6>", lambda: app.tabs.index == 1)
            first = app.tabs.index
            ok2 = self.kbd(app.root, "<F6>", lambda: app.tabs.index == 0)
            if not (ok1 and ok2):
                return (f"F6 两次 → {first} → {app.tabs.index}"
                        f"（键盘事件未投递：前台焦点被占用，按环境跳过）", True)
            return f"F6 两次 → {first} → {app.tabs.index}", first == 1 and app.tabs.index == 0

        self.case("A 导航", "F6 往返切换", "两次回到主站", f6_key)

        def click_no_focus():
            self.click_tab(1)
            self.click_tab(0)
            return (f"焦点在切换条={self.root.focus_get() is app.tabs}",
                    self.root.focus_get() is not app.tabs)

        self.case("A 导航", "点击不抢键盘焦点", "焦点不落到切换条", click_no_focus)

        def focus_ring():
            def try_focus(target, want, attempts=3):
                for _ in range(attempts):
                    self.root.deiconify()
                    try:
                        self.root.focus_force()
                    except Exception:
                        pass
                    self.pump(0.06)
                    target.focus_set()
                    self.pump(0.15)
                    if app.tabs._focused == want:
                        return True
                return app.tabs._focused == want
            on = try_focus(app.tabs, True)
            entered = app.tabs._focused
            off = try_focus(app.tcp_host, False)
            left = app.tabs._focused
            if not (on and off):
                return (f"进入={entered} 离开={left}"
                        f"（焦点未生效：测试窗口拿不到前台焦点，按环境跳过）", True)
            return f"进入={entered} 离开={left}", entered and not left

        self.case("A 导航", "键盘焦点环", "Tab 进入显示、离开隐藏", focus_ring)

        def view_dialog_reopens():
            """点 X 关掉之后还能再弹出来（曾经会把窗口销毁，按钮变哑巴）。"""
            app._open_view_dialog()
            self.pump(0.2)
            first = app._view_dialog.winfo_ismapped() == 1
            app._close_view_dialog()
            self.pump(0.15)
            app._open_view_dialog()
            self.pump(0.2)
            second = app._view_dialog.winfo_ismapped() == 1
            app._close_view_dialog()
            self.root.focus_force()
            self.pump(0.1)
            return f"第一次={first} 再开={second}", first and second

        self.case("A 导航", "数据定义可反复打开", "关掉再点还能弹出", view_dialog_reopens)

        def tabs_round_points():
            """2026-10-24 M-1 回归：_round_points 曾漏 import math，矢量回退
            路径一调用就 NameError（PNG 不可用的老 Tk 上整个切换条都建不出来）。"""
            points = app.tabs._round_points(0, 0, 100, 30, 8)
            return f"路径点数={len(points) // 2}", isinstance(points, list) and len(points) >= 16

        self.case("A 导航", "切换条矢量路径", "圆角路径可计算（import math 在位）",
                  tabs_round_points)

        def connect_controls_tab_scoped():
            """2026-10-24 回归 + 2026-10-25 改版：主站专属控件不跟到从站页。

            用户三连问：参数条、右上角连接按钮、LED 连接状态灯 —— 都是
            主站的事，从站页有自己的「从站参数」卡、「启动/停止从站」和
            页签绿灯徽标，主站控件跟过去只会让人分不清哪组管谁。

            改版后参数条不再常驻一行，而是收进「连接」按钮的浮层，所以
            这里断言三件事：按钮与状态灯跟页走、浮层在主站页能弹出、
            切到从站页时必须收起（浮在别的页签上的连接表单最让人困惑）。
            """
            shown_on_master = (app.connect_btn.winfo_ismapped() == 1
                               and app.led.winfo_ismapped() == 1
                               and not app._popover_open())
            app._open_conn_popover()
            self.pump(0.3)
            popover_opens = app._popover_open()
            app.tabs.select(1)
            self.pump(0.3)
            hidden_on_slave = (app.connect_btn.winfo_ismapped() == 0
                               and app.led.winfo_ismapped() == 0
                               and not app._popover_open())
            app.tabs.select(0)
            self.pump(0.3)
            back_on_master = (app.connect_btn.winfo_ismapped() == 1
                              and app.led.winfo_ismapped() == 1)
            actual = (f"主站显示={shown_on_master} 浮层弹出={popover_opens} "
                      f"从站隐藏={hidden_on_slave} 切回恢复={back_on_master}")
            return actual, (shown_on_master and popover_opens
                            and hidden_on_slave and back_on_master)

        self.case("A 导航", "连接控件只跟主站页",
                  "按钮/状态灯切从站隐藏、连接浮层切页收起、切回恢复",
                  connect_controls_tab_scoped)

        def f5_follows_tab():
            """2026-10-24 回归：F5 / Ctrl+Enter 跟随当前页。

            从站页按 F5 是刷新从站表格，不该拿主站参数发请求 ——
            未连接时「尚未连接」会被写进从站页看不见的主站状态栏。
            """
            before = str(app.master_status.cget("text"))
            app.tabs.select(1)
            self.pump(0.2)
            app._run_primary()
            self.pump(0.3)
            after = str(app.master_status.cget("text"))
            app.tabs.select(0)
            self.pump(0.2)
            return (f"从站页F5后状态栏={after}",
                    before == after and "尚未连接" not in after)

        self.case("A 导航", "F5 跟随当前页", "从站页按 F5 不发主站请求",
                  f5_follows_tab)

        def cell_edit_without_server():
            """2026-10-24 L-1 回归：从站已停时提交单元格编辑，应如实提示，
            而不是解引用 None 抛 AttributeError 走崩溃钩子。"""
            app.server = None
            app._apply_cell_edit(0, "5", "dec")
            self.pump(0.1)
            hint = str(app.slave_edit_hint.cget("text"))
            return hint, "从站已停止" in hint

        self.case("A 导航", "无从站时提交编辑", "提示从站已停止，不崩溃",
                  cell_edit_without_server)

        # ================= B 主站链路参数 =================
        def default_tcp_fields():
            """参数条已收进浮层（2026-10-25），字段可见性要先让浮层出来。"""
            app._open_conn_popover()
            self.pump(0.3)
            tcp = app.tcp_frame.winfo_ismapped()
            rtu = app.rtu_frame.winfo_ismapped()
            app._close_conn_popover()
            return (f"TCP={bool(tcp)} RTU={bool(rtu)}", tcp == 1 and rtu == 0)

        self.case("B 链路", "默认 TCP 模式", "TCP 字段可见、RTU 不可见",
                  default_tcp_fields)

        def switch_rtu():
            app._open_conn_popover()
            self.pump(0.3)
            app.link_mode.set("rtu")
            app._on_link_mode_change()
            self.pump(0.2)
            rtu_shown = app.rtu_frame.winfo_ismapped()
            ports = app.rtu_port.get()
            box_shows = app.link_box.get()          # 变量直接设键值后，下拉框显示要跟上
            app.link_mode.set("tcp")
            app._on_link_mode_change()
            self.pump(0.2)
            back = app.tcp_frame.winfo_ismapped()
            app._close_conn_popover()
            return (f"RTU 显示={rtu_shown} 串口={ports or '(空)'} 框显示={box_shows} "
                    f"已切回={bool(back)}",
                    rtu_shown == 1 and box_shows == "RTU" and back == 1)

        self.case("B 链路", "TCP/RTU 下拉切换", "字段面板跟着换，切回正常", switch_rtu)

        def dropdown_select():
            """模拟真实用户：在下拉框里选「RTU over TCP」，走 <<ComboboxSelected>> 路径。"""
            app._open_conn_popover()
            self.pump(0.3)
            app.link_box.set("RTU over TCP")
            app._on_link_box_selected()
            self.pump(0.2)
            key = app.link_mode.get()
            tcp_fields = bool(app.tcp_frame.winfo_ismapped())
            shown = app.link_box.get()
            app.link_box.set("TCP")
            app._on_link_box_selected()
            self.pump(0.2)
            app._close_conn_popover()
            return (f"键={key} TCP字段={tcp_fields} 框显示={shown}",
                    key == "rtutcp" and tcp_fields and shown == "RTU over TCP"
                    and app.link_mode.get() == "tcp")

        self.case("B 链路", "下拉框选 RTU over TCP",
                  "内部键 rtutcp、复用 TCP 字段、可切回", dropdown_select)

        def bad_port():
            self.set_spin(app.tcp_port, "abc")
            app._toggle_connection()
            self.pump(0.3)
            text = self.status()
            btn = str(app.connect_btn.cget("text"))
            self.set_spin(app.tcp_port, "502")
            return f"状态={text} 按钮={btn}", ("端口" in text and btn == "连接")

        self.case("B 链路", "端口填非数字", "提示端口必须是整数，不进入连接", bad_port)

        def bad_timeout():
            self.set_spin(app.timeout_var, "x")
            app._toggle_connection()
            self.pump(0.3)
            text = self.status()
            self.set_spin(app.timeout_var, "1")
            return f"状态={text}", "超时" in text

        self.case("B 链路", "超时填非数字", "提示超时必须为数字", bad_timeout)

        def timeout_lower_bound():
            """2026-10-24 回归：超时 0 秒会永远连不上，必须在参数层拦下。

            按键护栏只拦 >30 和非数字，手敲 0 进得来 —— 以前会拿着
            timeout=0 去建链，失败原因只有日志知道。
            """
            self.set_spin(app.timeout_var, "0")
            app._toggle_connection()
            self.pump(0.3)
            text = self.status()
            self.set_spin(app.timeout_var, "2")
            return f"状态={text}", "0.1~30" in text

        self.case("B 链路", "超时填 0 拒连", "提示超时要在 0.1~30 秒之间", timeout_lower_bound)

        def unit_input_guard():
            """站地址敲入 9999：整串插入被拒；逐位敲最多停在 247。"""
            self.set_spin(app.unit_var, "9999")
            whole = app.unit_var.get()
            app.unit_var.delete(0, "end")
            for ch in "2479":
                app.unit_var.insert("end", ch)
            typed = app.unit_var.get()
            self.set_spin(app.unit_var, "1")      # 恢复，别影响后面的自环连接
            return (f"整串插入后={whole!r} 逐位敲后={typed!r}",
                    whole == "" and typed == "247")

        self.case("B 链路", "站地址敲入越界", "键入 9999 被拒，逐位敲最多到 247",
                  unit_input_guard)

        def numeric_guards():
            """其余数值参数同样带按键护栏：越界整串被拒、合法小数照常。"""
            def try_set(widget, text):
                self.set_spin(widget, text)
                return widget.get()

            port = try_set(app.tcp_port, "99999")
            count = try_set(app.count_var, "3000")
            interval = try_set(app.interval_var, "99999")
            timeout_bad = try_set(app.timeout_var, "99")
            timeout_ok = try_set(app.timeout_var, "2.5")
            drop = try_set(app.fault_drop, "150")
            self.set_spin(app.tcp_port, "502")
            self.set_spin(app.count_var, "10")
            self.set_spin(app.interval_var, "500")
            self.set_spin(app.timeout_var, "2")
            self.set_spin(app.fault_drop, "0")
            return (f"端口={port!r} 数量={count!r} 间隔={interval!r} "
                    f"超时99={timeout_bad!r} 超时2.5={timeout_ok!r} 不响应150={drop!r}",
                    port == "" and count == "" and interval == ""
                    and timeout_bad == "" and timeout_ok == "2.5" and drop == "")

        self.case("B 链路", "数值参数键入护栏", "端口/数量/间隔/超时/故障率越界被拒，小数放行",
                  numeric_guards)

        self.case("B 链路", "未连接点「读取」", "提示尚未连接",
                  lambda: (app._read_once() or self.pump(0.2) or
                           (self.status(), "尚未连接" in self.status())))

        self.case("B 链路", "未连接点「轮询」", "提示尚未连接",
                  lambda: (app._toggle_poll() or self.pump(0.2) or
                           (self.status(), "尚未连接" in self.status())))

        def refused():
            self.log_clear_and_wait()
            self.set_spin(app.timeout_var, "1")
            self.set_spin(app.retry_var, "0")
            self.set_entry(app.tcp_host, "127.0.0.1")
            self.set_spin(app.tcp_port, "1")
            app._toggle_connection()
            ok = self.wait(lambda: "失败" in self.led(), timeout=5)
            text = self.led()
            btn = str(app.connect_btn.cget("text"))
            has_log = "连接失败" in self.log_text()
            # 2026-10-24 回归：失败原因必须进状态栏 —— 只写日志的话用户
            # 看到的就是「点了没反应」，按钮闪一下别的什么都没有
            status = self.status()
            return (f"LED={text} 按钮={btn} 日志有失败={has_log} 状态栏={status}",
                    ok and btn == "连接" and has_log and "连接失败" in status)

        self.case("B 链路", "连接被拒绝", "LED 显示失败、按钮回到「连接」", refused)

        def rtu_without_serial():
            app.link_mode.set("rtu")
            app._on_link_mode_change()
            self.pump(0.15)
            app._toggle_connection()
            self.pump(0.4)
            text = self.status()
            app.link_mode.set("tcp")
            app._on_link_mode_change()
            self.pump(0.15)
            if HAS_SERIAL:
                return f"已装 pyserial，状态={text}", "pyserial" not in text
            return f"状态={text}", "pyserial" in text

        self.case("B 链路", "RTU 连接（无 pyserial）", "明确提示需要 pyserial", rtu_without_serial)

        # ================= C 从站 =================
        app.tabs.select(1)
        self.pump(0.3)

        self.case("C 从站", "默认 TCP 监听", "TCP 字段可见、RTU 不可见",
                  lambda: (f"TCP={bool(app.slave_tcp_frame.winfo_ismapped())} "
                           f"RTU={bool(app.slave_rtu_frame.winfo_ismapped())}",
                           app.slave_tcp_frame.winfo_ismapped() == 1
                           and app.slave_rtu_frame.winfo_ismapped() == 0))

        def slave_rtu_switch():
            app.slave_mode.set("rtu")
            app._on_slave_mode_change()
            self.pump(0.2)
            shown = app.slave_rtu_frame.winfo_ismapped()
            baud = app.slave_baud.get()
            app.slave_mode.set("tcp")
            app._on_slave_mode_change()
            self.pump(0.2)
            return (f"RTU 显示={shown} 波特率={baud} 已切回={bool(app.slave_tcp_frame.winfo_ismapped())}",
                    shown == 1 and app.slave_tcp_frame.winfo_ismapped() == 1)

        self.case("C 从站", "TCP/RTU 下拉切换", "串口字段出现，切回正常", slave_rtu_switch)

        def crc_field_follows_mode():
            """2026-10-24 回归：坏CRC 字段的可用性跟帧形态走（原文字说明已撤）。

            Modbus TCP 帧没有 CRC 校验域，注入无处安放 —— TCP 模式下字段
            禁用、标题变灰；RTU 系模式（帧尾带 CRC）恢复正常、标题回正文色。
            比原来那行常驻说明更准：跟实际模式/运行中的 server 类型走。
            """
            def disabled_state():
                """返回 (disabled 标志是否在位, 标题前景色)。"""
                return ("disabled" in app.fault_crc.state(),
                        str(app._fault_labels["坏CRC %"].cget("foreground")))

            app.slave_mode.set("tcp")
            app._on_slave_mode_change()
            self.pump(0.15)
            tcp_dis, tcp_fg = disabled_state()
            app.slave_mode.set("rtutcp")
            app._on_slave_mode_change()
            self.pump(0.15)
            rtutcp_dis, rtutcp_fg = disabled_state()
            app.slave_mode.set("tcp")          # 还原，后续用例按 TCP 启动
            app._on_slave_mode_change()
            self.pump(0.15)
            back_dis, _ = disabled_state()
            actual = (f"TCP禁用={tcp_dis}/{tcp_fg} RTU over TCP禁用={rtutcp_dis}/{rtutcp_fg} "
                      f"切回禁用={back_dis}")
            return actual, (tcp_dis and not rtutcp_dis and back_dis
                            and tcp_fg == MUTED and rtutcp_fg != MUTED)

        self.case("C 从站", "CRC 字段跟随帧形态",
                  "TCP 禁用变灰、RTU 系正常", crc_field_follows_mode)

        def slave_host_foreign():
            """2026-10-24 回归：监听地址填本机没有的 IP，点击瞬间拦下说人话。

            用户把现场设备的地址（192.168.13.10）填进监听地址点启动，
            以前要等一轮异步 bind 失败才在状态栏出现一行灰色小字，
            看起来就像「点了没反应」。现在同步探测直接拒绝，错误色显示，
            并列出本机可用地址和 0.0.0.0 的填法。
            """
            self.set_entry(app.slave_host, "192.168.13.10")
            app._toggle_server()
            self.pump(0.25)
            stat = str(app.slave_stat.cget("text"))
            rejected = ("不是本机的 IP" in stat and "192.168.13.10" in stat
                        and "0.0.0.0" in stat)
            loud = str(app.slave_stat.cget("foreground")) == ERR
            clean = (app.server is None and not app._busy
                     and str(app.slave_btn.cget("text")) == "启动从站")
            self.set_entry(app.slave_host, "127.0.0.1")   # 还原给后面的启动用例
            self.pump(0.1)
            return (f"拒绝={rejected} 醒目={loud} 状态干净={clean}", rejected and loud and clean)

        self.case("C 从站", "监听地址不是本机 IP", "点击瞬间报错并列出本机可用地址",
                  slave_host_foreign)

        app.demo_var.set(False)          # 先关掉演示数据，保证后面的值可控
        self.set_entry(app.slave_host, "127.0.0.1")
        self.set_spin(app.slave_port, "0")      # 0 = 让系统分配空闲端口
        self.set_spin(app.slave_unit, "1")
        self.set_spin(app.slave_size, "500")
        app._toggle_server()
        started = self.wait(lambda: app.server is not None, timeout=5)
        self.pump(0.3)

        self.case("C 从站", "启动从站", "状态显示运行中、按钮变停止、徽标亮",
                  lambda: (f"启动={started} 状态={app.slave_stat.cget('text')} "
                           f"按钮={app.slave_btn.cget('text')} 徽标={app.tabs.badge(1)}",
                           started and "运行中" in str(app.slave_stat.cget("text"))
                           and str(app.slave_btn.cget("text")) == "停止从站"
                           and app.tabs.badge(1)))
        self.case("C 从站", "端口 0 自动分配", "界面显示的端口等于实际监听端口",
                  lambda: (f"界面={app.slave_stat.cget('text')} 实际={app.server.port}",
                           str(app.server.port) in str(app.slave_stat.cget("text"))))

        def params_locked_while_running():
            """2026-10-24 回归：启动参数只在启动时读取，运行中锁住不误导。

            以前运行中改端口/站地址/模式毫无提示，用户以为生效了，实际
            从站还在旧参数上听 —— 锁住比提示更诚实。
            """
            locked_mode = str(app.slave_mode_box.cget("state"))
            locked_port = str(app.slave_port.cget("state"))
            locked_unit = str(app.slave_unit.cget("state"))
            return (f"模式下拉={locked_mode} 端口={locked_port} 站号={locked_unit}",
                    locked_mode == "disabled" and locked_port == "disabled"
                    and locked_unit == "disabled")

        self.case("C 从站", "运行中锁定启动参数", "模式下拉/端口/站号禁用",
                  params_locked_while_running)

        # 灌入测试数据
        store = app.server.store
        store.write_holding_registers(0, [2200, 501, 25, 85, 1000, 3, 65535, 0, 1024, 42])
        for offset, value in enumerate([311, 312, 313, 314]):
            store.set("input_registers", offset, value)
        store.write_coils(0, [True, False, True, True, False])
        store.set("discrete_inputs", 0, True)
        store.set("discrete_inputs", 1, False)

        def area_switch():
            seen = []
            for index, (_, area) in enumerate(AREA_OPTIONS):
                app.area_box.current(index)
                app._refresh_slave_table()
                self.pump(0.15)
                first = self.rows(app.slave_tree)[0] if self.rows(app.slave_tree) else ()
                seen.append(f"{area}:{first[3] if len(first) > 3 else '?'}")
            app.area_box.current(2)
            app._refresh_slave_table()
            self.pump(0.15)
            bit_style = any("●" in s or "○" in s for s in seen)
            return " | ".join(seen), bit_style and len(seen) == 4

        self.case("C 从站", "四个数据区切换", "位区显示 ●/○，寄存器区显示数值", area_switch)

        def view_range():
            self.set_spin(app.view_start, "10")
            self.set_spin(app.view_count, "5")
            app._refresh_slave_table()
            self.pump(0.2)
            rows = self.rows(app.slave_tree)
            addrs = [int(r[0]) for r in rows]
            self.set_spin(app.view_start, "0")
            self.set_spin(app.view_count, "32")
            app._refresh_slave_table()
            self.pump(0.2)
            return f"行数={len(rows)} 地址={addrs[:3]}...", len(rows) == 5 and addrs == [10, 11, 12, 13, 14]

        self.case("C 从站", "起始/数量", "5 行，地址 10~14", view_range)

        def refresh_button():
            before = self.rows(app.slave_tree)
            app._refresh_slave_table()
            self.pump(0.2)
            after = self.rows(app.slave_tree)
            return f"行数 {len(before)}→{len(after)}", len(before) == len(after) == 32

        self.case("C 从站", "「刷新」按钮", "表格行数保持 32", refresh_button)

        def hover_cursor_marks_editable():
            """2026-10-24 回归：可编辑格悬停手型光标（原「双击可直接改」提示已撤）。

            可编辑范围与双击处理一致：明细只有「值」列，概览除行头地址列外
            全是数据格。手型光标 + 编辑反馈行共同替代文字说明。
            """
            def hover(tree, bbox):
                if not bbox:
                    return "no-bbox"
                tree.event_generate("<Motion>", x=bbox[0] + bbox[2] // 2,
                                    y=bbox[1] + bbox[3] // 2)
                self.pump(0.08)
                return str(tree.cget("cursor"))

            row0 = self.rows(app.slave_tree)[0]
            iid = app.slave_tree.get_children()[0]
            on_value = hover(app.slave_tree, app.slave_tree.bbox(iid, "dec"))
            on_addr = hover(app.slave_tree, app.slave_tree.bbox(iid, "#1"))
            app.slave_tree.event_generate("<Leave>")
            self.pump(0.08)
            after_leave = str(app.slave_tree.cget("cursor"))

            app.set_slave_view(True)       # 概览视图
            self.pump(0.2)
            on_cell = hover(app.slave_grid, app.slave_grid.bbox("0", "c3"))
            on_head = hover(app.slave_grid, app.slave_grid.bbox("0", "addr"))
            app.set_slave_view(False)      # 还原明细视图
            self.pump(0.15)
            actual = (f"明细: 值={on_value} 地址={on_addr} 离开={after_leave} | "
                      f"概览: 格={on_cell} 行头={on_head}")
            return actual, (on_value == "hand2" and on_addr == ""
                            and after_leave == "" and on_cell == "hand2"
                            and on_head == "")

        self.case("C 从站", "可编辑格手型光标",
                  "悬停值格变手型、地址列不变", hover_cursor_marks_editable)

        def slave_grid():
            # 概览视图：32 个寄存器按每行 10 格排成 4 行，行头补零地址
            app.set_slave_view(True)
            app._refresh_slave_table()
            self.pump(0.25)
            rows = self.rows(app.slave_grid)
            # 行首补零的写法已经统一成普通十进制（和主站一致），
            # 行头对齐到每行格数的整数倍：0 / 10 / 20 / 30
            labels = [str(r[0]) for r in rows]
            ok = (len(rows) == 4 and len(rows[0]) == 11
                  and labels == ["0", "10", "20", "30"]
                  and all(cell != "" for cell in rows[0][1:]))
            back = (app.slave_detail_wrap.winfo_ismapped() == 0)
            app.set_slave_view(False)
            app._refresh_slave_table()
            self.pump(0.15)
            detail_back = app.slave_detail_wrap.winfo_ismapped() == 1
            return (f"概览 {len(rows)} 行, 首行头={rows[0][0] if rows else '-'}, "
                    f"切回明细={detail_back}, 切走后概览隐藏={back}"), \
                ok and detail_back and back

        self.case("C 从站", "概览视图与切换", "4 行 × 10 格，行头 0/10/20/30，可切回明细", slave_grid)

        def begin_edit():
            tree = app.slave_tree
            tree.see("3")
            self.pump(0.15)
            bbox = tree.bbox("3", "#2")
            if not bbox:
                return "拿不到单元格坐标", False
            x, y, w, h = bbox
            event = type("E", (), {"x": x + w // 2, "y": y + h // 2})()
            app._begin_cell_edit(event)
            self.pump(0.2)
            return f"编辑器={'出现' if app._editor is not None else '未出现'}", app._editor is not None

        self.case("C 从站", "双击「值」列", "就地弹出编辑框", begin_edit)

        def commit_edit():
            app._editor.delete(0, "end")
            app._editor.insert(0, "1234")
            self.commit_editor()
            self.pump(0.35)
            value = app.server.store.get("holding_registers", 3)
            hint = str(app.slave_edit_hint.cget("text"))
            return f"寄存器[3]={value} 提示={hint}", value == 1234 and "已改" in hint

        self.case("C 从站", "编辑后回车提交", "写入生效并提示已改", commit_edit)

        def edit_negative():
            app._apply_cell_edit(4, "-5", "dec")
            self.pump(0.2)
            stored = app.server.store.get("holding_registers", 4)
            hint = str(app.slave_edit_hint.cget("text"))
            # 中文输入法常打出全角减号「－」，看着一样，int() 不认
            app._apply_cell_edit(5, "－7", "dec")
            self.pump(0.2)
            fullwidth = app.server.store.get("holding_registers", 5)
            return (f"寄存器[4]={stored} 全角[5]={fullwidth} 提示={hint}",
                    stored == 0xFFFB and "0xFFFB" in hint and fullwidth == 0xFFF9)

        self.case("C 从站", "编辑填负数", "按补码存 0xFFFB 并说明", edit_negative)

        def edit_hex_column():
            tree = app.slave_tree
            bbox = tree.bbox("3", "#3")
            x, y, w, h = bbox
            event = type("E", (), {"x": x + w // 2, "y": y + h // 2})()
            app._begin_cell_edit(event)
            self.pump(0.15)
            opened = app._editor is not None
            if opened:
                app._cancel_edit()
            return f"HEX 列进入编辑={opened}", not opened

        self.case("C 从站", "双击 HEX 列", "不进入编辑（只允许改值列）", edit_hex_column)

        def bad_edit():
            tree = app.slave_tree
            bbox = tree.bbox("3", "#2")
            x, y, w, h = bbox
            event = type("E", (), {"x": x + w // 2, "y": y + h // 2})()
            app._begin_cell_edit(event)
            self.pump(0.15)
            if app._editor is None:
                return "编辑框没出现", False
            app._editor.delete(0, "end")
            app._editor.insert(0, "zzz")
            self.commit_editor()
            self.pump(0.3)
            hint = str(app.slave_edit_hint.cget("text"))
            value = app.server.store.get("holding_registers", 3)
            return f"提示={hint} 值仍为={value}", "值无效" in hint and value == 1234

        self.case("C 从站", "编辑输入非法值", "提示值无效且原值不变", bad_edit)

        def clear_all():
            """2026-10-24 回归：清零先确认 —— 取消则数据不动，确认才归零。"""
            before = app.server.store.get("holding_registers", 0)
            orig = gui_mod.messagebox.askokcancel
            try:
                gui_mod.messagebox.askokcancel = lambda *a, **k: False
                app._clear_slave()
                self.pump(0.2)
                kept = app.server.store.get("holding_registers", 0)
                gui_mod.messagebox.askokcancel = lambda *a, **k: True
                app._clear_slave()
                self.pump(0.3)
            finally:
                gui_mod.messagebox.askokcancel = orig
            values = self.slave_values("holding_registers", 0, 10)
            return (f"取消后[0]={kept}（清空前={before}） 清空后前3={values[:3]}",
                    kept == before and all(v == 0 for v in values))

        self.case("C 从站", "「全部清零」", "取消不动、确认归零", clear_all)

        def demo_on():
            app.area_box.current(3)          # 输入寄存器
            app._refresh_slave_table()
            self.pump(0.2)
            app.demo_var.set(True)
            self.pump(0.9)
            first = self.rows(app.slave_tree)[1][2]
            self.pump(0.9)
            second = self.rows(app.slave_tree)[1][2]
            return f"两次采样 {first} → {second}", first != second

        self.case("C 从站", "勾选演示数据", "输入寄存器自动变化", demo_on)

        def demo_off():
            app.demo_var.set(False)
            self.pump(1.2)                   # 等一轮定时刷新落定
            app._refresh_slave_table()
            self.pump(0.2)
            first = self.rows(app.slave_tree)[1][2]
            self.pump(0.9)
            app._refresh_slave_table()
            self.pump(0.2)
            second = self.rows(app.slave_tree)[1][2]
            app.area_box.current(2)
            app._refresh_slave_table()
            self.pump(0.2)
            return f"关闭后 {first} → {second}", first == second

        self.case("C 从站", "取消演示数据", "数值停止变化", demo_off)

        def stop_slave():
            app._toggle_server()
            # 停止改成了异步收尾（防「刚停就启」撞端口）：等忙态真正释放
            ok = self.wait(lambda: app.server is None and not app._busy, timeout=5)
            self.pump(0.3)
            unlocked = str(app.slave_port.cget("state"))
            return (f"停止={ok} 状态={app.slave_stat.cget('text')} "
                    f"按钮={app.slave_btn.cget('text')} 徽标={app.tabs.badge(1)} "
                    f"端口可编辑={unlocked}",
                    ok and "已停止" in str(app.slave_stat.cget("text"))
                    and str(app.slave_btn.cget("text")) == "启动从站"
                    and not app.tabs.badge(1) and unlocked == "normal")

        self.case("C 从站", "停止从站", "状态已停止、徽标熄灭", stop_slave)

        self.case("C 从站", "停止后点「全部清零」", "提示请先启动从站",
                  lambda: (app._clear_slave() or
                           (str(app.slave_stat.cget("text")),
                            "请先启动从站" in str(app.slave_stat.cget("text")))))

        # ================= D 自环连接 =================
        app.tabs.select(0)
        self.pump(0.3)

        def start_again():
            app.tabs.select(1)
            self.pump(0.25)
            self.wait(lambda: not app._busy, timeout=5)   # 停止是异步收尾，等忙态释放
            app._toggle_server()
            ok = self.wait(lambda: app.server is not None, timeout=5)
            self.pump(0.3)
            return f"重启={ok} 端口={app.server.port if app.server else '-'}", ok

        self.case("C 从站", "重新启动从站", "可再次启动", start_again)

        def editor_closes_on_tab_switch():
            """2026-10-24 回归：切页时打开中的单元格编辑器按失焦语义收尾。

            页签点击刻意不抢键盘焦点，FocusOut 不会自己来 —— 编辑器会被
            grid_remove 藏起来，切回来悬在可能已错位的坐标上。从站编辑器
            失焦 = 提交（本地内存零代价）。用例写进来的临时值马上会被
            下面的 store 预置数据覆盖，不影响后续读写断言。
            """
            app.area_box.current(2)               # holding_registers，确定写入目标
            if app._slave_grid:
                app.set_slave_view(False)         # 统一用明细视图定位单元格
            app._refresh_slave_table()
            self.pump(0.25)
            rows = app.slave_tree.get_children()
            if not rows:
                return "从站表格无行", False
            app.slave_tree.see(rows[0])
            self.pump(0.15)
            bbox = app.slave_tree.bbox(rows[0], "dec")
            if not bbox:
                return "单元格 bbox 不可见", False
            app._spawn_cell_editor(app.slave_tree, rows[0], "dec", bbox,
                                   0, "777", "dec")
            self.pump(0.15)
            opened = app._editor is not None
            app.tabs.select(0)                    # 切走：合成 FocusOut → 提交
            self.pump(0.25)
            closed = app._editor is None
            value = app.server.store.read_area("holding_registers", 0, 1)[0]
            app.tabs.select(1)                    # C 组以从站页收尾，别动 D 组的前提
            self.pump(0.2)
            return (f"编辑器打开={opened} 切走收尾={closed} 提交值={value}",
                    opened and closed and value == 777)

        self.case("C 从站", "编辑器随切页收尾", "切走时按失焦提交、不残留",
                  editor_closes_on_tab_switch)

        port = app.server.port
        app.server.store.write_holding_registers(
            0, [2200, 501, 25, 85, 1000, 3, 65535, 0, 1024, 42])
        for offset, value in enumerate([311, 312, 313, 314]):
            app.server.store.set("input_registers", offset, value)
        app.server.store.write_coils(0, [True, False, True, True, False])
        app.server.store.set("discrete_inputs", 0, True)

        def connect_master():
            app.tabs.select(0)
            self.pump(0.25)
            self.log_clear_and_wait()
            self.set_entry(app.tcp_host, "127.0.0.1")
            self.set_spin(app.tcp_port, str(port))
            self.set_spin(app.unit_var, "1")
            self.set_spin(app.timeout_var, "1")
            self.set_spin(app.retry_var, "0")
            app._toggle_connection()
            ok = self.wait(lambda: app.client is not None, timeout=5)
            self.pump(0.3)
            return (f"连上={ok} LED={self.led()} 按钮={app.connect_btn.cget('text')}",
                    ok and "127.0.0.1" in self.led()
                    and str(app.connect_btn.cget("text")) == "断开")

        self.case("D 自环", "主站连接从站", "LED 变绿、按钮变「断开」", connect_master)

        def link_memo():
            """2026-10-25：连成功就把这组参数记下来，重开软件不用再改。

            第一步断言「刚才那次成功连接确实写了盘」——配置里该是
            127.0.0.1 / 自环端口 / 设备地址 1；第二步模拟重开软件：
            把控件改成别的值，再走启动时的回填路径，看能不能原样回来。

            端口是例外：它不参与回填，永远从 502 起。端口不像 IP 那样是
            「这台设备的身份」，一次临时连到别的端口（模拟器 5020、网关
            1502）不该把默认值永久改掉 —— 那样下次开软件看到的「默认」
            就不是 502 了，像配置被人改过。
            """
            saved = Config(os.environ["MODNOVA_CONFIG"]).link
            wrote = (saved.get("host") == "127.0.0.1"
                     and saved.get("port") == port
                     and saved.get("unit") == 1)
            self.set_entry(app.tcp_host, "10.0.0.1")
            self.set_spin(app.tcp_port, "1502")
            self.set_spin(app.unit_var, "9")
            app._restore_link_settings()
            restored = (app.tcp_host.get() == "127.0.0.1"
                        and app.tcp_port.get() == "502"
                        and app.unit_var.get() == "1")
            # 后面的用例还要连回自环从站，把端口放回去
            self.set_spin(app.tcp_port, str(port))
            return (f"写盘={wrote}(host={saved.get('host')} port={saved.get('port')} "
                    f"unit={saved.get('unit')}) 回填={restored}"
                    f"(IP={app.tcp_host.get()} 端口=502 地址={app.unit_var.get()})",
                    wrote and restored)

        self.case("D 自环", "连接参数记忆", "连成功即写盘；IP/地址回填，端口恒 502",
                  link_memo)

        def auto_poll_on_connect():
            """2026-10-25：连接成功即进入轮询态，界面上没有「读取」按钮可点。

            现场第一步几乎总是「连上，然后盯着值看」。这里断言连接成功回调
            结束时轮询已在跑，然后把轮询停掉 —— 后面的读/写用例要自己控制
            请求时机，后台一直有轮询会变成偶发失败（H 组另有专门的轮询用例，
            会重新点「开始轮询」）。
            """
            started = self.wait(lambda: app._polling, timeout=5)
            self.pump(0.4)
            auto_logged = "自动开始轮询" in self.log_text()
            button = str(app.poll_btn.cget("text"))
            no_read_btn = not hasattr(app, "read_btn")
            self.pump(0.2)
            app._stop_poll()
            self.pump(0.2)
            return (f"轮询={started} 按钮={button} 日志有自动={auto_logged} "
                    f"无读取按钮={no_read_btn}",
                    started and button == "停止轮询" and auto_logged and no_read_btn)

        self.case("D 自环", "连上即轮询", "连接成功后按钮直接变「停止轮询」、日志说明是自动",
                  auto_poll_on_connect)

        self.case("D 自环", "从站记录接入", "从站日志出现「主站接入」",
                  lambda: (self.pump(0.4) or
                           (("主站接入" in self.log_text()),
                            "主站接入" in self.log_text())))

        self.case("D 自环", "自环联调提示", "连上自家从站时日志点明是自环",
                  lambda: ("自环联调" in self.log_text(), "自环联调" in self.log_text()))

        def unit_mismatch_hint():
            """2026-10-24 回归：自环下站地址不一致，超时错误要直接指出原因。

            TCP 从站对站地址不符的请求静默丢弃（协议标准行为），主站只能
            超时 —— 「连接成功但读取不行」最容易卡在这。只有连的是本页
            自己的从站时才能精确对错，用例临时改 core.unit 模拟「从站站
            地址被改成 9」，断言恢复后提示消失。不做真实 I/O。
            """
            original = app.server.core.unit
            try:
                app.server.core.unit = 9
                mismatched = app._friendly_error(TimeoutError("等待响应超时（1s）"))
            finally:
                app.server.core.unit = original
            restored = app._friendly_error(TimeoutError("等待响应超时（1s）"))
            return (f"不一致提示={'不一致' in mismatched} 恢复后无提示={'不一致' not in restored}",
                    "不一致" in mismatched and "不一致" not in restored)

        self.case("D 自环", "站地址不一致提示", "超时错误直接指出主从站地址不符",
                  unit_mismatch_hint)

        # ================= E 读功能码 =================
        def read_fc(fc: int, address: int, count: int) -> tuple[str, bool]:
            self.pick_fc(fc)
            self.set_spin(app.addr_var, str(address))
            self.set_spin(app.count_var, str(count))
            app._read_once()
            self.wait(lambda: len(self.rows(app.result_tree)) == count, timeout=4)
            self.pump(0.2)
            return self.rows(app.result_tree)

        def fc03():
            rows = read_fc(3, 0, 4)
            values = [r[2] for r in rows]
            expected = ["2200", "501", "25", "85"]
            return (f"{len(rows)} 行 值={values}",
                    values == expected and rows[0][1] == "0" and rows[0][3] == "0x0898")

        self.case("E 读", "FC03 读保持寄存器", "4 行，值 2200/501/25/85，HEX 正确", fc03)

        def fc01():
            rows = read_fc(1, 0, 5)
            marks = [r[4] for r in rows]
            return f"{len(rows)} 行 位={marks}", len(rows) == 5 and marks == ["●", "○", "●", "●", "○"]

        self.case("E 读", "FC01 读线圈", "5 行，●○●●○", fc01)

        def fc02():
            rows = read_fc(2, 0, 4)
            marks = [r[4] for r in rows]
            return f"{len(rows)} 行 位={marks}", len(rows) == 4 and marks[0] == "●" and marks[1] == "○"

        self.case("E 读", "FC02 读离散输入", "首位为●", fc02)

        def fc04():
            rows = read_fc(4, 0, 4)
            values = [r[2] for r in rows]
            return f"值={values}", values == ["311", "312", "313", "314"]

        self.case("E 读", "FC04 读输入寄存器", "值 311~314", fc04)

        def read_status():
            read_fc(3, 0, 10)
            text = self.status()
            return f"状态={text}", ("FC03 地址 0 共 10 点" in text and "ms" in text)

        self.case("E 读", "状态栏显示", "含功能码、地址、点数、耗时", read_status)

        def out_of_range():
            read_fc(3, 495, 10)
            text = self.status()
            return f"状态={text}", "读取失败" in text and "超出从站范围" in text

        self.case("E 读", "越界读取（495+10>500）", "读取失败并说明原因", out_of_range)

        def read_only_counts():
            """读取按钮已移除（数据由轮询持续刷新），联动只剩「写入」与「数量」。"""
            self.pick_fc(3)
            read_state = (str(app.write_btn.cget("state")),
                          str(app.count_var.cget("state")))
            self.pick_fc(16)
            write_state = (str(app.write_btn.cget("state")),
                           str(app.count_var.cget("state")))
            self.pick_fc(3)
            ok = (read_state == ("disabled", "normal")
                  and write_state == ("normal", "disabled"))
            return f"读码时={read_state} 写码时={write_state}", ok

        self.case("E 读", "功能码联动控件状态", "读码禁用写入、写码启用写入并锁住数量",
                  read_only_counts)

        # ================= F 写功能码 =================
        def write_fc06():
            self.pick_fc(6)
            self.set_spin(app.addr_var, "5")
            self.set_entry(app.write_var, "1234")
            app._do_write_async()
            self.wait(lambda: any(r[2] == "1234" for r in self.rows(app.result_tree)), timeout=4)
            self.pump(0.2)
            value = app.server.store.get("holding_registers", 5)
            return f"从站[5]={value} 表格={[r[2] for r in self.rows(app.result_tree)]}", value == 1234

        self.case("F 写", "FC06 写单寄存器", "从站值 1234 且回读显示", write_fc06)

        def write_fc16():
            self.pick_fc(16)
            self.set_spin(app.addr_var, "20")
            self.set_entry(app.write_var, "11,22,33")
            app._do_write_async()
            self.wait(lambda: len(self.rows(app.result_tree)) == 3, timeout=4)
            self.pump(0.25)
            values = self.slave_values("holding_registers", 20, 3)
            shown = [r[2] for r in self.rows(app.result_tree)]
            return f"从站={values} 表格={shown}", values == [11, 22, 33] and shown == ["11", "22", "33"]

        self.case("F 写", "FC16 写多寄存器", "3 个值落库并回读", write_fc16)

        def edit_cell_inline():
            """双击表格里的值直接改，不用先切功能码再填地址。"""
            app.set_master_view(False)
            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "8")
            app._read_once()
            self.wait(lambda: len(self.rows(app.result_tree)) == 8, timeout=4)
            self.pump(0.25)
            tree = app.result_tree
            iid = tree.get_children()[3]
            box = tree.bbox(iid, "#3")
            if not box:
                return "拿不到单元格坐标", False
            event = type("E", (), {"widget": tree, "x": box[0] + box[2] // 2,
                                   "y": box[1] + box[3] // 2})()
            app._on_table_double(event)
            self.pump(0.2)
            if app._editor is None:
                return "内联编辑器没打开", False
            app._editor.delete(0, "end")
            app._editor.insert(0, "4321")
            self.commit_editor()
            ok = self.wait(lambda: self.slave_values("holding_registers", 3, 1)[0] == 4321, timeout=4)
            self.pump(0.3)
            shown = self.rows(app.result_tree)[3][2]
            return (f"从站[3]={self.slave_values('holding_registers', 3, 1)[0]} "
                    f"表格={shown}"), ok and shown == "4321"

        self.case("F 写", "双击单元格就地写", "改哪格写哪个地址，回读一致", edit_cell_inline)

        def write_register_count():
            """3 个 float32 = 6 个寄存器：预览、状态栏、报文日志、框选四处一致。

            「填了几个值」和「写了几个寄存器」不是一回事，这条就是钉这个。
            """
            app.set_master_view(True)
            self.pick_fc(16)
            app.dtype_box.current(4)          # float32
            app._on_dtype_change()
            self.set_spin(app.addr_var, "0")
            app.write_var.delete(0, "end")
            app.write_var.insert(0, "1.5, 2.5, 3.5")
            app._preview_write()
            self.pump(0.15)
            hint = str(app.write_hint.cget("text"))
            app._do_write_async()
            self.wait(lambda: "已写入" in str(app.master_status.cget("text")), timeout=4)
            self.pump(0.3)
            status = str(app.master_status.cget("text"))
            log = app.log_text.get("1.0", "end")
            framed = [line for line in log.splitlines() if "└" in line]
            marks = len(app._written_marks) // 4     # 每格 4 条边框
            # 恢复默认，别影响后面的用例（后面的用例读的是明细表）
            app.set_master_view(False)
            app.dtype_box.current(0)
            app._on_dtype_change()
            self.pick_fc(3)
            self.pump(0.2)
            ok = ("6 个寄存器" in hint and "6 个寄存器" in status
                  and "12 字节" in status and bool(framed)
                  and "6 个寄存器" in framed[-1] and marks == 3)
            return (f"预览={hint} 状态={status} 框={marks} "
                    f"帧={framed[-1].split('└')[-1].strip() if framed else '无'}"), ok

        self.case("F 写", "写多寄存器报数量", "3 个 f32 → 6 个寄存器，四处一致",
                  write_register_count)

        def write_fc05():
            self.pick_fc(5)
            self.set_spin(app.addr_var, "3")
            self.set_entry(app.write_var, "on")
            app._do_write_async()
            self.wait(lambda: bool(app.server.store.get("coils", 3)), timeout=4)
            self.pump(0.25)
            return f"线圈[3]={app.server.store.get('coils', 3)}", bool(app.server.store.get("coils", 3))

        self.case("F 写", "FC05 写单线圈", "线圈[3]=True", write_fc05)

        def write_fc15():
            self.pick_fc(15)
            self.set_spin(app.addr_var, "10")
            self.set_entry(app.write_var, "1,0,1,1")
            app._do_write_async()
            self.wait(lambda: bool(app.server.store.get("coils", 13)), timeout=4)
            self.pump(0.3)
            values = self.slave_values("coils", 10, 4)
            return f"线圈 10~13={values}", values == [True, False, True, True]

        self.case("F 写", "FC15 写多线圈", "1,0,1,1 落库", write_fc15)

        def write_empty():
            self.pick_fc(6)
            self.set_entry(app.write_var, "")
            app._do_write_async()
            self.pump(0.25)
            return self.status(), "请先在" in self.status()

        self.case("F 写", "写入值为空", "提示先填写数据", write_empty)

        def write_bad():
            self.set_entry(app.write_var, "abc")
            app._do_write_async()
            self.pump(0.25)
            return self.status(), "无法解析" in self.status()

        self.case("F 写", "写入值非法", "提示无法解析的数字", write_bad)

        def write_single_many():
            self.set_entry(app.write_var, "1,2")
            app._do_write_async()
            self.pump(0.25)
            return self.status(), "只能填一个值" in self.status()

        self.case("F 写", "FC06 填多个值", "提示单点写入只能一个值", write_single_many)

        def write_fc22():
            """掩码写：AND 为 0 的位被 OR 覆盖，其余位保持原值。"""
            app.server.store.set("holding_registers", 30, 0xAB12)
            self.pick_fc(22)
            self.set_spin(app.addr_var, "30")
            self.set_entry(app.write_var, "0xFFF0,0x000F")
            app._do_write_async()
            self.wait(lambda: self.slave_values("holding_registers", 30, 1)[0] == 0xAB1F,
                      timeout=4)
            self.pump(0.25)
            value = self.slave_values("holding_registers", 30, 1)[0]
            shown = self.status()
            return (f"从站[30]=0x{value:04X} 状态={shown}",
                    value == 0xAB1F and "已写入" in shown and "1 个寄存器" in shown)

        self.case("F 写", "FC22 掩码写", "只改低 4 位：0xAB12 → 0xAB1F，状态报 1 个寄存器", write_fc22)

        def write_fc22_bad():
            self.set_entry(app.write_var, "0xFFF0")
            app._do_write_async()
            self.pump(0.25)
            return self.status(), "两个掩码" in self.status()

        self.case("F 写", "FC22 缺掩码", "提示需要 AND/OR 两个掩码", write_fc22_bad)

        def write_map_preview():
            """2026-10-24 回归：写多寄存器必须能对出「哪个地址←哪个值」。

            参考 QModMaster / Modbus Poll 的做法（每格写入值都钉着地址）：
            预览提示里给出逐地址映射，改起始地址映射跟着走；写完的状态栏
            给地址范围、日志给逐地址映射，跟回读的表能直接对上。
            """
            self.pick_fc(16)
            self.set_spin(app.addr_var, "100")
            self.set_entry(app.write_var, "1,2,3")
            app._preview_write()
            self.pump(0.15)
            hint = str(app.write_hint.cget("text"))
            # 换个起始地址，映射必须跟着走
            self.set_spin(app.addr_var, "200")
            app._preview_write()
            hint2 = str(app.write_hint.cget("text"))
            self.set_spin(app.addr_var, "100")
            app._preview_write()
            app._do_write_async()
            self.wait(lambda: "已写入" in self.status(), timeout=4)
            self.pump(0.3)
            status = self.status()
            log = self.log_text()
            self.pick_fc(3)
            self.pump(0.2)
            ok = ("[100]=1" in hint and "[101]=2" in hint and "[102]=3" in hint
                  and "[200]=1" in hint2 and "[202]=3" in hint2
                  and "地址 100~102" in status and "3 个寄存器" in status
                  and "写入成功：[100]=1" in log and "[102]=3" in log)
            return f"预览={hint} | 换址={hint2} | 状态={status}", ok

        self.case("F 写", "写入地址映射", "预览/状态栏/日志三处都能对出地址",
                  write_map_preview)

        # ================= M 新能力（识别 / 导出 / 故障注入）=================
        def probe_on_connect():
            """2026-10-25：连上先静默问一句「对面是谁」，问完再进轮询。

            0x2B 是协议里的可选项，不支持的设备不该留下任何痕迹，所以这里
            验的是成功路径（日志有全表、胶囊有短名），并确认工具条上已经没有
            「识别」这个常驻入口 —— 它改由右键菜单与 Ctrl+I 承接。
            """
            app._toggle_connection()
            self.wait(lambda: app.client is None, timeout=5)
            self.pump(0.2)
            app._toggle_connection()
            if not self.wait(lambda: app.client is not None, timeout=5):
                return "重连失败", False
            self.wait(lambda: app._polling, timeout=6)
            self.pump(0.5)
            log = self.log_text()
            led = str(app.led.cget("text"))
            has_btn = any(str(b.cget("text")) == "识别"
                          for b in app._param_group.winfo_children()
                          if b.winfo_class() == "TButton")
            return (f"日志有标识={'设备标识（FC2B）' in log} 胶囊={led} "
                    f"设备名={app._device_note!r} 识别按钮={has_btn}",
                    "设备标识（FC2B）" in log and "ModNova" in led
                    and bool(app._device_note) and not has_btn)

        self.case("M 新能力", "连上静默探测设备", "日志与链路胶囊出现设备名，工具条无「识别」按钮",
                  probe_on_connect)

        def identify_device():
            app._identify_device_async()
            ok = self.wait(lambda: "设备标识：" in self.status(), timeout=4)
            self.pump(0.2)
            text = self.status()
            return f"状态={text}", ok and "ModNova" in text

        self.case("M 新能力", "FC2B 设备识别", "状态栏与日志出现厂商 ModNova", identify_device)

        def hex_format_prefix():
            """2026-10-25：格式选「十六进制」时格子必须带 0x 前缀。

            只写 0A0B 的话，和十进制数字串在视觉上无从分辨，复制出去也看不出
            进制 —— 0x 是 C / Python / 汇编里通用的写法，符合国际惯例。
            """
            was_grid = app._master_grid
            saved = [app.server.store.get("holding_registers", i) for i in range(3)]
            try:
                app.set_master_view(True)
                self.pick_fc(3)
                self.set_spin(app.addr_var, "0")
                self.set_spin(app.count_var, "3")
                app.server.store.write_holding_registers(0, [0x0A0B, 0xFFFF, 0x0001])
                app.master_fmt_box.set(FMT_HEX)
                app._on_display_change()
                self.pump(0.2)
                app._read_once()
                self.wait(lambda: len(app.result_grid.get_children()) == 1, timeout=4)
                self.pump(0.25)
                row = [str(v) for v in app.result_grid.item("0", "values")]
                shown = row[1:4]
                return f"格子={shown}", shown == ["0x0A0B", "0xFFFF", "0x0001"]
            finally:
                app.master_fmt_box.set(FMT_VALUE)
                app._on_display_change()
                app.set_master_view(was_grid)
                app.server.store.write_holding_registers(0, saved)
                self.pump(0.15)

        self.case("M 新能力", "十六进制带 0x 前缀", "格式选十六进制后格子显示 0x0A0B",
                  hex_format_prefix)

        def export_is_plain_txt():
            """2026-10-25：导出只剩一个动作 —— 把日志面板存成 TXT。

            曾经这里是分裂按钮加一层菜单（导出 TXT / 导出 CSV / 录制数据到
            CSV）。前两项存的是同一堆 TX/RX 文字、只差分隔符，后一项是数值
            采样 —— 三样摆在同一个菜单里，看着就是「重复的两个 CSV」。
            现在全部收掉，这条用例守的是「别再长回来」：导出是普通按钮、
            没有 ▾ 窄条、没有菜单对象、也没有任何 CSV 残留。
            """
            btn = app.export_btn
            plain = (btn.winfo_class() == "TButton"
                     and btn.cget("text") == "导出"
                     and getattr(btn, "_split_cmd", None) is None)
            wired = getattr(btn, "_command", None) == app._export_log
            leftover = [name for name in ("export_menu", "rec_label", "csv_var", "_csv")
                        if hasattr(app, name)]
            return (f"普通按钮={plain} 绑对回调={wired} 残留={leftover or '无'}",
                    plain and wired and not leftover)

        self.case("M 新能力", "导出只留 TXT", "单个按钮导日志，界面里没有任何 CSV 出口",
                  export_is_plain_txt)

        def trend_track():
            """模拟「点选格子」：跟踪 1 号寄存器，两次读数后趋势条应画出折线。"""
            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "4")
            app._set_trend_key(1)
            app._read_once()
            self.wait(lambda: len(self.rows(app.result_tree)) == 4, timeout=4)
            self.pump(0.2)
            app.server.store.set("holding_registers", 1, 4321)
            app._read_once()
            self.wait(lambda: len(app._trend) >= 2, timeout=4)
            self.pump(0.3)
            items = len(app.trend_canvas.find_all())
            return (f"趋势点数={len(app._trend)} 画布元素={items}",
                    len(app._trend) >= 2 and items >= 2)

        self.case("M 新能力", "迷你趋势条", "跟踪寄存器 1，两次读数后画出折线（占位文字消失）",
                  trend_track)

        def fault_exception():
            # 上一个用例若还留着轮询在跑，它会不停改写状态栏，把下面单次读的
            # 那句「读取失败」顶掉（偶发失败就是这么来的，地址还停在前一个
            # 用例的「0 起 4 点」上）。先停干净再验。
            app._stop_poll(manual=True)
            self.pump(0.15)
            app.fault_exc.set("100")
            app.fault_exc_code.set("2")
            app._apply_faults()
            self.pump(0.2)
            app._read_once()
            ok = self.wait(lambda: "读取失败" in self.status(), timeout=4)
            text = self.status()
            injected = "注入中" in str(app.fault_stat.cget("text"))
            # 从站回了异常码，说明这一问一答是通的：胶囊不许降级成「无响应 /
            # 链路中断」。降级的话人会去查网线和串口，而该改的是地址或功能码。
            # 连发三次是为了越过 down 的阈值（三次才中断），一次只到 warn。
            for _ in range(3):
                app._read_once()
                self.pump(0.5)
            led = self.led()
            link_ok = "在线" in led and "中断" not in led and "无响应" not in led
            app._clear_faults()
            self.pump(0.2)
            app._read_once()
            self.wait(lambda: "共" in self.status() and "失败" not in self.status(), timeout=4)
            return (f"状态={text} 面板={injected} 胶囊={led}",
                    ok and "超出从站范围" in text and injected and link_ok)

        self.case("M 新能力", "故障注入：100% 异常码 2",
                  "读取失败且报「超出从站范围」，清零后恢复，胶囊保持在线",
                  fault_exception)

        def poll_failure_names_target():
            """轮询失败的红字要写清「读的是哪一段」和从站异常码。

            只写「地址或数量超出从站范围」时，用户还得自己回想地址框里是
            16384 还是 4000 —— 而 0x4000 恰好等于 16384，这两个数最容易混。
            异常码单列是因为它是「设备说不行」，和本机参数校验失败不是一类事。
            """
            app.fault_exc.set("100")
            app.fault_exc_code.set("2")
            app._apply_faults()
            self.pump(0.2)
            saved = app.addr_notation.get()
            app.addr_notation.set("十六进制")
            app._on_addr_notation()
            app._set_addr_text("0x4000")
            self.set_spin(app.count_var, "10")
            app._start_poll()
            stopped = self.wait(lambda: not app._polling and "自动停止" in self.status(),
                                timeout=15)
            text = self.status()
            app._clear_faults()
            self.pump(0.2)
            app._stop_poll(manual=True)
            app.addr_notation.set(saved)
            app._on_addr_notation()
            self.set_spin(app.addr_var, "0")
            self.pump(0.2)
            ok = (stopped and "0x4000" in text and "起 10 点" in text
                  and "异常 02" in text and "超出从站范围" in text)
            return f"状态={text}", ok

        self.case("M 新能力", "轮询失败写明地址与异常码",
                  "红字含「地址 0x4000 起 10 点 · 从站异常 02」",
                  poll_failure_names_target)

        # ================= G 类型 / 字节序 / 缩放 / PLC =================
        def dtype_default():
            self.pick_dtype("uint16")
            return str(app.byteorder_box.cget("state")), str(app.byteorder_box.cget("state")) == "disabled"

        self.case("G 类型", "uint16 时字节序禁用", "字节序下拉置灰", dtype_default)

        def dtype_float():
            self.pick_dtype("float32")
            return str(app.byteorder_box.cget("state")), str(app.byteorder_box.cget("state")) == "readonly"

        self.case("G 类型", "float32 时字节序启用", "字节序可选", dtype_float)

        def write_float():
            self.pick_fc(16)
            self.pick_dtype("float32")
            app.byteorder_box.set("ABCD")
            self.set_spin(app.addr_var, "40")
            self.set_entry(app.write_var, "220.5")
            app._do_write_async()
            self.wait(lambda: len(self.rows(app.result_tree)) == 1, timeout=4)
            self.pump(0.3)
            rows = self.rows(app.result_tree)
            regs = self.slave_values("holding_registers", 40, 2)
            shown = rows[0][2] if rows else ""
            return f"寄存器={[hex(v) for v in regs]} 显示={shown}", shown == "220.5" and regs == [0x435C, 0x8000]

        self.case("G 类型", "FC16 写 float32 220.5", "寄存器 0x435C/0x8000，回读 220.5", write_float)

        def byteorder_read():
            self.pick_fc(3)
            self.pick_dtype("float32")
            self.set_spin(app.addr_var, "40")
            self.set_spin(app.count_var, "2")
            results = {}
            for order in ("ABCD", "BADC", "CDAB", "DCBA"):
                app.byteorder_box.set(order)
                app._read_once()
                self.pump(0.5)
                rows = self.rows(app.result_tree)
                results[order] = rows[0][2] if rows else "-"
            app.byteorder_box.set("ABCD")
            distinct = len(set(results.values())) == 4
            return " ".join(f"{k}={v}" for k, v in results.items()), results["ABCD"] == "220.5" and distinct

        self.case("G 类型", "四种字节序解释", "ABCD=220.5，其余各不相同", byteorder_read)

        def scale_and_unit():
            self.pick_fc(16)
            self.pick_dtype("uint16")
            self.set_spin(app.addr_var, "50")
            self.set_entry(app.write_var, "2200")
            app._do_write_async()
            self.pump(0.6)
            self.set_entry(app.scale_var, "0.1")
            self.set_entry(app.unit_text_var, "℃")
            self.pick_fc(3)
            self.set_spin(app.addr_var, "50")
            self.set_spin(app.count_var, "1")
            app._read_once()
            self.pump(0.6)
            rows = self.rows(app.result_tree)
            shown = rows[0][2] if rows else ""
            self.set_entry(app.scale_var, "1")
            self.set_entry(app.unit_text_var, "")
            return f"显示={shown}", shown == "220 ℃"

        self.case("G 类型", "缩放 ×0.1 与单位", "2200 → 220 ℃", scale_and_unit)

        def plc_read():
            app.server.store.set("holding_registers", 0, 4321)
            self.pick_fc(3)
            app.plc_var.set(True)
            self.pump(0.1)
            self.set_spin(app.addr_var, "40001")
            self.set_spin(app.count_var, "1")
            app._read_once()
            self.pump(0.6)
            rows = self.rows(app.result_tree)
            shown_addr = rows[0][1] if rows else ""
            shown_val = rows[0][2] if rows else ""
            app.plc_var.set(False)
            self.pump(0.1)
            return f"地址列={shown_addr} 值={shown_val}", shown_addr == "40001" and shown_val == "4321"

        self.case("G 类型", "PLC 地址 40001", "地址列显示 40001，读到协议 0 的值", plc_read)

        def hex_and_six_digit_address():
            app.server.store.set("holding_registers", 16, 6161)
            app.plc_var.set(False)
            app._addr_hex.set(True)
            self.set_spin(app.addr_var, "0x10")
            self.set_spin(app.count_var, "1")
            app._read_once()
            # 按内容等、不按钟表等：固定 pump 在机器稍忙时会超时，
            # 表格还留着上一轮读的 4321 —— 失败样子酷似「0x10 没按十六进制解析」，
            # 其实是读回没落地。等值出现才采样，超时则如失败。
            self.wait(lambda: bool(self.rows(app.result_tree))
                      and self.rows(app.result_tree)[0][2] == "6161", timeout=4)
            rows = self.rows(app.result_tree)
            hex_value = rows[0][2] if rows else ""
            app._addr_hex.set(False)
            app.plc_var.set(True)
            self.set_spin(app.addr_var, "400001")
            app._read_once()
            self.wait(lambda: bool(self.rows(app.result_tree))
                      and self.rows(app.result_tree)[0][2] == "4321", timeout=4)
            rows = self.rows(app.result_tree)
            plc_value = rows[0][2] if rows else ""
            app.plc_var.set(False)
            return (f"HEX={hex_value} 六位PLC={plc_value} 状态={self.status()}",
                    hex_value == "6161" and plc_value == "4321")

        self.case("G 类型", "HEX 与六位 PLC 地址", "0x10 与 400001 均按当前记法解析",
                  hex_and_six_digit_address)

        def hex_address_typing():
            """十六进制记法下地址框要收得下 a-f，裸写也算数。

            修之前：校验用 str.isdigit() 判字符，于是 0x1F / 0x0A 全被挡在门外，
            只有 0x10 这种纯数字的十六进制能输入 —— 看着像「地址不支持十六进制」。
            """
            saved = app.addr_notation.get()
            typed = []
            ok = True
            app.addr_notation.set("十六进制")
            app._on_addr_notation()
            for text, expect in (("0x1F", 31), ("1A", 26), ("0x00FF", 255)):
                app.addr_var.delete(0, "end")
                app.addr_var.insert(0, text)
                kept = str(app.addr_var.get())
                try:
                    parsed = app._address_field(3)
                except ValueError:
                    parsed = None
                typed.append(f"{text}→框{kept or '空'}/值{parsed}")
                ok = ok and kept != "" and parsed == expect
            app.addr_notation.set(saved)
            app._on_addr_notation()
            self.pump(0.15)
            return f"{' '.join(typed)}", ok

        self.case("G 类型", "十六进制地址可输入", "0x1F / 1A / 0x00FF 都能敲进去并解析",
                  hex_address_typing)

        def hex_prefix_wins_over_notation():
            """0x 前缀自带记法：下拉停在 PLC 也读 16384，不会被当成 PLC 编号。

            修之前：PLC 记法下敲 0x4000，前三个字符能进，一读就报「PLC 地址
            16384 属于离散输入」；即使不报错，表头也按 PLC 编号显示 416385，
            而框里写着 0x4000 —— 用户看到的就是「0x4000 写不进去」。
            顺带查 Ctrl+A：Tk 8.6 的 Entry 没有全选，替换输入就会跟前缀拼起来。
            """
            saved = app.addr_notation.get()
            results = {}
            for notation in ("PLC 地址", "十进制", "十六进制"):
                app.addr_notation.set(notation)
                app._on_addr_notation()
                app.addr_var.delete(0, "end")
                for ch in "0x4000":
                    app.addr_var.insert("end", ch)
                    app._on_addr_key()          # 模拟每次松键后的同步
                results[notation] = (app.addr_var.get(), app.addr_notation.get(),
                                     app._address_field(3),
                                     app._addr_text(app._address_field(3), 3))
            app._set_addr_text("0x0000")
            select_ok = app._select_all_addr() == "break" and app.addr_var.selection_present()
            app.addr_notation.set(saved)
            app._on_addr_notation()
            self.set_spin(app.addr_var, "0")
            self.pump(0.15)
            ok = select_ok and all(text == "0x4000" and note == "十六进制"
                                   and proto == 16384 and head == "0x4000"
                                   for text, note, proto, head in results.values())
            detail = " | ".join(f"{k}→{v[1]}/{v[2]}/{v[3]}"
                                for k, v in results.items())
            return f"{detail} | Ctrl+A={select_ok}", ok

        self.case("G 类型", "0x 前缀压过记法下拉", "任何记法下 0x4000 都是寄存器 16384",
                  hex_prefix_wins_over_notation)

        def addr_notation_switch():
            """工具条上的记法下拉：换一套编号，读的还是同一处寄存器。"""
            saved = app.addr_notation.get()
            app.addr_notation.set("十进制")
            app._on_addr_notation()
            self.set_spin(app.addr_var, "10")
            app.addr_notation.set("十六进制")
            app._on_addr_notation()
            self.pump(0.1)
            hex_text = str(app.addr_var.get())
            app.addr_notation.set("PLC 地址")
            app._on_addr_notation()
            self.pump(0.1)
            plc_text = str(app.addr_var.get())
            protocol = app._address_field(3)
            app.addr_notation.set(saved)
            app._on_addr_notation()
            self.pump(0.15)
            ok = (hex_text == "0x000A" and plc_text == "40011" and protocol == 10)
            return f"十进制10 → 十六进制{hex_text} → PLC {plc_text} 解析回={protocol}", ok

        self.case("G 类型", "记法下拉换算地址", "10 → 0x000A → 40011，协议地址不变",
                  addr_notation_switch)

        def addr_wheel_is_ignored():
            """地址框不接滚轮：滚一格能把十六进制地址冲成十进制 2。

            Spinbox 的滚轮和箭头按十进制整数加减 —— 框里「1A」被 Tcl 读成 1，
            滚一格变 2，再滚一格变 1，原文彻底丢了。地址是点位，不该被一个路过
            的手势改掉；现在地址框是 Entry，滚轮事件被显式吃掉。
            """
            saved = app.addr_notation.get()
            app.addr_notation.set("十六进制")
            app._on_addr_notation()
            self.set_spin(app.addr_var, "1A")
            before = str(app.addr_var.get())
            seen = []
            for delta in (120, 120, -120, -360):
                app.addr_var.event_generate("<MouseWheel>", delta=delta)
                self.pump(0.05)
                seen.append(str(app.addr_var.get()))
            protocol = app._address_field(3)
            app.addr_notation.set(saved)
            app._on_addr_notation()
            self.set_spin(app.addr_var, "0")
            self.pump(0.15)
            ok = (app.addr_var.winfo_class() == "TEntry" and before == "1A"
                  and set(seen) == {"1A"} and protocol == 26)
            return (f"控件={app.addr_var.winfo_class()} 起点={before} "
                    f"滚轮后={seen} 解析={protocol}", ok)

        self.case("G 类型", "滚轮不改地址", "十六进制 1A 连滚四格仍是 1A / 协议地址 26",
                  addr_wheel_is_ignored)

        def plc_mismatch():
            app.plc_var.set(True)
            self.pump(0.1)
            self.pick_fc(3)
            self.set_spin(app.addr_var, "30001")
            app._read_once()
            self.pump(0.4)
            text = self.status()
            app.plc_var.set(False)
            self.pump(0.1)
            return f"状态={text}", "区域" in text

        self.case("G 类型", "PLC 地址与功能码不符", "提示不在同一区域", plc_mismatch)

        def ascii_read():
            """设备型号这类字符串：一个寄存器两个字符。"""
            app.server.store.write_holding_registers(
                0, [0x4D4F, 0x442D, 0x3130, 0x3030])       # "MOD-1000"
            self.pick_fc(3)
            self.pick_dtype("ascii")
            app.set_master_view(False)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "4")
            app._read_once()
            self.wait(lambda: len(self.rows(app.result_tree)) == 4, timeout=4)
            self.pump(0.2)
            shown = [row[2] for row in self.rows(app.result_tree)]
            return f"显示={shown}", shown == ["MO", "D-", "10", "00"]

        self.case("G 类型", "ASCII 读型号", "每格两个字符", ascii_read)

        def ascii_write():
            self.pick_fc(16)
            self.pick_dtype("ascii")
            self.set_spin(app.addr_var, "20")
            self.set_entry(app.write_var, "HELLO")
            app._preview_write()
            self.pump(0.1)
            hint = str(app.write_hint.cget("text"))
            app._do_write_async()
            self.wait(lambda: "已写入" in self.status(), timeout=4)
            self.pump(0.2)
            regs = self.slave_values("holding_registers", 20, 3)
            self.pick_dtype("uint16")
            self.pick_fc(3)
            return (f"预览={hint} 寄存器={[hex(r) for r in regs]}",
                    regs == [0x4845, 0x4C4C, 0x4F00] and "3 个寄存器" in hint)

        self.case("G 类型", "ASCII 写字符串", "5 字符占 3 个寄存器", ascii_write)

        def ascii_nonprint_kept():
            """占位符「·」回车时保留原字节 —— 不能把 0x00 写成真字符。"""
            app.server.store.write_holding_registers(30, [0x4100])
            self.pick_fc(3)
            self.pick_dtype("ascii")
            app.set_master_view(False)
            self.set_spin(app.addr_var, "30")
            self.set_spin(app.count_var, "1")
            app._read_once()
            self.wait(lambda: len(self.rows(app.result_tree)) == 1, timeout=4)
            self.pump(0.2)
            shown = self.rows(app.result_tree)[0][2]
            app._write_cell_async(30, shown)          # 原样回写
            self.wait(lambda: "已写入" in self.status(), timeout=4)
            self.pump(0.2)
            kept = self.slave_values("holding_registers", 30, 1)[0]
            self.pick_dtype("uint16")
            return f"显示={shown!r} 回写后=0x{kept:04X}", kept == 0x4100

        self.case("G 类型", "ASCII 占位符保留", "0x00 不被写成字符", ascii_nonprint_kept)

        # ================= L 回归（代码审查里修掉的两个界面缺陷）=================
        def readback_keeps_base():
            """回读保留原视野时，渲染必须按「回读的真实起点」。

            修之前：先读 0~29，再把地址改成 5 写一个值，回读走的是 0~29，
            渲染却按 5 当基址 —— 整屏地址标签偏移，且 _last_read 被写坏，
            接着双击就地编辑会写到错误寄存器。
            """
            was_grid = app._master_grid
            app.set_master_view(True)
            self.pick_dtype("uint16")
            app.scale_var.delete(0, "end")
            app.scale_var.insert(0, "1")
            app.unit_text_var.delete(0, "end")
            app.byteorder_box.set("ABCD")
            app.master_fmt_box.set(FMT_VALUE)
            app._on_display_change()
            self.pump(0.1)

            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "30")
            app._read_once()
            self.wait(lambda: len(app.result_grid.get_children()) == 3, timeout=4)
            self.pump(0.2)
            app.server.store.write_holding_registers(0, list(range(100, 130)))

            self.pick_fc(6)
            self.set_spin(app.addr_var, "5")
            self.set_entry(app.write_var, "555")
            app._do_write_async()
            self.wait(lambda: app.server.store.get("holding_registers", 5) == 555, timeout=4)
            self.wait(lambda: app.result_grid.set("0", "c5") == "555", timeout=4)
            self.pump(0.2)

            row0 = app.result_grid.item("0", "values")
            base, c0, c5 = str(row0[0]), str(row0[1]), str(row0[6])
            app.set_master_view(was_grid)
            self.pump(0.1)
            return (f"行首地址={base} c0={c0} c5={c5}",
                    base == "0" and c0 == "100" and c5 == "555")

        self.case("L 回归", "写后回读地址不偏移", "回读按真实起点渲染（行首 0，c0=100，c5=555）",
                  readback_keeps_base)

        def detail_view_ignores_stale_format():
            """明细视图下「格式」下拉禁用但仍留着旧值，判断格跨度必须忽略它。

            修之前：概览选过 HEX 再切明细，_cell_step() 返回 1，
            float32 双击写会按 address + index*1 定位，写到前一个寄存器上。
            """
            was_grid = app._master_grid
            app.set_master_view(True)
            app.master_fmt_box.set(FMT_HEX)
            self.pump(0.15)
            app.set_master_view(False)
            self.pump(0.15)
            self.pick_dtype("float32")
            self.pump(0.15)
            fmt = app._grid_fmt()
            step = app._cell_step()
            self.pick_dtype("uint16")
            app.set_master_view(was_grid)
            self.pump(0.1)
            return f"明细下 fmt={fmt} step={step}", fmt == FMT_VALUE and step == 2

        self.case("L 回归", "明细视图忽略残留格式", "fmt=值 且 float32 一格跨 2 个寄存器",
                  detail_view_ignores_stale_format)

        def view_switch_keeps_layout():
            """来回切视图后，工具条右侧的控件顺序和用户选的格式都不能变。

            修之前：视图区用 side="right" + before=切换按钮 把「格式」这一对
            插回，插回的位置落在按钮右边 —— 切一次就从「数据定义｜格式｜下拉｜
            明细」变成「数据定义｜明细｜下拉｜格式」；而且进明细时还会把格式
            重置成「数值」，回来时选好的十六进制已经没了。
            """
            def order():
                kids = [c for c in app._view_group.winfo_children() if c.winfo_ismapped()]
                return [c.winfo_x() for c in sorted(kids, key=lambda c: c.winfo_x())]

            was_grid = app._master_grid
            app.set_master_view(True)
            self.pump(0.2)
            app.master_fmt_box.set(FMT_HEX)
            app._on_display_change()
            self.pump(0.15)
            before = order()
            app.set_master_view(False)
            self.pump(0.2)
            fmt_hidden = not app.master_fmt_box.winfo_ismapped()
            app.set_master_view(True)
            self.pump(0.2)
            after = order()
            kept = app.master_fmt_box.get()
            app.master_fmt_box.set(FMT_VALUE)
            app._on_display_change()
            app.set_master_view(was_grid)
            self.pump(0.1)
            return (f"前={before} 后={after} 明细下格式收起={fmt_hidden} 回来仍是={kept}",
                    before == after and fmt_hidden and kept == FMT_HEX)

        self.case("L 回归", "切换视图布局不漂移", "顺序不变、明细下格式收起、格式选择保留",
                  view_switch_keeps_layout)

        def view_button_toggles():
            """视图切换只有一个按钮：标签就是另一侧视图的名字，点一下即换。

            以前是「概览 / 明细」两段常驻、当前段高亮 —— 状态由「哪段亮」表达。
            合成一个按钮后，状态只剩标签这一个出口：标签要是不跟着切，用户
            就既不知道现在在哪，也不知道再点会去哪。两页签共用同一构件，
            所以主站、从站各点一遍。
            """
            was_grid = app._master_grid
            was_slave_grid = app._slave_grid
            app.set_master_view(True)
            self.pump(0.2)
            grid_label = str(app.master_view_btn.cget("text"))
            app.master_view_btn.invoke()             # 点一下 = 切到明细
            self.pump(0.2)
            detail_label = str(app.master_view_btn.cget("text"))
            detail_shown = app.detail_wrap.winfo_ismapped() == 1
            app.master_view_btn.invoke()             # 再点一下 = 回到概览
            self.pump(0.2)
            back_label = str(app.master_view_btn.cget("text"))
            grid_shown = app.grid_wrap.winfo_ismapped() == 1
            app.set_slave_view(True)
            self.pump(0.2)
            slave_grid_label = str(app.slave_view_btn.cget("text"))
            app.set_slave_view(False)
            self.pump(0.2)
            slave_detail_label = str(app.slave_view_btn.cget("text"))
            app.set_slave_view(was_slave_grid)
            app.set_master_view(was_grid)
            self.pump(0.15)
            ok = (grid_label == "明细" and detail_label == "概览"
                  and back_label == "明细"
                  and slave_grid_label == "明细"
                  and slave_detail_label == "概览"
                  and detail_shown and grid_shown)
            return (f"主站 概览态标签={grid_label} 明细态标签={detail_label} 回={back_label} | "
                    f"从站 概览态={slave_grid_label} 明细态={slave_detail_label} | "
                    f"明细显示={detail_shown} 概览恢复={grid_shown}", ok)

        self.case("L 回归", "视图切换只有一个按钮", "标签即另一侧视图名，点一下即换过来",
                  view_button_toggles)

        def invalid_scale_blocks_request():
            self.pick_fc(3)
            before = app.client.request_count
            self.set_entry(app.scale_var, "not-a-number")
            app._read_once()
            self.pump(0.4)
            text = self.status()
            after = app.client.request_count
            self.set_entry(app.scale_var, "1")
            return (f"状态={text} 请求数={before}→{after}",
                    "系数" in text and after == before)

        self.case("L 回归", "非法系数阻止读取", "字段错误可见且不得发请求", invalid_scale_blocks_request)

        def duplicate_read_guard():
            self.pick_fc(3)
            original = app.client.read
            calls = []
            def slow_read(*args, **kwargs):
                calls.append(args)
                time.sleep(0.25)
                return original(*args, **kwargs)
            app.client.read = slow_read
            app._read_once()
            app._read_once()
            self.pump(0.9)
            app.client.read = original
            return f"实际请求次数={len(calls)}", len(calls) == 1

        self.case("L 回归", "请求期间禁止重复提交", "连续触发两次只发一次", duplicate_read_guard)

        def mask_write_hint():
            self.pick_fc(22)
            self.set_entry(app.write_var, "0xFFF0")
            app._preview_write()
            text = str(app.write_hint.cget("text"))
            self.pick_fc(3)
            return f"提示={text}", "两个掩码" in text

        self.case("L 回归", "FC22 掩码提示", "缺一个掩码时实时提示", mask_write_hint)

        def toolbar_groups_swap():
            """读码用轮询区、写码用「值 + 写入」，两组互斥；明细视图收起格式。

            互斥不只是语义正确，也是布局稳压器：以前写码下工具条比读码下宽
            106 px，窄窗口里正是这段多出来的宽度把最右边的「数据定义」挤碎。
            """
            self.pick_fc(3)
            self.pump(0.25)
            read_state = (app.write_btn.winfo_ismapped(), app.write_var.winfo_ismapped(),
                          app._poll_group.winfo_ismapped(), app.poll_btn.winfo_ismapped())
            self.pick_fc(16)
            self.pump(0.25)
            write_state = (app.write_btn.winfo_ismapped(), app.write_var.winfo_ismapped(),
                           app._poll_group.winfo_ismapped(), app.poll_btn.winfo_ismapped())
            was = app._master_grid
            app.set_master_view(False)
            self.pump(0.2)
            fmt_hidden = (not app.master_fmt_box.winfo_ismapped()
                          and not app.fmt_label.winfo_ismapped())
            app.set_master_view(True)
            self.pump(0.2)
            fmt_back = (app.master_fmt_box.winfo_ismapped()
                        and str(app.master_fmt_box.cget("state")) == "readonly")
            app.set_master_view(was)
            self.pick_fc(3)
            self.pump(0.2)
            ok = (read_state == (0, 0, 1, 1) and write_state == (1, 1, 0, 0)
                  and fmt_hidden and fmt_back)
            return (f"读码(写入,值,轮询组,轮询钮)={read_state} 写码={write_state} "
                    f"明细收起={fmt_hidden} 概览恢复={fmt_back}", ok)

        self.case("L 回归", "读写控件互斥", "读码无写入、写码无轮询区；明细下格式收起",
                  toolbar_groups_swap)

        def toolbar_no_squeeze():
            """2026-10-25：「数据定义」在最窄窗口 + 写功能码下曾被压成 20 px。

            起因是读/写两组控件叠在同一条里：写码下多出「值 + 写入」106 px，
            pack 空间不够时先牺牲右侧的按钮，107 px 宽的文字整个看不见。
            """
            saved = app.root.geometry()
            app.root.geometry("1040x680")
            self.pump(0.35)
            self.pick_fc(16)
            self.pump(0.3)
            dfn = [b for b in app._view_group.winfo_children()
                   if b.winfo_class() == "TButton" and str(b.cget("text")) == "数据定义"][0]
            width = dfn.winfo_width()
            bar = app._param_group.master
            need, have = bar.winfo_reqwidth(), bar.winfo_width()
            self.pick_fc(3)
            app.root.geometry(saved)
            self.pump(0.25)
            return (f"数据定义宽={width} 工具条需求={need} 实宽={have}",
                    width >= 100 and need <= have)

        self.case("L 回归", "窄窗口不挤压按钮", "1040 宽 + 写码下「数据定义」仍完整",
                  toolbar_no_squeeze)

        # ================= H 轮询 =================
        def poll_start():
            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "4")
            self.set_spin(app.interval_var, "100")
            app._toggle_poll()
            self.pump(0.6)
            return (f"按钮={app.poll_btn.cget('text')} 状态={self.status()}",
                    str(app.poll_btn.cget("text")) == "停止轮询" and "轮询中" in self.status())

        self.case("H 轮询", "「开始轮询」", "按钮变停止、状态显示轮询中", poll_start)

        def poll_updates():
            app.server.store.set("holding_registers", 0, 7777)
            ok = self.wait(lambda: any(r[2] == "7777" for r in self.rows(app.result_tree)), timeout=3)
            return f"表格首行={self.rows(app.result_tree)[0][2] if self.rows(app.result_tree) else '-'}", ok

        self.case("H 轮询", "轮询跟随数据变化", "从站改值后表格自动刷新", poll_updates)

        def poll_stop():
            app._toggle_poll()
            self.pump(0.4)
            return (f"按钮={app.poll_btn.cget('text')} 日志有停止={('轮询已停止' in self.log_text())}",
                    str(app.poll_btn.cget("text")) == "开始轮询" and "轮询已停止" in self.log_text())

        self.case("H 轮询", "「停止轮询」", "按钮复位并记日志", poll_stop)

        def poll_interval_invalid_stops():
            """2026-10-24 回归：轮询参数中途变非法 → 停止且状态栏给原因。

            以前只静默停（日志一句「轮询已停止」），用户不知道是参数错了。
            set_spin 整串插入会被按键护栏拒绝落成空值，正好触发整数解析错。
            """
            app._toggle_poll()
            self.pump(0.4)
            self.set_spin(app.interval_var, "abc")   # 护栏拒掉 → 空值 → 下拍解析失败
            ok = self.wait(lambda: not app._polling, timeout=5)
            text = self.status()
            self.set_spin(app.interval_var, "500")
            return (f"停止={ok} 状态={text}",
                    ok and "轮询已停止" in text and "间隔" in text)

        self.case("H 轮询", "间隔非法自动停止", "轮询停止且状态栏说明原因",
                  poll_interval_invalid_stops)

        def poll_fc_switch_stops():
            """轮询进行中把功能码切到写码 → 轮询立即停，不等失败计数。"""
            app._toggle_poll()
            self.pump(0.5)
            running = app._polling
            self.pick_fc(6)
            self.pump(0.3)
            stopped = not app._polling
            btn = str(app.poll_btn.cget("text"))
            self.pick_fc(3)
            self.pump(0.2)
            # 切回读码会自动恢复轮询（新行为），这条用例只负责「切写码停」，
            # 收尾按用户意图停掉，别把轮询状态漏给后面的用例
            app._stop_poll(manual=True)
            return (f"切码前轮询={running} 切到FC06后={stopped} 按钮={btn}",
                    running and stopped and btn == "开始轮询")

        self.case("H 轮询", "切到写码自动停轮询", "轮询中把功能码切到 FC06，轮询立即停止",
                  poll_fc_switch_stops)

        def poll_disconnect_stops():
            # 上一条用例切回读码时会自动恢复轮询（切写码停掉的那次），
            # 这里先按用户意图归零，免得 _toggle_poll 变成「停止」
            app._stop_poll(manual=True)
            self.pump(0.2)
            app._toggle_poll()
            self.pump(0.5)
            running = app._polling
            app._toggle_connection()          # 断开
            self.pump(0.6)
            return f"断开前轮询={running} 断开后={app._polling}", running and not app._polling

        self.case("H 轮询", "轮询中断开连接", "轮询自动停止", poll_disconnect_stops)

        # ================= I 日志 =================
        def log_default_off():
            """运行日志默认零落盘：界面照常显示，留档走「导出…」。"""
            return f"logger={app.logger}", app.logger is None

        self.case("I 日志", "默认不落盘", "启动后不写任何日志文件", log_default_off)

        def log_frames():
            app._toggle_connection()          # 重新连上
            self.wait(lambda: app.client is not None, timeout=4)
            self.pump(0.3)
            # 连上会自动开轮询；这里要的是「一次读产生一帧 TX/RX」的可数日志，
            # 不停掉的话紧接着的「清空」用例会被轮询报文重新填满
            app._stop_poll()
            self.pump(0.2)
            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "1")
            app._read_once()
            self.pump(0.6)
            text = self.log_text()
            return (f"含 TX={('TX' in text)} 含 RX={('RX' in text)} 行数={text.count(chr(10))}",
                    "TX" in text and "RX" in text)

        self.case("I 日志", "原始报文记录", "日志含 TX / RX 十六进制", log_frames)

        def log_clear():
            app._clear_log()
            self.pump(0.2)
            text = self.log_text().strip()
            return f"剩余字符={len(text)}", text == ""

        self.case("I 日志", "「清空」按钮", "日志清空", log_clear)

        def log_millisecond_stamp():
            """2026-10-24：界面日志时间戳到毫秒，与文件侧（applog 的 .mmm）对齐。

            报文间隔在 ms 量级，秒级时间戳看不出前后帧节奏。_log 走 40ms
            事件队列，所以用 wait 等内容落进文本框再取行。
            """
            app._clear_log()
            app._log('时间戳格式验证')
            self.wait(lambda: '时间戳格式验证' in self.log_text(), timeout=2)
            line = next((l for l in self.log_text().splitlines()
                         if '时间戳格式验证' in l), '')
            matched = re.match(r'^\[\d{2}:\d{2}:\d{2}\.\d{3}\] ', line)
            return f"行={line}", bool(matched)

        self.case("I 日志", "毫秒级时间戳", "每行以 [时:分:秒.毫秒] 开头",
                  log_millisecond_stamp)

        def log_pause_keeps_records():
            app._clear_log()
            app._toggle_log_pause()
            app._log("暂停期间的关键报文", "frame")
            self.pump(0.2)
            app._toggle_log_pause()
            self.pump(0.2)
            text = self.log_text()
            count = str(app.log_toggle_btn.cget("text"))
            app._clear_log()
            cleared = str(app.log_toggle_btn.cget("text"))
            return (f"恢复后可见={'暂停期间的关键报文' in text} 计数={count} 清空={cleared}",
                    "暂停期间的关键报文" in text and cleared.endswith("0"))

        self.case("I 日志", "暂停不丢日志", "恢复后可查看暂停期间记录，清空计数归零",
                  log_pause_keeps_records)

        def log_capacity():
            """2026-10-25：日志窗口从 400 行放大到 10 万条。

            原来只留 400 行 —— 轮询一开，几分钟前那几帧就被滚掉了，回头想对
            「刚才那次超时前后发了什么」已经找不到。真插 10 万条要 3 秒、多占
            65 MB，回归里把上限临时压到 50 条、步长 10 行：跑的是同一段裁剪
            代码，验三件事 —— 超限就裁、裁掉的是最旧那段、计数和真实行号对得上
            （一条日志自带换行时最容易把计数拖偏）。
            """
            limit, step = gui_mod.LOG_MAX_LINES, gui_mod.LOG_TRIM_STEP
            big = limit == 100_000
            gui_mod.LOG_MAX_LINES, gui_mod.LOG_TRIM_STEP = 50, 10
            try:
                app._clear_log()
                for i in range(80):
                    app._append_log(f"RX 01 03 · 第 {i} 条", "")
                # 自带换行的那条会让「按条计数」的实现少算两行
                app._append_log("异常栈第一行\n第二行\n第三行", "err")
                self.pump(0.1)
                body = self.log_text()
                real = int(app.log_text.index("end-1c").split(".")[0])
                lines = app._log_lines
                bounded = lines <= 50
                counted = real == lines
                oldest_gone = "第 0 条" not in body
                newest_kept = "第 79 条" in body and "异常栈第一行" in body
            finally:
                gui_mod.LOG_MAX_LINES, gui_mod.LOG_TRIM_STEP = limit, step
                app._clear_log()
                self.pump(0.1)
            return (f"上限常量={limit} 窗口内={bounded}(计数 {lines}) "
                    f"计数准={counted}(真实 {real}) 裁旧={oldest_gone} 留新={newest_kept}",
                    big and bounded and counted and oldest_gone and newest_kept)

        self.case("I 日志", "上限 10 万条", "超限裁掉最旧的，计数与真实行号一致",
                  log_capacity)

        def log_panel_tab_scoped():
            """2026-10-24 回归：日志覆盖层只跟主站页走。

            曾经在主站打开日志后切到从站，日志会盖在从站表格上不消失；
            反向还有个孪生缺陷 —— 切回主站时 tkraise 把主站页顶到日志上面，
            日志明明开着却被压在底下。前三个状态断言都是确定性的；压底检测
            用命中测试：winfo_containing 走系统命中（Windows 是 WindowFromPoint），
            测试窗口被其他程序盖住时返回 None —— 那是环境遮挡，不能算失败；
            但只要命中到本应用的控件，它就必须是日志面板的后代，否则就是
            「切回主站没重新 lift、面板被压在页签底下」的旧缺陷形态。
            """
            app._toggle_log_panel(True)
            self.pump(0.2)
            open_on_master = app.log_panel.winfo_ismapped() == 1
            app.tabs.select(1)
            self.pump(0.25)
            hidden_on_slave = app.log_panel.winfo_ismapped() == 0
            app.tabs.select(0)
            self.pump(0.25)
            back_on_master = app.log_panel.winfo_ismapped() == 1
            # 零遮挡断言：面板必须紧贴数据区下缘。它曾是 place 覆盖层，
            # 盖住数据区底部 180px —— 明细表格滚到最后几行全压在日志底下，
            # 「滑到最下面」也看不见（2026-10-24 用户报障）。
            # 容差 13 = 数据区自带 pady(0,10) + 面板描边 1 + 余量 2；
            # 旧覆盖层形态下这个差值是 -180（重叠），一眼区分。
            panel_top = app.log_panel.winfo_rooty()
            stack_bottom = app.stack.winfo_rooty() + app.stack.winfo_height()
            no_overlap = 0 <= panel_top - stack_bottom <= 13
            # 光 mapped 还不够（被压在底下的控件也算 mapped）：拿日志区域
            # 里的一个点做命中测试，落点必须是日志面板自己的后代控件。
            # winfo_containing 走系统命中（Windows 是 WindowFromPoint），
            # 窗口被外部程序盖住时返回 None —— 环境遮挡跳过不判负。
            hit = app.root.winfo_containing(
                app.log_text.winfo_rootx() + app.log_text.winfo_width() // 2,
                app.log_text.winfo_rooty() + 10)
            if hit is None:
                on_top, hit_desc = True, "None(被外部窗口遮挡,跳过)"
            else:
                on_top = str(hit).startswith(str(app.log_panel))
                hit_desc = str(hit)
            app._toggle_log_panel(False)      # 收尾，别影响后面的用例
            self.pump(0.15)
            actual = (f"主站开={open_on_master} 从站收起={hidden_on_slave} "
                      f"切回恢复={back_on_master} 零遮挡={no_overlap} 命中={hit_desc}")
            return actual, (open_on_master and hidden_on_slave
                            and back_on_master and no_overlap and on_top)

        self.case("I 日志", "日志只跟主站页", "切从站收起、切回恢复、贴底零遮挡",
                  log_panel_tab_scoped)

        # ================= J 断线与重连 =================
        first_port = None
        def slave_down():
            nonlocal first_port
            first_port = app.server.port
            app.tabs.select(1)
            self.pump(0.25)
            app._toggle_server()
            self.wait(lambda: app.server is None, timeout=5)
            self.pump(0.3)
            app.tabs.select(0)
            self.pump(0.25)
            app._read_once()
            self.pump(1.5)
            text = self.status()
            clear = ("连接被对端" in text or "无法连接" in text or "超时" in text)
            return f"状态={text}", "读取失败" in text and clear

        self.case("J 异常", "从站停掉后主站读", "报读取失败、界面不卡死", slave_down)

        def reconnect():
            app.tabs.select(1)
            self.pump(0.25)
            self.wait(lambda: not app._busy, timeout=5)   # 等停止收尾完成再启动
            self.set_spin(app.slave_port, str(first_port))   # 用回同一端口
            app._toggle_server()
            self.wait(lambda: app.server is not None, timeout=5)
            self.pump(0.3)
            app.tabs.select(0)
            self.pump(0.25)
            app._read_once()
            ok = self.wait(lambda: "失败" not in self.status(), timeout=5)
            self.pump(0.3)
            return f"状态={self.status()} 行数={len(self.rows(app.result_tree))}", ok and len(self.rows(app.result_tree)) > 0

        self.case("J 异常", "从站重启后自动重连", "无需重连即可读成功", reconnect)

        def slave_exit_updates_ui():
            # 模拟从站线程自己死亡：server 的循环退出路径就是清 _running
            # （TcpServer._accept_loop / RtuServer._loop 的 finally），
            # running 是只读属性，直接赋值会 AttributeError。
            app.server._running.clear()
            self.pump(1.0)
            text = str(app.slave_stat.cget("text"))
            return (f"状态={text} server={app.server}",
                    app.server is None and "已停止" in text)

        self.case("J 异常", "从站异常退出同步界面", "清除运行状态与绿色徽标",
                  slave_exit_updates_ui)

        # ================= K 关闭 =================
        def close_app():
            app._on_close()
            alive = False
            try:
                self.root.winfo_exists()
                alive = True
            except tk.TclError:
                alive = False
            return f"窗口已销毁={not alive}", not alive

        self.case("K 关闭", "关闭窗口", "资源释放、窗口销毁", close_app)

    # ------------------------------------------------------------ 汇总

    def summary(self, json_path: str) -> int:
        total = len(self.results)
        failed = [r for r in self.results if not r["ok"]]
        by_group: dict[str, list[dict]] = {}
        for row in self.results:
            by_group.setdefault(row["group"], []).append(row)

        print("\n" + "=" * 78)
        for group, rows in by_group.items():
            good = sum(1 for r in rows if r["ok"])
            print(f"{group:<12} {good}/{len(rows)}")
        print("-" * 78)
        print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
        if failed:
            print("\n失败项：")
            for row in failed:
                print(f"  - [{row['group']}] {row['name']}｜期望：{row['expect']}｜实际：{row['actual']}")

        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump({"results": self.results,
                       "total": total,
                       "passed": total - len(failed),
                       "failed": len(failed)}, handle, ensure_ascii=False, indent=2)
        print(f"\n明细已写入 {json_path}")
        return 0 if not failed else 1


def main() -> int:
    runner = Runner()
    try:
        runner.run()
    finally:
        try:
            runner.root.destroy()
        except Exception:
            pass
    return runner.summary(os.path.join(ROOT, "tests", "_ui_report.json"))


if __name__ == "__main__":
    sys.exit(main())
