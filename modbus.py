#!/usr/bin/env python3
"""统一入口。

    python modbus.py                     启动界面（Tkinter）
    python modbus.py read  --tcp ...     命令行读取
    python modbus.py serve --tcp :5020   以从站方式运行

网页版已下线（原 `web.py` + `webui/`），剩下的界面只有 Tk 经典版。
"""

from __future__ import annotations

import sys

# Windows 控制台默认 GBK，中文日志会炸；能改就改。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    argv = sys.argv[1:]

    if not argv or argv[0] in ("gui", "ui", "界面", "--tk", "tk"):
        try:
            from modbus_tool.gui import main as gui_main
        except ImportError as exc:
            # 当前解释器没带 tcl/tk（很多精简版 Python 就是这样）。
            # 直接抛 ModuleNotFoundError 的话，用户只会看到一段回溯。
            print(f"当前 Python 无法加载界面（{exc}）。", file=sys.stderr)
            print("请用 run_tk.bat 启动，或用 MODBUS_PYTHON 指定一个带 tkinter 的解释器。",
                  file=sys.stderr)
            return 1

        gui_main()
        return 0

    from modbus_tool.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
