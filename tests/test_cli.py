"""CLI（cli.py）的回归测试。

背景：cli.py 曾是全仓唯一没有 unittest 覆盖的模块，2026-10-24 审查发现的
H-1 缺陷（`_resolve_address` 引用未导入的 AddressError，`--plc` 非法地址
触发 NameError 裸 traceback）正是从这个缺口漏网的 —— 245 项逻辑测试全绿，
唯独测不到它。

这组用例只测「参数解析与错误路径」，不发任何网络/串口请求；
所有触发 SystemExit 的路径都在创建链路之前。

    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.cli import _display_address, _parse_endpoint, _parse_values, build_parser


def run_main(argv: list[str]):
    from modbus_tool import cli
    return cli.main(argv)


class TestEndpoint(unittest.TestCase):
    """HOST[:PORT] 拆分。"""

    def test_host_and_port(self):
        self.assertEqual(_parse_endpoint("192.168.1.10:1502", 502), ("192.168.1.10", 1502))

    def test_default_port(self):
        self.assertEqual(_parse_endpoint("127.0.0.1", 502), ("127.0.0.1", 502))

    def test_empty_host_falls_back_to_localhost(self):
        # 以前 `:5020` 会 bind("") 绑到全部网卡，统一回退 127.0.0.1
        self.assertEqual(_parse_endpoint(":5020", 502), ("127.0.0.1", 5020))

    def test_bad_port_exits_with_chinese_message(self):
        with self.assertRaises(SystemExit) as ctx:
            _parse_endpoint("host:abc", 502)
        self.assertIn("端口必须是整数", str(ctx.exception.code))


class TestParseValues(unittest.TestCase):
    """写值解析走 parsing 的同一份实现，CLI 侧只负责把 ValueError 变退出码。"""

    def test_ascii_whole_string(self):
        # 早期 CLI 先按 float 解析，`--type ascii --value MODBUS-TEST` 必然报错
        self.assertEqual(_parse_values("MODBUS-TEST", 16, "ascii"), ["MODBUS-TEST"])

    def test_mask_write_hex_pair(self):
        self.assertEqual(_parse_values("0xFFF0,0x000F", 22, "uint16"), [0xFFF0, 0x000F])

    def test_chinese_separators(self):
        self.assertEqual(_parse_values("1、2，3；4", 16, "uint16"), [1, 2, 3, 4])

    def test_fullwidth_minus(self):
        self.assertEqual(_parse_values("－5", 6, "int16"), [-5])

    def test_garbage_exits_with_message(self):
        with self.assertRaises(SystemExit) as ctx:
            _parse_values("abc", 16, "uint16")
        self.assertIn("无法解析为整数", str(ctx.exception.code))


class TestDisplayAddress(unittest.TestCase):
    def test_plc_mode(self):
        self.assertEqual(_display_address(0, 3, True), 40001)

    def test_protocol_mode(self):
        self.assertEqual(_display_address(0, 3, False), 0)


class TestInvalidPlcAddress(unittest.TestCase):
    """H-1 回归：`--plc` 非法地址曾触发 NameError（AddressError 未导入）。

    修之前的实际输出是一段裸 traceback：
        NameError: name 'AddressError' is not defined
    修之后应得到一句中文 + 退出码（SystemExit，code 为文案字符串）。
    """

    def test_read_out_of_4digit_range(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["read", "--tcp", "127.0.0.1:1", "--fc", "3",
                      "--addr", "99999", "--plc"])
        code = ctx.exception.code
        self.assertIsInstance(code, str)
        self.assertIn("超出", code)

    def test_wrong_area_for_fc(self):
        # 30001 是输入寄存器区，与 FC03（保持寄存器）不同区
        with self.assertRaises(SystemExit) as ctx:
            run_main(["read", "--tcp", "127.0.0.1:1", "--fc", "3",
                      "--addr", "30005", "--plc"])
        self.assertIn("不在同一区域", str(ctx.exception.code))

    def test_write_path_also_validated(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["write", "--tcp", "127.0.0.1:1", "--fc", "6",
                      "--addr", "99999", "--plc", "--value", "1"])
        self.assertIsInstance(ctx.exception.code, str)


class TestReadCountLimit(unittest.TestCase):
    def test_count_over_limit_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["read", "--tcp", "127.0.0.1:1", "--fc", "3",
                      "--addr", "0", "--count", "200"])
        self.assertIn("数量必须在 1~125", str(ctx.exception.code))

    def test_fc6_rejected_in_read(self):
        # argparse 的 choices=[1,2,3,4] 先拦一道（退出码 2）；cmd_read 里的
        # read_limit 判定是第二道防线，兜住程序化构造 args 的调用方
        with self.assertRaises(SystemExit):
            run_main(["read", "--tcp", "127.0.0.1:1", "--fc", "6", "--addr", "0"])


class TestServeInitLimit(unittest.TestCase):
    """L-8 回归：`--init` 超过 `--size` 曾靠 DataStore 抛 ModbusError 裸 traceback。"""

    def test_init_over_size_exits_early(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["serve", "--tcp", "127.0.0.1:0", "--size", "4",
                      "--init", "1,2,3,4,5"])
        self.assertIn("超出区间长度", str(ctx.exception.code))


class TestParserDefaults(unittest.TestCase):
    def test_read_defaults(self):
        args = build_parser().parse_args(["read", "--tcp", "h"])
        self.assertEqual((args.fc, args.count, args.dtype, args.byteorder),
                         (3, 10, "uint16", "ABCD"))

    def test_write_fc22_allowed(self):
        args = build_parser().parse_args(
            ["write", "--tcp", "h", "--fc", "22", "--value", "0xFFF0,0x000F"])
        self.assertEqual(args.fc, 22)


if __name__ == "__main__":
    unittest.main()
