"""审查报告里每个修复项的回归测试。

这些用例的共同点：**修之前它们会失败**。放在一起是为了以后有人改动
相关代码时，能立刻知道这里踩过什么坑（每条 docstring 都写了当时的症状）。

    python -m unittest discover -s tests -p "test_*.py"
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
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
from modbus_tool.config import Config
from modbus_tool.core import ModbusCore
from modbus_tool.datastore import DataStore
from modbus_tool.parsing import (
    normalize_number,
    parse_int,
    parse_write_values,
    split_values,
)
from modbus_tool.pdu import (
    ExceptionCode,
    FC,
    ModbusError,
    ParameterError,
    Request,
    build_device_id_response,
    pack_request,
    pack_response,
    parse_device_id_response,
    read_limit,
    unpack_response,
    write_limit,
)
from modbus_tool.server import FaultConfig, ModbusServer, TcpServer
from modbus_tool.transport import TcpTransport, TransportError
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


class TestHalfFrameTimeout(unittest.TestCase):
    """P0-1：TCP 半帧超时后必须断链，否则残字节让后续响应全部错位。"""

    def setUp(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.server.accept()
            self.conn = conn
            # 只发 4 字节 MBAP 头的前半截，然后装死 —— 模拟弱网断流
            conn.sendall(b"\x00\x01\x00\x00")
        except OSError:
            pass

    def tearDown(self):
        if self.conn is not None:
            self.conn.close()
        self.server.close()

    def test_half_frame_timeout_closes_link(self):
        t = TcpTransport("127.0.0.1", self.port, timeout=0.3)
        t.open()
        time.sleep(0.2)          # 等对端把半帧发出来
        with self.assertRaises(TimeoutError):
            t.recv()
        # 修之前：socket 保持打开，残字节留在流里，下一个响应对不上号；
        # 修之后：半帧超时主动断链，上层重连逻辑从干净的字节流开始
        self.assertFalse(t.connected)


class TestStaleTidFrameLogged(unittest.TestCase):
    """2026-10-24 规范评审 P2-2：事务号不符的迟到应答此前不进报文日志。

    排查「上一条为什么超时」时，迟到帧恰恰是最直接的证据 ——
    丢弃前必须先 _emit 留档，否则超时永远无从对证。
    """

    def test_stale_frame_emitted_before_discard(self):
        a, b = socket.socketpair()
        t = TcpTransport("127.0.0.1", 502, timeout=1.0)
        t._sock = a                    # 不真连：直接把 socketpair 一端塞给它
        t._tid = 5
        seen: list[tuple[str, int, bytes]] = []
        t.set_tap(lambda direction, unit, frame: seen.append((direction, unit, frame)))

        stale = struct.pack(">HHHB", 1, 0, 5, 1) + b"\x03\x02\x12\x34"
        fresh = struct.pack(">HHHB", 5, 0, 5, 1) + b"\x03\x02\x12\x34"
        b.sendall(stale + fresh)
        try:
            unit, body = t.recv()
        finally:
            b.close()
            t.close()

        self.assertEqual((unit, body), (1, b"\x03\x02\x12\x34"))
        # 修之前 tap 只记匹配帧（1 条）；修之后迟到帧也留下证据（2 条）
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(direction == "rx" for direction, _u, _f in seen))
        self.assertEqual(seen[0][2][0:2], b"\x00\x01")     # 迟到帧的事务号 1


class TestEmptyHostFallback(unittest.TestCase):
    """P0-2：监听地址留空一律回退 127.0.0.1，绝不静默绑 0.0.0.0。"""

    def test_tcp_server_default_is_loopback(self):
        srv = TcpServer(port=0)          # 不传 host，走默认值
        srv.start()
        try:
            self.assertEqual(srv._sock.getsockname()[0], "127.0.0.1")
        finally:
            srv.stop()

    def test_core_slave_start_empty_host_is_loopback(self):
        cfg_dir = tempfile.mkdtemp()
        core = ModbusCore(config_path=os.path.join(cfg_dir, "config.json"))
        try:
            core.slave_start({"mode": "tcp", "host": "", "port": 0})
            self.assertEqual(core.slave.host, "127.0.0.1")
        finally:
            core.slave_stop()

    def test_cli_endpoint_empty_host_is_loopback(self):
        from modbus_tool.cli import _parse_endpoint
        self.assertEqual(_parse_endpoint(":5020", 502), ("127.0.0.1", 5020))
        self.assertEqual(_parse_endpoint("0.0.0.0:5020", 502), ("0.0.0.0", 5020))


class TestServerKeepalive(unittest.TestCase):
    """P1-4：接受的连接必须开 SO_KEEPALIVE，半开连接不能永久占槽。"""

    def test_accepted_connection_sets_keepalive(self):
        srv = TcpServer(port=0)
        srv.start()
        cli = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        try:
            time.sleep(0.5)      # 等 accept 线程完成设置
            flags = [c.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
                     for c in list(srv._clients)]
            self.assertTrue(flags)
            self.assertTrue(all(flags))
        finally:
            cli.close()
            srv.stop()


class TestDeviceIdBudget(unittest.TestCase):
    """P1-5：设备标识响应整体不得超过 PDU 253 字节，且必须可解析。"""

    def test_long_identity_stays_within_pdu_limit(self):
        identity = {i: "X" * 250 for i in range(7)}
        for code in (1, 2, 3):
            pdu = build_device_id_response(identity, code)
            self.assertLessEqual(len(pdu), 253)
            info = parse_device_id_response(pdu)      # 超限帧自己客户端都收不了
            self.assertTrue(info)

    def test_normal_identity_intact(self):
        identity = {0: "ModNova", 1: "SIM", 2: "V1.4.0"}
        for code in (1, 2, 3):
            pdu = build_device_id_response(identity, code)
            self.assertEqual(parse_device_id_response(pdu), identity)


class TestProbeThreadSafety(unittest.TestCase):
    """P1-6：probe 换站号全程持锁，既不死锁也不把并发请求发到错站。"""

    def test_probe_restores_unit_and_never_deadlocks(self):
        class StubTransport:
            label = "stub"

            def __init__(self):
                self.closed = False

            @property
            def connected(self):
                return False

            def open(self):
                raise TransportError("连不上")

            def close(self):
                self.closed = True

            def send(self, unit, pdu):
                raise TransportError("not connected")

            def recv(self):
                raise TransportError("not connected")

            def set_tap(self, callback):
                pass

        client = ModbusClient(StubTransport(), 1, retries=0)
        self.assertFalse(client.probe(5))    # 修之前这里会死锁（Lock 不可重入）
        self.assertEqual(client.unit, 1)     # 站号必须被还原


class TestFC23Atomic(unittest.TestCase):
    """P1-7：FC23 先写后读必须在一个锁周期内完成。"""

    def test_read_sees_own_write_when_ranges_overlap(self):
        store = DataStore(size=100)
        store.write_holding_registers(8, [111, 222, 0])
        # 写 [10..11]，读 [8..13]：读区包含刚写的格子，必须看到新值
        result = store.rw_holding_registers(10, [7, 7], 8, 6)
        self.assertEqual(result, [111, 222, 7, 7, 0, 0])

    def test_out_of_range_write_rejected_before_read(self):
        store = DataStore(size=100)
        with self.assertRaises(ModbusError):
            store.rw_holding_registers(99, [1, 2], 0, 1)


class TestResponseQuantityExact(unittest.TestCase):
    """P2-10：读寄存器应答数量必须精确相等，多给少给都拒收。"""

    def test_extra_registers_rejected(self):
        request = Request(FC.READ_HOLDING_REGISTERS, 0, 2)
        pdu = bytes([FC.READ_HOLDING_REGISTERS, 6]) + struct.pack(">3H", 1, 2, 3)
        with self.assertRaises(ModbusError):
            unpack_response(pdu, request)

    def test_short_response_still_rejected(self):
        request = Request(FC.READ_HOLDING_REGISTERS, 0, 2)
        pdu = bytes([FC.READ_HOLDING_REGISTERS, 2]) + struct.pack(">H", 1)
        with self.assertRaises(ModbusError):
            unpack_response(pdu, request)

    def test_exact_response_accepted(self):
        request = Request(FC.READ_HOLDING_REGISTERS, 0, 2)
        pdu = bytes([FC.READ_HOLDING_REGISTERS, 4]) + struct.pack(">2H", 1, 2)
        self.assertEqual(unpack_response(pdu, request).values, [1, 2])

    def test_fc23_extra_registers_rejected(self):
        request = Request(FC.READ_WRITE_MULTIPLE_REGISTERS, 0, 2,
                          [1], write_address=0)
        pdu = bytes([FC.READ_WRITE_MULTIPLE_REGISTERS, 6]) + struct.pack(">3H", 1, 2, 3)
        with self.assertRaises(ModbusError):
            unpack_response(pdu, request)


class TestConfigSaveConcurrency(unittest.TestCase):
    """P2-11：多线程并发保存，配置文件必须始终是合法 JSON。"""

    def test_concurrent_save_keeps_file_valid(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        cfg = Config(path)
        errors = []

        def worker(n):
            try:
                for i in range(15):
                    cfg.data["view"]["count"] = n * 1000 + i
                    cfg.save()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(errors, [])
        with open(path, encoding="utf-8") as handle:
            json.load(handle)


class TestSeparatorConsistency(unittest.TestCase):
    """P2-8：工具条提示与真实解析必须同口径 —— 顿号、半角分号都算分隔符。"""

    def test_split_values_accepts_all_separators(self):
        self.assertEqual(split_values("1、2；3，4;5,6"),
                         ["1", "2", "3", "4", "5", "6"])


class _LoopbackTransport:
    """发出去的 PDU 原样收回 —— 让 client.write 能走完整校验链而不碰网络。"""

    label = "loopback"

    def __init__(self) -> None:
        self.connected = True
        self.sent: bytes = b""

    def open(self) -> None: ...
    def close(self) -> None: ...

    def send(self, unit: int, pdu: bytes) -> None:
        self.sent = pdu

    def recv(self) -> tuple[int, bytes]:
        return 1, self.sent


class TestWriteEchoAddress(unittest.TestCase):
    """2026-10-24 L-3：FC05/06 响应地址回显不符要报错（此前只有 FC22 校验）。"""

    def test_register_echo_mismatch_rejected(self):
        request = Request(FC.WRITE_SINGLE_REGISTER, 7, 1, [123])
        with self.assertRaises(ModbusError):
            unpack_response(struct.pack(">BHH", 6, 8, 123), request)

    def test_coil_echo_mismatch_rejected(self):
        request = Request(FC.WRITE_SINGLE_COIL, 3, 1, [1])
        with self.assertRaises(ModbusError):
            unpack_response(struct.pack(">BHH", 5, 4, 0xFF00), request)

    def test_echo_ok_passes(self):
        resp = unpack_response(struct.pack(">BHH", 6, 7, 123),
                               Request(FC.WRITE_SINGLE_REGISTER, 7, 1, [123]))
        self.assertEqual((resp.address, resp.values), (7, [123]))

    def test_end_to_end_mismatch_surfaces_via_client(self):
        # 环回链路上把回显地址改错，client.write 必须把它当失败而不是照单全收
        transport = _LoopbackTransport()

        original_recv = transport.recv

        def corrupt_echo():
            unit, pdu = original_recv()
            return unit, struct.pack(">BHH", pdu[0], pdu[1] + 1,  # 地址 +1
                                     int.from_bytes(pdu[3:5], "big"))

        client = ModbusClient(transport, unit=1, retries=0)
        client.transport.recv = corrupt_echo  # type: ignore[method-assign]
        with self.assertRaises(ModbusError):
            client.write(6, 7, [123])


class TestWriteEchoValue(unittest.TestCase):
    """2026-10-24 规范评审 P1-3：FC05/06 的**值**回显此前没有校验。

    地址对得上、值对不上，病因相同（流错位 / 从站行为异常）—— 照单全收
    会让用户以为写成功了。FC05 回显 0xFF00/0x0000，FC06 原值回传。
    """

    def test_register_value_echo_mismatch_rejected(self):
        request = Request(FC.WRITE_SINGLE_REGISTER, 7, 1, [123])
        with self.assertRaises(ModbusError) as ctx:
            unpack_response(struct.pack(">BHH", 6, 7, 124), request)
        self.assertIn("写回显值不符", str(ctx.exception))

    def test_coil_value_echo_mismatch_rejected(self):
        # 写 ON(0xFF00) 却回 OFF(0x0000)：地址一致也不能收
        request = Request(FC.WRITE_SINGLE_COIL, 3, 1, [1])
        with self.assertRaises(ModbusError):
            unpack_response(struct.pack(">BHH", 5, 3, 0x0000), request)

    def test_value_echo_ok_passes(self):
        resp = unpack_response(struct.pack(">BHH", 5, 3, 0xFF00),
                               Request(FC.WRITE_SINGLE_COIL, 3, 1, [1]))
        self.assertEqual((resp.address, resp.values), (3, [0xFF00]))

    def test_end_to_end_value_mismatch_surfaces_via_client(self):
        # 环回链路上把回显值 +1，client.write 必须报错而不是静默成功
        transport = _LoopbackTransport()
        original_recv = transport.recv

        def corrupt_value_echo():
            unit, pdu = original_recv()
            return unit, struct.pack(">BHH", pdu[0], int.from_bytes(pdu[1:3], "big"),
                                     int.from_bytes(pdu[3:5], "big") + 1)

        client = ModbusClient(transport, unit=1, retries=0)
        client.transport.recv = corrupt_value_echo  # type: ignore[method-assign]
        with self.assertRaises(ModbusError):
            client.write(6, 7, [123])


class TestClientWriteRejectsFractional(unittest.TestCase):
    """2026-10-24 L-4：client.write 对 220.5 这类值曾静默 int() 截断成 220。"""

    def setUp(self):
        self.client = ModbusClient(_LoopbackTransport(), unit=1, retries=0)

    def test_fractional_single_register_rejected(self):
        with self.assertRaises(ValueError):
            self.client.write(6, 0, [1.5])

    def test_fractional_multi_register_rejected(self):
        with self.assertRaises(ValueError):
            self.client.write(16, 0, [100, 220.5])

    def test_fractional_mask_rejected(self):
        with self.assertRaises(ValueError):
            self.client.write(22, 0, [0.5, 0x000F])

    def test_integral_float_and_bool_accepted(self):
        resp = self.client.write(6, 7, [5.0])
        self.assertEqual(resp.values, [5])
        resp = self.client.write(6, 7, [True])
        self.assertEqual(resp.values, [1])


class TestCoreStateZeroLatency(unittest.TestCase):
    """2026-10-24 L-7：0.0ms 是合法读数，truthiness 判断会把它丢成 None。"""

    def test_zero_latency_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ModbusCore(os.path.join(tmp, "cfg.json"))
            try:
                dummy = ModbusClient(_LoopbackTransport(), unit=1)
                dummy.request_count = 3
                dummy.error_count = 0
                dummy.last_latency_ms = 0.0
                app.client = dummy
                state = app.state()
                self.assertEqual(state["stats"]["latency"], 0.0)
            finally:
                app.client = None
                app.shutdown()


if __name__ == "__main__":
    unittest.main()
