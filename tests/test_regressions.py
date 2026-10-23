"""审查报告里每个修复项的回归测试。

这些用例的共同点：**修之前它们会失败**。放在一起是为了以后有人改动
相关代码时，能立刻知道这里踩过什么坑（每条 docstring 都写了当时的症状）。

    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.addressing import AddressError, area_of_fc, plc_to_protocol, resolve_address
from modbus_tool.client import ModbusClient
from modbus_tool.codec import (
    NONPRINT,
    DataType,
    decode,
    encode,
    hex_of,
    patch_ascii,
    span,
)
from modbus_tool.core import ModbusCore
from modbus_tool.datastore import DataStore
from modbus_tool.parsing import normalize_number, parse_int, parse_write_values
from modbus_tool.pdu import (
    ExceptionCode,
    FC,
    ModbusError,
    ParameterError,
    Request,
    pack_request,
    pack_response,
    read_limit,
    unpack_response,
    write_limit,
)
from modbus_tool.server import FaultConfig, ModbusServer, TcpServer
from modbus_tool.transport import TcpTransport
from modbus_tool.view import Field, parse_fields


class TestAsciiPatch(unittest.TestCase):
    """P0-2：单字符编辑不能把相邻字节写成空格；控制字符不能写进去。"""

    def test_short_input_keeps_neighbour_byte(self):
        # 修之前：patch_ascii("A", 0x4243) == 0x4120，第二个字节被写成空格
        self.assertEqual(patch_ascii("A", 0x4243), 0x4143)
        self.assertEqual(patch_ascii("", 0x4243), 0x4243)

    def test_placeholder_still_keeps_byte(self):
        self.assertEqual(patch_ascii("B" + NONPRINT, 0x4100), 0x4200)

    def test_control_characters_rejected(self):
        for bad in ("\x01A", "A\t"):
            with self.assertRaises(ValueError):
                patch_ascii(bad, 0x4142)


class TestFieldReuse(unittest.TestCase):
    """P0-3：add_task 存的是 Field 对象，read() 必须能原样接住。"""

    def test_parse_fields_is_idempotent(self):
        field = Field(offset=2, dtype="float32", name="温度")
        parsed = parse_fields([field])
        self.assertIs(parsed[0], field)

    def test_parse_fields_accepts_dicts(self):
        parsed = parse_fields([{"offset": 1, "dtype": "int16"}])
        self.assertEqual((parsed[0].offset, parsed[0].dtype), (1, "int16"))

    def test_task_with_fields_polls(self):
        """修之前：latest['error'] == "'Field' object has no attribute 'get'"。"""
        with tempfile.TemporaryDirectory() as tmp:
            app = ModbusCore(os.path.join(tmp, "cfg.json"))
            try:
                started = app.slave_start(
                    {"mode": "tcp", "host": "127.0.0.1", "port": 0, "unit": 1, "size": 64})
                app.connect({"mode": "tcp", "host": "127.0.0.1", "port": started["port"],
                             "unit": 1, "timeout": 2, "retries": 0})
                app.slave_set("holding_registers", 0, 2200)
                task = app.add_task(3, 0, 4, 0.1,
                                    fields=[{"offset": 0, "dtype": "uint16", "name": "电压"}])
                deadline = time.time() + 4
                latest = {}
                while time.time() < deadline:
                    latest = app.list_tasks()[0]["latest"]
                    if latest.get("rows"):
                        break
                    time.sleep(0.05)
                self.assertIsNone(latest.get("error"), latest)
                self.assertEqual(latest["rows"][0]["name"], "电压")
                app.remove_task(task["id"])
            finally:
                app.shutdown()


class TestServerQuantityLimits(unittest.TestCase):
    """P1-5：从站自己不校验数量，会发出超规帧或回错异常码。"""

    def setUp(self):
        self.server = ModbusServer(DataStore(size=1000), unit=1)

    def _request(self, fc: int, count: int) -> bytes:
        return self.server.handle_pdu(struct.pack(">BHH", fc, 0, count))

    def test_over_limit_returns_illegal_value(self):
        # 修之前：count=128/200 会因 struct.error 回 0x04（设备故障）
        for count in (126, 128, 200):
            response = self._request(int(FC.READ_HOLDING_REGISTERS), count)
            self.assertEqual(response[0], 0x83, f"count={count}")
            self.assertEqual(response[1], int(ExceptionCode.ILLEGAL_DATA_VALUE), f"count={count}")

    def test_limit_itself_still_works(self):
        response = self._request(int(FC.READ_HOLDING_REGISTERS), 125)
        self.assertEqual(response[0], 0x03)
        self.assertEqual(response[1], 250)

    def test_write_over_limit(self):
        payload = b"\x01\x02" * 124          # 124 个寄存器 > 123
        pdu = struct.pack(">BHHB", int(FC.WRITE_MULTIPLE_REGISTERS), 0, 124, 248) + payload
        response = self.server.handle_pdu(pdu)
        self.assertEqual(response[1], int(ExceptionCode.ILLEGAL_DATA_VALUE))


class TestTruncatedResponse(unittest.TestCase):
    """P1-6：线圈应答不够长时抛 ModbusError，不能是 IndexError。"""

    def test_short_coil_response(self):
        with self.assertRaises(ModbusError) as ctx:
            unpack_response(bytes([0x01, 0x01, 0xFF]), Request(1, 0, 20))
        self.assertEqual(ctx.exception.code, ExceptionCode.SERVER_DEVICE_FAILURE)

    def test_short_register_response(self):
        with self.assertRaises(ModbusError):
            unpack_response(bytes([0x03, 0x02, 0x00, 0x01]), Request(3, 0, 4))

    def test_exact_coil_response_ok(self):
        response = unpack_response(bytes([0x01, 0x01, 0b1010_1010]), Request(1, 0, 8))
        self.assertEqual(response.values[:4], [False, True, False, True])


class TestProbe(unittest.TestCase):
    """P1-8：probe 不能拿上一次成功请求的延迟当「在线」证据。"""

    def test_offline_after_successful_read(self):
        server = TcpServer(host="127.0.0.1", port=0, store=DataStore(size=16), unit=1)
        server.start()
        client = ModbusClient(TcpTransport("127.0.0.1", server.port, timeout=0.5), 1, retries=0)
        try:
            client.transport.open()
            self.assertEqual(client.read_holding_registers(0, 1), [0])
            self.assertIsNotNone(client.last_latency_ms)
        finally:
            server.stop()
        try:
            # 修之前：这里返回 True，因为 last_latency_ms 还是上一次的值
            self.assertFalse(client.probe())
        finally:
            client.close()

    def test_exception_response_counts_as_online(self):
        server = ModbusServer(DataStore(size=16), unit=1)
        self.assertEqual(server.handle_pdu(struct.pack(">BHH", 3, 999, 1))[0], 0x83)


class TestParameterError(unittest.TestCase):
    """P1-11：本地参数错误要和「从站拒绝」分得开。"""

    def test_is_both_types(self):
        exc = ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, "数量超限")
        self.assertIsInstance(exc, ModbusError)     # 旧调用方的兜底仍然成立
        self.assertIsInstance(exc, ValueError)      # 新调用方可以按参数错误捕获
        self.assertEqual(exc.message, "数量超限")
        self.assertNotIn("ILLEGAL_DATA_VALUE", exc.message)

    def test_pack_request_raises_parameter_error(self):
        with self.assertRaises(ParameterError):
            pack_request(Request(FC.READ_HOLDING_REGISTERS, 0, 126))

    def test_address_plus_count_overflow(self):
        """起始 65535 读 10 个，最后一格地址在协议里表示不出来。"""
        with self.assertRaises(ParameterError):
            pack_request(Request(FC.READ_HOLDING_REGISTERS, 0xFFFF, 10))


class TestLimitsSingleSource(unittest.TestCase):
    """P3：上限只有一份（pdu），客户端与从站都从它取。"""

    def test_limits(self):
        self.assertEqual(read_limit(FC.READ_HOLDING_REGISTERS), 125)
        self.assertEqual(read_limit(FC.READ_COILS), 2000)
        self.assertEqual(write_limit(FC.WRITE_MULTIPLE_REGISTERS), 123)
        self.assertEqual(write_limit(FC.WRITE_MULTIPLE_COILS), 1968)
        self.assertIsNone(read_limit(FC.WRITE_MULTIPLE_REGISTERS))


class TestAddressingRoundTrip(unittest.TestCase):
    """P3：PLC 地址往返要闭合；FC23 要有区域归属。"""

    def test_round_trip(self):
        for address in (40001, 40108, 30005, 10001, 465535):
            protocol, area = plc_to_protocol(address)
            from modbus_tool.addressing import protocol_to_plc
            self.assertEqual(protocol_to_plc(protocol, area), address)

    def test_out_of_four_digit_range(self):
        # 修之前：99999 被当成「40001 + 59998」，反算成 459998
        with self.assertRaises(AddressError):
            plc_to_protocol(99999)

    def test_fc23_area(self):
        self.assertEqual(area_of_fc(23), "holding_registers")

    def test_resolve_rejects_area_mismatch(self):
        with self.assertRaises(AddressError) as ctx:
            resolve_address(3, 30001, "plc")
        self.assertIn("区域", str(ctx.exception))


class TestDataStoreArea(unittest.TestCase):
    """P2-20：区域名要校验，别让 getattr 拿到内部属性。"""

    def test_invalid_area(self):
        store = DataStore(size=16)
        for area in ("_lock", "signals", "holding"):
            with self.assertRaises(ValueError):
                store.get(area, 0)
            with self.assertRaises(ValueError):
                store.set(area, 0, 1)

    def test_read_area_bulk(self):
        store = DataStore(size=32)
        store.write_holding_registers(4, [7, 8, 9])
        self.assertEqual(store.read_area("holding_registers", 4, 3), [7, 8, 9])
        with self.assertRaises(ModbusError):
            store.read_area("holding_registers", 30, 10)


class TestParsing(unittest.TestCase):
    """P2-21 / P3：界面与命令行共用一份输入解析。"""

    def test_full_width_and_separators(self):
        self.assertEqual(normalize_number("－5"), "-5")
        self.assertEqual(parse_write_values("1、2；3", 16, "uint16"), [1, 2, 3])

    def test_leading_zero_and_radix(self):
        self.assertEqual(parse_int("0005"), 5)
        self.assertEqual(parse_int("0x10"), 16)
        self.assertEqual(parse_int("0b101"), 5)
        self.assertEqual(parse_int("－5"), -5)

    def test_ascii_string_kept_whole(self):
        self.assertEqual(parse_write_values("ABC, Inc.", 16, "ascii"), ["ABC, Inc."])
        with self.assertRaises(ValueError):
            parse_write_values("MODBUS", 6, "uint16")

    def test_ascii_encode_rejects_numbers(self):
        with self.assertRaises(ValueError):
            encode([220.5], DataType.ASCII)
        self.assertEqual(decode(encode("HELLO", DataType.ASCII), DataType.ASCII),
                         ["HE", "LL", "O" + NONPRINT])


class TestFaultConfig(unittest.TestCase):
    """P2-13：异常码是帧里的一个字节，超范围要夹住。"""

    def test_out_of_range_code_is_clamped(self):
        faults = FaultConfig.from_dict({"exception_code": 300})
        self.assertTrue(1 <= faults.exception_code <= 0xFF)

    def test_injected_code_still_reaches_the_wire(self):
        server = ModbusServer(DataStore(size=16), unit=1,
                              faults=FaultConfig(exception_rate=1.0, exception_code=4))
        response = server.handle_pdu(struct.pack(">BHH", 3, 0, 1))
        self.assertEqual(response, bytes([0x83, 0x04]))


class TestRecorderShutdown(unittest.TestCase):
    """P1-12：退出要关掉 CSV，否则最后几行丢失、句柄泄漏。"""

    def test_file_closed_on_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ModbusCore(os.path.join(tmp, "cfg.json"))
            app.record_start(os.path.join(tmp, "log.csv"))
            handle = app.recorder["file"]
            app.shutdown()
            self.assertTrue(handle.closed)
            self.assertIsNone(app.recorder)


class TestServerCapacity(unittest.TestCase):
    """P2-14：从站要有并发上限，否则谁都能开几百条连接。"""

    def test_max_clients_declared(self):
        server = TcpServer(host="127.0.0.1", port=0)
        self.assertGreaterEqual(server.max_clients, 1)

    def test_over_capacity_is_rejected(self):
        import socket
        server = TcpServer(host="127.0.0.1", port=0, store=DataStore(size=16), unit=1)
        server.max_clients = 1
        server.start()
        first = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        try:
            # 等第一条被 accept 并计入 _clients
            deadline = time.time() + 2
            while not server._clients and time.time() < deadline:
                time.sleep(0.02)
            second = socket.create_connection(("127.0.0.1", server.port), timeout=2)
            try:
                second.settimeout(2)
                self.assertEqual(second.recv(1), b"")   # 被直接关掉
            finally:
                second.close()
        finally:
            first.close()
            server.stop()


class TestIntegerPrecision(unittest.TestCase):
    """P1-9：64 位整数不能过 float，否则低位会被静默改掉。"""

    def test_uint64_through_int(self):
        value = 12345678901234567890
        registers = encode([value], DataType.UINT64)
        self.assertEqual(decode(registers, DataType.UINT64), [value])
        # 走 float 就会丢位：这正是界面原来那条路径做过的事
        via_float = encode([float(value)], DataType.UINT64)
        self.assertNotEqual(decode(via_float, DataType.UINT64)[0], value)

    def test_parse_int_feeds_encode(self):
        self.assertEqual(encode([parse_int("0x4444333322221111")], DataType.UINT64),
                         [0x4444, 0x3333, 0x2222, 0x1111])


class TestFormatHelpers(unittest.TestCase):
    """P3：十六进制/二进制格式化只有一份实现。"""

    def test_hex_and_bin(self):
        self.assertEqual(hex_of([0x435C, 0x8000]), "0x435C8000")
        self.assertEqual(hex_of([]), "")
        self.assertEqual(hex_of([0x000A], prefix=False), "000A")

    def test_pack_response_too_long(self):
        from modbus_tool.pdu import Response
        with self.assertRaises(ValueError):
            pack_response(Response(FC.READ_HOLDING_REGISTERS, [0] * 200))


if __name__ == "__main__":
    unittest.main()
