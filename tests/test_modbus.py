"""协议层与端到端测试。

运行：python -m unittest discover -s tests -v
（或直接 python tests/test_modbus.py）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.crc import append_crc, crc16, verify_crc
from modbus_tool.datastore import DataStore
from modbus_tool.pdu import (
    FC,
    ExceptionCode,
    ModbusError,
    describe_write_frame,
    Request,
    Response,
    pack_request,
    pack_response,
    unpack_request,
    unpack_response,
)
from modbus_tool.server import ModbusServer, TcpServer
from modbus_tool.client import ModbusClient
from modbus_tool.transport import TcpTransport


class TestCRC(unittest.TestCase):
    def test_standard_vector(self):
        # Modbus 规范里的经典例子：01 03 00 00 00 0A → C5 CD
        frame = bytes.fromhex("01030000000A")
        self.assertEqual(crc16(frame), 0xCDC5)
        self.assertEqual(append_crc(frame).hex(), "01030000000ac5cd")

    def test_roundtrip(self):
        # 最短合法帧是 站地址+功能码+CRC 共 4 字节，所以载荷至少 2 字节
        for payload in (b"\x01\x02", b"\xff\x00\x12", bytes(range(64))):
            self.assertTrue(verify_crc(append_crc(payload)))

    def test_corruption_detected(self):
        frame = bytearray(append_crc(b"\x01\x03\x00\x00\x00\x0a"))
        frame[3] ^= 0xFF
        self.assertFalse(verify_crc(bytes(frame)))


class TestPDU(unittest.TestCase):
    def test_read_holding_request(self):
        self.assertEqual(
            pack_request(Request(FC.READ_HOLDING_REGISTERS, 0, 10)).hex(),
            "030000000a",
        )

    def test_write_single_coil_encoding(self):
        self.assertEqual(pack_request(Request(FC.WRITE_SINGLE_COIL, 5, 1, [True])).hex(),
                         "050005ff00")
        self.assertEqual(pack_request(Request(FC.WRITE_SINGLE_COIL, 5, 1, [False])).hex(),
                         "0500050000")

    def test_write_coils_bit_order(self):
        # 低位在前：第 0、2 个线圈为 1 → 0b00000101
        pdu = pack_request(Request(FC.WRITE_MULTIPLE_COILS, 0, 3, [True, False, True]))
        self.assertEqual(pdu.hex(), "0f000000030105")

    def test_write_registers_roundtrip(self):
        req = Request(FC.WRITE_MULTIPLE_REGISTERS, 100, 3, [1, 0xFFFF, 1234])
        parsed = unpack_request(pack_request(req))
        self.assertEqual(parsed.address, 100)
        self.assertEqual(parsed.values, [1, 0xFFFF, 1234])

    def test_read_write_multiple_roundtrip(self):
        req = Request(FC.READ_WRITE_MULTIPLE_REGISTERS, 0, 2, [7, 8, 9], write_address=50)
        parsed = unpack_request(pack_request(req))
        self.assertEqual((parsed.address, parsed.count), (0, 2))
        self.assertEqual((parsed.write_address, parsed.values), (50, [7, 8, 9]))

    def test_quantity_limits(self):
        with self.assertRaises(ModbusError) as ctx:
            pack_request(Request(FC.READ_HOLDING_REGISTERS, 0, 126))
        self.assertEqual(ctx.exception.code, ExceptionCode.ILLEGAL_DATA_VALUE)

    def test_exception_response(self):
        from modbus_tool.pdu import pack_exception
        with self.assertRaises(ModbusError) as ctx:
            unpack_response(pack_exception(FC.READ_COILS, ExceptionCode.ILLEGAL_DATA_ADDRESS),
                            Request(FC.READ_COILS, 0, 1))
        self.assertEqual(ctx.exception.code, ExceptionCode.ILLEGAL_DATA_ADDRESS)

    def test_response_roundtrip(self):
        resp = Response(FC.READ_HOLDING_REGISTERS, [11, 22, 33])
        parsed = unpack_response(pack_response(resp), Request(FC.READ_HOLDING_REGISTERS, 0, 3))
        self.assertEqual(parsed.values, [11, 22, 33])

    def test_unsupported_function(self):
        with self.assertRaises(ModbusError) as ctx:
            unpack_request(b"\x08\x00\x00\x12\x34")
        self.assertEqual(ctx.exception.code, ExceptionCode.ILLEGAL_FUNCTION)


class TestDataStore(unittest.TestCase):
    def test_bounds(self):
        store = DataStore(size=100)
        with self.assertRaises(ModbusError) as ctx:
            store.read_holding_registers(95, 10)
        self.assertEqual(ctx.exception.code, ExceptionCode.ILLEGAL_DATA_ADDRESS)

    def test_write_read(self):
        store = DataStore(size=32)
        store.write_holding_registers(4, [1, 2, 3])
        self.assertEqual(store.read_holding_registers(4, 3), [1, 2, 3])


class TestServerCore(unittest.TestCase):
    def setUp(self):
        self.server = ModbusServer(DataStore(size=200), unit=1)

    def test_read_write_through_pdu(self):
        write = pack_request(Request(FC.WRITE_MULTIPLE_REGISTERS, 10, 2, [111, 222]))
        self.server.handle_pdu(write)
        read = pack_request(Request(FC.READ_HOLDING_REGISTERS, 10, 2))
        resp = unpack_response(self.server.handle_pdu(read), Request(FC.READ_HOLDING_REGISTERS, 10, 2))
        self.assertEqual(resp.values, [111, 222])

    def test_bad_address_gives_exception_pdu(self):
        pdu = self.server.handle_pdu(pack_request(Request(FC.READ_HOLDING_REGISTERS, 190, 20)))
        self.assertEqual(pdu[0], 0x83)
        self.assertEqual(pdu[1], ExceptionCode.ILLEGAL_DATA_ADDRESS)

    def test_illegal_coil_value(self):
        # 0x05 只允许 0000 / FF00
        pdu = self.server.handle_pdu(bytes.fromhex("0500001234"))
        self.assertEqual(pdu[1], ExceptionCode.ILLEGAL_DATA_VALUE)

    def test_unknown_function(self):
        pdu = self.server.handle_pdu(b"\x08\x00\x00\x12\x34")
        self.assertEqual(pdu[:2], bytes([0x88, ExceptionCode.ILLEGAL_FUNCTION]))


class TestTcpLoopback(unittest.TestCase):
    """真起一个 TCP 从站，用主站连上去跑完整链路。"""

    def setUp(self):
        self.server = TcpServer(host="127.0.0.1", port=0, unit=1)
        self.server.start()
        self.client = ModbusClient(
            TcpTransport("127.0.0.1", self.server.port, timeout=2.0), unit=1
        )

    def tearDown(self):
        self.client.close()
        self.server.stop()

    def test_single_register_roundtrip(self):
        self.client.write_register(7, 4321)
        self.assertEqual(self.client.read_holding_registers(7, 1), [4321])

    def test_multi_register_roundtrip(self):
        self.client.write_registers(100, [11, 22, 33, 44])
        self.assertEqual(self.client.read_holding_registers(100, 4), [11, 22, 33, 44])

    def test_coils_roundtrip(self):
        self.client.write_coils(0, [True, False, True, True, False])
        self.assertEqual(self.client.read_coils(0, 5), [True, False, True, True, False])

    def test_single_coil(self):
        self.client.write_coil(3, True)
        self.assertTrue(self.client.read_coils(3, 1)[0])

    def test_input_and_discrete_are_independent(self):
        self.server.store.set("input_registers", 0, 99)
        self.assertEqual(self.client.read_input_registers(0, 1), [99])
        self.assertEqual(self.client.read_holding_registers(0, 1), [0])

    def test_address_out_of_range(self):
        with self.assertRaises(ModbusError) as ctx:
            self.client.read_holding_registers(9999, 20)
        self.assertEqual(ctx.exception.code, ExceptionCode.ILLEGAL_DATA_ADDRESS)

    def test_sequence_of_requests(self):
        # 连续多次请求，检验事务号推进与粘包处理
        for i in range(50):
            self.client.write_register(i, i * 3)
        self.assertEqual(self.client.read_holding_registers(0, 50), [i * 3 for i in range(50)])


class DescribeWriteFrameTest(unittest.TestCase):
    """报文日志里那行「写了几个」的解读。

    「填了几个值」和「写了几个寄存器」不是一回事 —— float32 一个值占 2 个，
    所以这个数字必须从帧里读，而不是从界面参数猜。
    """

    def test_fc16_register_count(self):
        frame = bytes([1, 0x10, 0, 100, 0, 2, 4, 0x41, 0x48, 0, 0, 0x5A, 0x3D])
        text = describe_write_frame(frame)
        self.assertIn("2 个寄存器", text)
        self.assertIn("4 字节", text)
        self.assertIn("起始 100", text)

    def test_fc16_with_zero_address_is_not_taken_as_tcp(self):
        """起始地址 0 时，RTU 帧的第 3–4 字节也是 00 00，别误判成 TCP。"""
        frame = bytes([1, 0x10, 0, 0, 0, 5, 10] + [0] * 10 + [0, 0])
        text = describe_write_frame(frame)
        self.assertIn("5 个寄存器", text)
        self.assertIn("起始 0", text)

    def test_tcp_frame(self):
        frame = bytes([0, 1, 0, 0, 0, 0x0B, 1, 0x10, 0, 100, 0, 2, 4, 0x41, 0x48, 0, 0])
        self.assertIn("2 个寄存器", describe_write_frame(frame))

    def test_single_write_functions(self):
        self.assertIn("1 个寄存器",
                      describe_write_frame(bytes([1, 6, 0, 5, 0, 10, 0, 0])))
        self.assertIn("1 个线圈",
                      describe_write_frame(bytes([1, 5, 0, 5, 0xFF, 0, 0, 0])))

    def test_fc15_coils(self):
        frame = bytes([1, 0x0F, 0, 0, 0, 10, 2, 0xFF, 0x03, 0, 0])
        text = describe_write_frame(frame)
        self.assertIn("10 个线圈", text)
        self.assertIn("2 字节", text)

    def test_read_frame_has_no_summary(self):
        self.assertEqual(describe_write_frame(bytes([1, 3, 0, 0, 0, 10, 0, 0])), "")

    def test_byte_count_mismatch_is_flagged(self):
        """数量说 2 个、字节数只给 3 —— 报文本身有问题，要提示。"""
        frame = bytes([1, 0x10, 0, 1, 0, 2, 3, 0x41, 0x48, 0])
        text = describe_write_frame(frame)
        self.assertIn("可疑", text)

    def test_short_frame_is_ignored(self):
        self.assertEqual(describe_write_frame(b"\x01\x06\x00"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
