"""报文解析器（frameparser）的测试。

输入输出都是纯文本，测试聚焦三件事：TCP/RTU 外壳识别、各类功能码的字段
拆解、以及坏帧（CRC 错误 / 复制不全）时的诚实降级 —— 绝不猜。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.crc import append_crc
from modbus_tool.frameparser import parse_frame


def hexs(data: bytes) -> str:
    return data.hex(" ")


# FC03 请求：站 1 · 地址 0 · 数量 2
RTU_READ_REQ = append_crc(bytes([1, 0x03, 0x00, 0x00, 0x00, 0x02]))
# FC03 应答：站 1 · 2 个寄存器 0x0898 0x01F5
RTU_READ_RESP = append_crc(bytes([1, 0x03, 0x04, 0x08, 0x98, 0x01, 0xF5]))
# Modbus TCP 同一请求：tid=7 · pid=0 · len=6 · unit=1 + PDU
TCP_READ_REQ = bytes([0, 7, 0, 0, 0, 6, 1, 0x03, 0x00, 0x00, 0x00, 0x02])
# FC22 掩码写：站 1 · 地址 0x1234 · AND 0xFFF0 · OR 0x000F
RTU_MASK = append_crc(bytes([1, 0x16, 0x12, 0x34, 0xFF, 0xF0, 0x00, 0x0F]))


class TestShellDetection(unittest.TestCase):
    def test_rtu_request(self):
        lines = parse_frame(hexs(RTU_READ_REQ))
        self.assertTrue(any("Modbus RTU" in line for line in lines), lines)
        self.assertTrue(any("功能码 0x03" in line for line in lines))
        self.assertTrue(any("起始地址 0" in line and "数量 2" in line for line in lines))

    def test_tcp_request(self):
        lines = parse_frame(hexs(TCP_READ_REQ))
        self.assertTrue(any("Modbus TCP" in line for line in lines))
        self.assertTrue(any("事务号 7" in line for line in lines))
        self.assertTrue(any("数量 2" in line for line in lines))

    def test_explicit_tcp_mode_rejects_rtu(self):
        # RTU 帧在 tcp 模式下：MBAP 检查会撞上「长度不符」或 pid 非零 —— 明确报错
        with self.assertRaises(ValueError):
            parse_frame(hexs(RTU_READ_REQ), mode="tcp")

    def test_bare_pdu(self):
        pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
        lines = parse_frame(hexs(pdu), mode="pdu")
        self.assertTrue(any("裸 PDU" in line for line in lines))
        self.assertTrue(any("起始地址 0" in line for line in lines))

    def test_tolerant_input(self):
        noisy = "01 03，0x00 00-02；C4,0B"
        lines = parse_frame(noisy)          # 0x 前缀、中文逗号、连字符混着来
        self.assertTrue(any("功能码 0x03" in line for line in lines))

    def test_garbage_rejected(self):
        with self.assertRaises(ValueError):
            parse_frame("zz hh")
        with self.assertRaises(ValueError):
            parse_frame("")
        with self.assertRaises(ValueError):
            parse_frame("01")               # 单字节，连功能码都缺


class TestPduFields(unittest.TestCase):
    def test_read_response_values(self):
        lines = parse_frame(hexs(RTU_READ_RESP))
        self.assertTrue(any("应答数据 4 字节" in line for line in lines))
        self.assertTrue(any("0x0898" in line for line in lines))

    def test_exception_response(self):
        resp = append_crc(bytes([1, 0x83, 0x02]))
        lines = parse_frame(hexs(resp))
        joined = "\n".join(lines)
        self.assertIn("异常响应", joined)
        self.assertIn("地址或数量超出从站范围", joined)

    def test_mask_write_semantics(self):
        lines = parse_frame(hexs(RTU_MASK))
        joined = "\n".join(lines)
        self.assertIn("掩码写寄存器", joined)
        self.assertIn("AND 0xFFF0", joined)
        self.assertIn("0x000F", joined)

    def test_write_single_register(self):
        frame = append_crc(bytes([1, 0x06, 0x00, 0x05, 0xBE, 0xEF]))
        lines = parse_frame(hexs(frame))
        self.assertTrue(any("寄存器地址 5" in line and "0xBEEF" in line
                            for line in lines))

    def test_write_single_coil_on(self):
        frame = append_crc(bytes([1, 0x05, 0x00, 0x03, 0xFF, 0x00]))
        joined = "\n".join(parse_frame(hexs(frame)))
        self.assertIn("ON (0xFF00)", joined)

    def test_write_multiple_registers(self):
        frame = append_crc(bytes([1, 0x10, 0x00, 0x14, 0x00, 0x03, 0x06,
                                  0x00, 0x0B, 0x00, 0x16, 0x00, 0x21]))
        joined = "\n".join(parse_frame(hexs(frame)))
        self.assertIn("起始地址 20", joined)
        self.assertIn("0x000B", joined)

    def test_device_identification_response(self):
        # MEI 0x0E · 读码 1 · 一致性 0x81 · more 0 · next 0 · 对象数 1
        # 对象 0「厂商名」长度 7 = "ModNova"
        frame = append_crc(bytes([1, 0x2B, 0x0E, 0x01, 0x81, 0x00, 0x00,
                                  0x01, 0x00, 0x07]) + b"ModNova")
        joined = "\n".join(parse_frame(hexs(frame)))
        self.assertIn("读设备标识", joined)
        self.assertIn("ModNova", joined)
        self.assertIn("厂商名", joined)

    def test_bad_crc_rtu_still_parsed_with_warning(self):
        broken = RTU_READ_REQ[:-1] + bytes([RTU_READ_REQ[-1] ^ 0xFF])
        lines = parse_frame(hexs(broken))
        joined = "\n".join(lines)
        self.assertIn("CRC 校验失败", joined)
        self.assertIn("功能码 0x03", joined)       # 照样拆出字段，只是带着警告


if __name__ == "__main__":
    unittest.main()
