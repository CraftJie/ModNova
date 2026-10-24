"""类型解释、地址换算、配置持久化的单元测试。

字节序那部分用 IEEE754 已知编码做锚点 —— 220.5 的 float32 是 0x435C8000，
四种字节序各自对应一组确定的寄存器值。这类变换写反了肉眼看不出来，
只能靠固定向量锁住。
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.addressing import (
    AddressError,
    area_of_fc,
    describe_plc,
    plc_to_protocol,
    protocol_to_plc,
)
from modbus_tool.codec import (
    NONPRINT,
    ByteOrder,
    DataType,
    decode,
    encode,
    format_value,
    is_integer,
    patch_ascii,
    span,
)
from modbus_tool.config import DEFAULT_CONFIG, Config


class TestByteOrder(unittest.TestCase):
    """220.5 的 IEEE754 大端字节是 43 5C 80 00。"""

    def test_float32_all_orders(self):
        cases = {
            ByteOrder.ABCD: [0x435C, 0x8000],
            ByteOrder.BADC: [0x5C43, 0x0080],
            ByteOrder.CDAB: [0x8000, 0x435C],
            ByteOrder.DCBA: [0x0080, 0x5C43],
        }
        for order, registers in cases.items():
            with self.subTest(order=order.value):
                self.assertEqual(decode(registers, DataType.FLOAT32, order), [220.5])

    def test_float32_roundtrip_all_orders(self):
        for order in ByteOrder:
            with self.subTest(order=order.value):
                registers = encode([220.5], DataType.FLOAT32, order)
                self.assertEqual(decode(registers, DataType.FLOAT32, order), [220.5])

    def test_reorder_is_self_inverse(self):
        # decode/encode 共用同一个变换，靠的就是「做两次回到原样」
        for order in ByteOrder:
            for registers in ([0x1234, 0x5678], [0x1111, 0x2222, 0x3333, 0x4444]):
                with self.subTest(order=order.value, regs=registers):
                    dtype = DataType.UINT32 if len(registers) == 2 else DataType.UINT64
                    back = encode(decode(registers, dtype, order), dtype, order)
                    self.assertEqual(back, registers)


class TestNumericTypes(unittest.TestCase):
    def test_int16_negative(self):
        self.assertEqual(decode([0xFF9C], DataType.INT16), [-100])
        self.assertEqual(encode([-100], DataType.INT16), [0xFF9C])

    def test_uint16_upper_bound(self):
        self.assertEqual(decode([0xFFFF], DataType.UINT16), [65535])

    def test_int32_negative(self):
        self.assertEqual(decode([0xFFFF, 0xFF9C], DataType.INT32), [-100])

    def test_uint32_large(self):
        self.assertEqual(decode([0x0001, 0x86A0], DataType.UINT32), [100000])

    def test_float64(self):
        # 3.14 的 double 是 0x40091EB851EB851F
        registers = [0x4009, 0x1EB8, 0x51EB, 0x851F]
        self.assertAlmostEqual(decode(registers, DataType.FLOAT64)[0], 3.14, places=12)

    def test_span(self):
        self.assertEqual(span(DataType.UINT16), 1)
        self.assertEqual(span(DataType.FLOAT32), 2)
        self.assertEqual(span(DataType.FLOAT64), 4)

    def test_integer_flag(self):
        self.assertTrue(is_integer("int32"))
        self.assertFalse(is_integer("float32"))


class TestScaling(unittest.TestCase):
    def test_scale_only(self):
        # 原始 1234，×0.1 → 123.4 摄氏度
        self.assertAlmostEqual(decode([1234], DataType.UINT16, scale=0.1)[0], 123.4)

    def test_scale_and_bias(self):
        # 4~20mA 折算成 0~100%：raw 4095 → 100
        value = decode([4095], DataType.UINT16, scale=100 / 4095, bias=0)[0]
        self.assertAlmostEqual(value, 100.0, places=6)

    def test_encode_inverts_scaling(self):
        registers = encode([123.4], DataType.UINT16, scale=0.1)
        self.assertEqual(registers, [1234])

    def test_negative_scale(self):
        self.assertEqual(decode([100], DataType.INT16, scale=-1)[0], -100)

    def test_zero_scale_rejected_on_decode(self):
        # 2026-10-24 规范评审 P0-2 回归：scale=0 曾把所有读数静默归零，
        # 看起来像「设备读不到数据」—— 现在必须显式报错。
        with self.assertRaises(ValueError) as ctx:
            decode([1234], DataType.UINT16, scale=0)
        self.assertIn("系数不能为 0", str(ctx.exception))

    def test_zero_scale_rejected_on_encode(self):
        # 旧实现走到除法才抛 ZeroDivisionError（界面只捕 ValueError，
        # 用户看到的是「操作失败（内部错误）」）
        with self.assertRaises(ValueError) as ctx:
            encode([220.5], DataType.FLOAT32, scale=0)
        self.assertIn("系数不能为 0", str(ctx.exception))


class TestEdges(unittest.TestCase):
    def test_partial_span_is_dropped(self):
        # 3 个寄存器按 float32 只能解释出 1 个完整值，剩下半个不能硬拼
        self.assertEqual(len(decode([0x435C, 0x8000, 0x0000], DataType.FLOAT32)), 1)

    def test_empty(self):
        self.assertEqual(decode([], DataType.FLOAT32), [])
        self.assertEqual(encode([], DataType.FLOAT32), [])

    def test_encode_overflow_raises(self):
        with self.assertRaises(ValueError):
            encode([40000], DataType.INT16)
        with self.assertRaises(ValueError):
            encode([70000], DataType.UINT16)

    def test_negative_values_use_twos_complement(self):
        """负数按补码编码：-5 → 0xFFFB，主站按 int16 读回来还是 -5。"""
        self.assertEqual(encode([-5], DataType.INT16), [0xFFFB])
        self.assertEqual(encode([-32768], DataType.INT16), [0x8000])
        self.assertEqual(decode([0xFFFB], DataType.INT16), [-5])
        self.assertEqual(decode([0x8000], DataType.INT16), [-32768])

    def test_negative_unsigned_error_suggests_signed(self):
        """给无符号类型填负数时，报错要说清「改成 int16」。"""
        with self.assertRaises(ValueError) as ctx:
            encode([-5], DataType.UINT16)
        message = str(ctx.exception)
        self.assertIn("uint16", message)
        self.assertIn("int16", message)

    def test_nan_and_inf_formatting(self):
        nan = decode([0x7FC0, 0x0000], DataType.FLOAT32)[0]
        self.assertNotEqual(nan, nan)
        self.assertEqual(format_value(nan, DataType.FLOAT32), "NaN")
        self.assertEqual(format_value(decode([0x7F80, 0x0000], DataType.FLOAT32)[0],
                                     DataType.FLOAT32), "∞")

    def test_format_value(self):
        self.assertEqual(format_value(220.5, "float32"), "220.5")
        self.assertEqual(format_value(1234.0, "uint16"), "1234")

    def test_float32_keeps_seven_digits(self):
        """单精度有 7 位有效数字，6 位会丢掉真实的末位。"""
        value = struct.unpack(">f", struct.pack(">f", 123456.7))[0]
        self.assertEqual(format_value(value, DataType.FLOAT32), "123456.7")
        # 不能为了精度把整数尾巴也带出来
        self.assertEqual(format_value(220.5, DataType.FLOAT32), "220.5")


class TestAscii(unittest.TestCase):
    """ASCII 类型：一个寄存器两个字符。

    现场用途单一 —— 读设备型号、序列号、版本号这类字符串。
    麻烦不在解码，在「不可打印字节怎么显示」和「改一格时别把占位符
    当成真字符写回设备」。
    """

    def test_two_chars_per_register(self):
        self.assertEqual(span(DataType.ASCII), 1)
        self.assertEqual(decode([0x4D4F, 0x442D], DataType.ASCII), ["MO", "D-"])

    def test_byteorder_swaps_the_two_chars(self):
        # 一个寄存器里两个字符谁在前，就是字节序的事
        self.assertEqual(decode([0x4142], DataType.ASCII, ByteOrder.ABCD), ["AB"])
        self.assertEqual(decode([0x4142], DataType.ASCII, ByteOrder.BADC), ["BA"])

    def test_control_bytes_become_placeholder(self):
        # 0x00 填充很常见，显示成占位符而不是看不见的空白
        self.assertEqual(decode([0x4100], DataType.ASCII), ["A" + NONPRINT])
        self.assertEqual(decode([0x0D0A], DataType.ASCII), [NONPRINT * 2])

    def test_space_is_kept(self):
        # 空格是字符串的一部分（"ABC 123"），不能当不可打印处理
        self.assertEqual(decode([0x4120], DataType.ASCII), ["A "])

    def test_encode_pads_odd_length(self):
        self.assertEqual(encode(["AB"], DataType.ASCII), [0x4142])
        self.assertEqual(encode(["ABC"], DataType.ASCII), [0x4142, 0x4300])
        self.assertEqual(encode(["HELLO"], DataType.ASCII), [0x4845, 0x4C4C, 0x4F00])

    def test_encode_rejects_non_ascii(self):
        # 静默写成一堆问号最糟：设备里存了乱码，界面上还看不出来
        with self.assertRaises(ValueError) as ctx:
            encode(["温度"], DataType.ASCII)
        self.assertIn("ASCII", str(ctx.exception))

    def test_roundtrip(self):
        text = "MOD-1000"
        self.assertEqual(decode(encode([text], DataType.ASCII), DataType.ASCII),
                         [text[:2], text[2:4], text[4:6], text[6:]])

    def test_patch_keeps_original_byte_for_placeholder(self):
        """双击改一格后直接回车，0x00 不能被写成句点。"""
        self.assertEqual(patch_ascii("A" + NONPRINT, 0x4100), 0x4100)
        # 只改前一个字符，后一个字节保留
        self.assertEqual(patch_ascii("B" + NONPRINT, 0x4100), 0x4200)
        # 两个都改
        self.assertEqual(patch_ascii("AB", 0x4100), 0x4142)

    def test_patch_respects_byteorder(self):
        # 原值 0x4142 按 BADC 解是 "BA"，写 "AB" 就得把两个字节换过来
        self.assertEqual(patch_ascii("AB", 0x4142, ByteOrder.BADC), 0x4241)

    def test_format_value_passes_string_through(self):
        self.assertEqual(format_value("AB", DataType.ASCII), "AB")

    def test_not_integer(self):
        self.assertFalse(is_integer(DataType.ASCII))


class TestAddressing(unittest.TestCase):
    def test_plc_to_protocol(self):
        cases = {
            40001: (0, "holding_registers"),
            40108: (107, "holding_registers"),
            30005: (4, "input_registers"),
            10009: (8, "discrete_inputs"),
            1: (0, "coils"),
            9999: (9998, "coils"),
            400001: (0, "holding_registers"),
        }
        for plc, expected in cases.items():
            with self.subTest(plc=plc):
                self.assertEqual(plc_to_protocol(plc), expected)

    def test_roundtrip(self):
        for area in ("coils", "discrete_inputs", "input_registers", "holding_registers"):
            for address in (0, 1, 107, 4999):
                with self.subTest(area=area, address=address):
                    plc = protocol_to_plc(address, area)
                    self.assertEqual(plc_to_protocol(plc), (address, area))

    def test_six_digit_form(self):
        self.assertEqual(protocol_to_plc(10000, "holding_registers"), 410001)

    def test_invalid(self):
        with self.assertRaises(AddressError):
            plc_to_protocol(0)
        with self.assertRaises(AddressError):
            protocol_to_plc(-1, "coils")

    def test_area_of_fc(self):
        self.assertEqual(area_of_fc(3), "holding_registers")
        self.assertEqual(area_of_fc(1), "coils")
        self.assertIsNone(area_of_fc(0x2B))

    def test_describe(self):
        self.assertIn("保持寄存器", describe_plc(40001))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_when_missing(self):
        cfg = Config(self.path)
        self.assertEqual(cfg.link["mode"], "tcp")
        self.assertEqual(cfg.view["dtype"], "uint16")
        self.assertFalse(self.path.exists())      # 只读不写

    def test_save_and_reload(self):
        cfg = Config(self.path)
        cfg.update_link({"host": "192.168.1.10", "port": 5020})
        cfg.update_view({"dtype": "float32", "byteorder": "CDAB", "scale": 0.1})
        cfg.save()
        self.assertTrue(self.path.exists())

        again = Config(self.path)
        self.assertEqual(again.link["host"], "192.168.1.10")
        self.assertEqual(again.view["byteorder"], "CDAB")
        self.assertAlmostEqual(again.view["scale"], 0.1)

    def test_missing_fields_are_filled(self):
        # 模拟旧版本配置文件：只有 host，没有 dtype
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"link": {"host": "10.0.0.1"}}), encoding="utf-8")
        cfg = Config(self.path)
        self.assertEqual(cfg.link["host"], "10.0.0.1")
        self.assertEqual(cfg.link["unit"], DEFAULT_CONFIG["link"]["unit"])
        self.assertEqual(cfg.view["dtype"], "uint16")

    def test_corrupted_file_recovers(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ 这不是 json", encoding="utf-8")
        cfg = Config(self.path)
        self.assertEqual(cfg.view["fc"], 3)             # 回到默认
        self.assertTrue(self.path.with_suffix(".json.bad").exists())   # 坏文件留档

    def test_unknown_keys_ignored(self):
        cfg = Config(self.path)
        cfg.update_view({"dtype": "int16", "不存在的字段": 1})
        self.assertNotIn("不存在的字段", cfg.view)
        self.assertEqual(cfg.view["dtype"], "int16")

    def test_presets(self):
        cfg = Config(self.path)
        cfg.update_link({"host": "192.168.1.5"})
        cfg.save_preset("一号机")
        cfg.update_link({"host": "192.168.1.6"})
        cfg.save_preset("二号机")
        self.assertEqual(len(cfg.list_presets()), 2)

        cfg.apply_preset("一号机")
        self.assertEqual(cfg.link["host"], "192.168.1.5")

        # 同名覆盖而不是追加
        cfg.save_preset("一号机")
        self.assertEqual(len(cfg.list_presets()), 2)

        self.assertTrue(cfg.delete_preset("一号机"))
        self.assertFalse(cfg.delete_preset("不存在的"))
        self.assertEqual(Config(self.path).link["host"], "192.168.1.5")

    def test_preset_requires_name(self):
        cfg = Config(self.path)
        with self.assertRaises(ValueError):
            cfg.save_preset("   ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
