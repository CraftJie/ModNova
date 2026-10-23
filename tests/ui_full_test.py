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
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tkinter as tk

from modbus_tool.gui import AREA_OPTIONS, DTYPE_OPTIONS, FC_OPTIONS, ModbusApp
from modbus_tool.transport import HAS_SERIAL


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

        # 双视图改造后默认显示 Modbus Poll 式网格；下面的用例大多是针对
        # 逐行明细的行列语义写的，测试期间统一切到明细，网格另有专项用例。
        app.set_master_view(False)
        app.set_slave_view(False)

        # ================= A 启动与导航 =================
        self.case("A 导航", "窗口标题", "Modbus 工具",
                  lambda: (self.root.title(), self.root.title() == "Modbus 工具"))
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
            app.root.event_generate("<Control-Key-2>")
            self.pump(0.25)
            after_ctrl2 = app.tabs.index
            app.root.event_generate("<Control-Key-1>")
            self.pump(0.25)
            after_ctrl1 = app.tabs.index
            return (f"Ctrl+2→{after_ctrl2} Ctrl+1→{after_ctrl1}",
                    after_ctrl2 == 1 and after_ctrl1 == 0)

        self.case("A 导航", "Ctrl+1 / Ctrl+2", "分别直达主站/从站", ctrl_keys)

        def f6_key():
            app.root.event_generate("<F6>")
            self.pump(0.25)
            first = app.tabs.index
            app.root.event_generate("<F6>")
            self.pump(0.25)
            return f"F6 两次 → {first} → {app.tabs.index}", first == 1 and app.tabs.index == 0

        self.case("A 导航", "F6 往返切换", "两次回到主站", f6_key)

        def click_no_focus():
            self.click_tab(1)
            self.click_tab(0)
            return (f"焦点在切换条={self.root.focus_get() is app.tabs}",
                    self.root.focus_get() is not app.tabs)

        self.case("A 导航", "点击不抢键盘焦点", "焦点不落到切换条", click_no_focus)

        def focus_ring():
            app.tabs.focus_set()
            self.pump(0.2)
            on = app.tabs._focused
            app.tcp_host.focus_set()
            self.pump(0.2)
            off = app.tabs._focused
            return f"进入={on} 离开={off}", on and not off

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

        self.case("A 导航", "数据解释可反复打开", "关掉再点还能弹出", view_dialog_reopens)

        # ================= B 主站链路参数 =================
        self.case("B 链路", "默认 TCP 模式", "TCP 字段可见、RTU 不可见",
                  lambda: (f"TCP={bool(app.tcp_frame.winfo_ismapped())} "
                           f"RTU={bool(app.rtu_frame.winfo_ismapped())}",
                           app.tcp_frame.winfo_ismapped() == 1
                           and app.rtu_frame.winfo_ismapped() == 0))

        def switch_rtu():
            app.link_mode.set("rtu")
            app._on_link_mode_change()
            self.pump(0.2)
            rtu_shown = app.rtu_frame.winfo_ismapped()
            ports = app.rtu_port.get()
            app.link_mode.set("tcp")
            app._on_link_mode_change()
            self.pump(0.2)
            return (f"RTU 显示={rtu_shown} 串口={ports or '(空)'} 已切回={bool(app.tcp_frame.winfo_ismapped())}",
                    rtu_shown == 1 and app.tcp_frame.winfo_ismapped() == 1)

        self.case("B 链路", "TCP/RTU 单选切换", "字段面板跟着换，切回正常", switch_rtu)

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

        self.case("B 链路", "未连接点「读取」", "提示尚未连接",
                  lambda: (app._do_read_async() or self.pump(0.2) or
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
            return (f"LED={text} 按钮={btn} 日志有失败={has_log}", ok and btn == "连接" and has_log)

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

        self.case("C 从站", "TCP/RTU 单选切换", "串口字段出现，切回正常", slave_rtu_switch)

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

        def slave_grid():
            # 网格视图：32 个寄存器按每行 10 格排成 4 行，行头补零地址
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
            return (f"网格 {len(rows)} 行, 首行头={rows[0][0] if rows else '-'}, "
                    f"切回明细={detail_back}, 切走后网格隐藏={back}"), \
                ok and detail_back and back

        self.case("C 从站", "网格视图与切换", "4 行 × 10 格，行头 0/10/20/30，可切回明细", slave_grid)

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
            app._clear_slave()
            self.pump(0.3)
            values = self.slave_values("holding_registers", 0, 10)
            return f"前 10 个={values}", all(v == 0 for v in values)

        self.case("C 从站", "「全部清零」", "四个区归零", clear_all)

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
            ok = self.wait(lambda: app.server is None, timeout=5)
            self.pump(0.3)
            return (f"停止={ok} 状态={app.slave_stat.cget('text')} "
                    f"按钮={app.slave_btn.cget('text')} 徽标={app.tabs.badge(1)}",
                    ok and "已停止" in str(app.slave_stat.cget("text"))
                    and str(app.slave_btn.cget("text")) == "启动从站"
                    and not app.tabs.badge(1))

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
            app._toggle_server()
            ok = self.wait(lambda: app.server is not None, timeout=5)
            self.pump(0.3)
            return f"重启={ok} 端口={app.server.port if app.server else '-'}", ok

        self.case("C 从站", "重新启动从站", "可再次启动", start_again)

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

        self.case("D 自环", "从站记录接入", "从站日志出现「主站接入」",
                  lambda: (self.pump(0.4) or
                           (("主站接入" in self.log_text()),
                            "主站接入" in self.log_text())))

        # ================= E 读功能码 =================
        def read_fc(fc: int, address: int, count: int) -> tuple[str, bool]:
            self.pick_fc(fc)
            self.set_spin(app.addr_var, str(address))
            self.set_spin(app.count_var, str(count))
            app._do_read_async()
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
            self.pick_fc(3)
            read_state = (str(app.read_btn.cget("state")), str(app.write_btn.cget("state")),
                          str(app.count_var.cget("state")))
            self.pick_fc(16)
            write_state = (str(app.read_btn.cget("state")), str(app.write_btn.cget("state")),
                           str(app.count_var.cget("state")))
            self.pick_fc(3)
            ok = (read_state == ("normal", "disabled", "normal")
                  and write_state == ("disabled", "normal", "disabled"))
            return f"读码时={read_state} 写码时={write_state}", ok

        self.case("E 读", "功能码联动按钮状态", "读码禁用写入、写码禁用读取与数量", read_only_counts)

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
            app._do_read_async()
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
                app._do_read_async()
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
            app._do_read_async()
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
            app._do_read_async()
            self.pump(0.6)
            rows = self.rows(app.result_tree)
            shown_addr = rows[0][1] if rows else ""
            shown_val = rows[0][2] if rows else ""
            app.plc_var.set(False)
            self.pump(0.1)
            return f"地址列={shown_addr} 值={shown_val}", shown_addr == "40001" and shown_val == "4321"

        self.case("G 类型", "PLC 地址 40001", "地址列显示 40001，读到协议 0 的值", plc_read)

        def plc_mismatch():
            app.plc_var.set(True)
            self.pump(0.1)
            self.pick_fc(3)
            self.set_spin(app.addr_var, "30001")
            app._do_read_async()
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
            app._do_read_async()
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
            app._do_read_async()
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
            app.master_fmt_box.set("值")
            app._on_display_change()
            self.pump(0.1)

            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "30")
            app._do_read_async()
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

            修之前：网格选过 HEX 再切明细，_cell_step() 返回 1，
            float32 双击写会按 address + index*1 定位，写到前一个寄存器上。
            """
            was_grid = app._master_grid
            app.set_master_view(True)
            app.master_fmt_box.set("HEX")
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
            return f"明细下 fmt={fmt} step={step}", fmt == "值" and step == 2

        self.case("L 回归", "明细视图忽略残留格式", "fmt=值 且 float32 一格跨 2 个寄存器",
                  detail_view_ignores_stale_format)

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

        def poll_disconnect_stops():
            app._toggle_poll()
            self.pump(0.5)
            running = app._polling
            app._toggle_connection()          # 断开
            self.pump(0.6)
            return f"断开前轮询={running} 断开后={app._polling}", running and not app._polling

        self.case("H 轮询", "轮询中断开连接", "轮询自动停止", poll_disconnect_stops)

        # ================= I 日志 =================
        def log_frames():
            app._toggle_connection()          # 重新连上
            self.wait(lambda: app.client is not None, timeout=4)
            self.pump(0.3)
            self.pick_fc(3)
            self.set_spin(app.addr_var, "0")
            self.set_spin(app.count_var, "1")
            app._do_read_async()
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
            app._do_read_async()
            self.pump(1.5)
            text = self.status()
            clear = ("连接被对端" in text or "无法连接" in text or "超时" in text)
            return f"状态={text}", "读取失败" in text and clear

        self.case("J 异常", "从站停掉后主站读", "报读取失败、界面不卡死", slave_down)

        def reconnect():
            app.tabs.select(1)
            self.pump(0.25)
            self.set_spin(app.slave_port, str(first_port))   # 用回同一端口
            app._toggle_server()
            self.wait(lambda: app.server is not None, timeout=5)
            self.pump(0.3)
            app.tabs.select(0)
            self.pump(0.25)
            app._do_read_async()
            ok = self.wait(lambda: "失败" not in self.status(), timeout=5)
            self.pump(0.3)
            return f"状态={self.status()} 行数={len(self.rows(app.result_tree))}", ok and len(self.rows(app.result_tree)) > 0

        self.case("J 异常", "从站重启后自动重连", "无需重连即可读成功", reconnect)

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
