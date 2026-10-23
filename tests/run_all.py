"""一键跑完三套测试（逻辑 / 界面 / RTU），并给出统一结论。

为什么要这个脚本：三套测试的跑法原本各不相同 —— 逻辑走 `unittest discover`，
界面脚本要一个带 tkinter 的解释器，RTU 脚本要 pyserial。CI 里只能记住第一条，
另外两套就慢慢没人跑了（而界面恰好是缺陷最密集的地方）。

    python tests/run_all.py            # 全部
    python tests/run_all.py --logic    # 只跑逻辑

RTU 部分在没有 pyserial 的机器上会自动生成一个最小桩（测试本身会把
`serial.Serial` 换成 socket 对），所以不需要联网装包也能验帧结构。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


def _run(cmd: list[str], env: dict | None = None) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _has_tkinter(python: str) -> bool:
    ok, _ = _run([python, "-c", "import tkinter"])
    return ok


def _has_serial(python: str) -> bool:
    ok, _ = _run([python, "-c", "import serial"])
    return ok


def _make_serial_stub() -> str:
    """造一个只提供属性位的 pyserial 桩，让 RTU 帧级用例能跑。"""
    root = tempfile.mkdtemp(prefix="modbus-serial-stub-")
    os.makedirs(os.path.join(root, "serial", "tools"), exist_ok=True)
    with open(os.path.join(root, "serial", "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent('''
            """最小 pyserial 桩：测试自己会把 serial.Serial 换成假串口。"""
            __version__ = "0.0-stub"

            class Serial:  # pragma: no cover
                def __init__(self, *args, **kwargs):
                    raise RuntimeError("stub serial.Serial 不应被直接使用")
        ''').lstrip())
    open(os.path.join(root, "serial", "tools", "__init__.py"), "w", encoding="utf-8").close()
    with open(os.path.join(root, "serial", "tools", "list_ports.py"), "w", encoding="utf-8") as fh:
        fh.write("def comports():\n    return []\n")
    return root


def run_logic(python: str) -> tuple[bool, str]:
    return _run([python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"])


def run_ui(python: str) -> tuple[bool, str]:
    if not _has_tkinter(python):
        return False, "当前解释器没有 tkinter，跳过不了就用 run_tk.bat 里那个"
    return _run([python, os.path.join(TESTS, "ui_full_test.py")])


def run_rtu(python: str) -> tuple[bool, str]:
    env = dict(os.environ)
    note = ""
    if not _has_serial(python):
        stub = _make_serial_stub()
        env["PYTHONPATH"] = stub + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        note = "（用 pyserial 桩）"
    ok, text = _run([python, os.path.join(TESTS, "rtu_full_test.py")], env=env)
    return ok, text + (f"\n注：本次运行{note}" if note else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="跑完 Modbus 工具的全部测试")
    parser.add_argument("--logic", action="store_true", help="只跑逻辑测试")
    parser.add_argument("--ui", action="store_true", help="只跑界面测试")
    parser.add_argument("--rtu", action="store_true", help="只跑 RTU 测试")
    parser.add_argument("--python", default=sys.executable, help="跑界面/RTU 用的解释器")
    args = parser.parse_args()

    only = args.logic or args.ui or args.rtu
    plan = []
    if args.logic or not only:
        plan.append(("逻辑", lambda: run_logic(args.python)))
    if args.ui or not only:
        plan.append(("界面", lambda: run_ui(args.python)))
    if args.rtu or not only:
        plan.append(("RTU", lambda: run_rtu(args.python)))

    results = []
    for name, fn in plan:
        ok, text = fn()
        results.append((name, ok, text))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(textwrap.indent(_tail(text), "    "))

    failed = [name for name, ok, _ in results if not ok]
    print("\n" + "=" * 60)
    print("合计 {} 套，通过 {} 套，失败 {} 套".format(
        len(results), len(results) - len(failed), len(failed)))
    if failed:
        print("失败：" + "、".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
