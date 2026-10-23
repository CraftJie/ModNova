"""RTU over TCP 的回归测试：真起从站走完整链路。

串口服务器/DTU 透传模式的帧格式契约在这里锁定：
TCP 流里必须是「站地址 + PDU + CRC」的裸 RTU 帧，绝不允许出现 MBAP 头。
"""

from __future__ import annotations

import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.client import ModbusClient
from modbus_tool.core import ModbusCore
from modbus_tool.server import RtuOverTcpServer, create_server
from modbus_tool.transport import RtuOverTcpTransport


def _free_server() -> tuple[RtuOverTcpServer, int]:
    server = RtuOverTcpServer(host="127.0.0.1", port=0, unit=1)
    server.start()
    return server, server.port


class TestRtuOverTcpLoop(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _free_server()

    def tearDown(self):
        self.server.stop()

    def _client(self) -> ModbusClient:
        transport = RtuOverTcpTransport("127.0.0.1", self.port, timeout=2)
        client = ModbusClient(transport, unit=1, retries=0)
        transport.set_tap(lambda *a: None)
        return client

    def test_read_write_roundtrip(self):
        self.server.store.set("holding_registers", 0, 1234)
        client = self._client()
        try:
            self.assertEqual(client.read_holding_registers(0, 2), [1234, 0])
            client.write_register(5, 0xBEEF)
            self.assertEqual(client.read_holding_registers(5, 1), [0xBEEF])
        finally:
            client.close()

    def test_wire_frame_is_bare_rtu(self):
        """线上必须是没有 MBAP 头的裸 RTU 帧 —— 这是对透传链路的根本契约。"""
        seen = []
        transport = RtuOverTcpTransport("127.0.0.1", self.port, timeout=2)
        transport.set_tap(lambda d, u, f: seen.append((d, bytes(f))))
        client = ModbusClient(transport, unit=1, retries=0)
        try:
            client.read_holding_registers(0, 1)
        finally:
            client.close()
        tx = next(f for d, f in seen if d == "tx")
        rx = next(f for d, f in seen if d == "rx")
        # FC03 请求：站号 1 + FC + 地址 2 + 数量 2 + CRC 2 = 8 字节
        self.assertEqual(len(tx), 8)
        self.assertEqual(tx[0], 1)
        self.assertEqual(tx[1], 0x03)
        # 应答：站号 + FC + 字节数 + 数据 2 + CRC 2 = 7 字节
        self.assertEqual(len(rx), 7)
        self.assertEqual(rx[0], 1)
        self.assertEqual(rx[1], 0x03)
        self.assertEqual(rx[2], 2)

    def test_device_identification(self):
        client = self._client()
        try:
            info = client.read_device_id()
            self.assertEqual(info[0x00], "ModNova")
        finally:
            client.close()

    def test_exception_passthrough(self):
        client = self._client()
        try:
            # 65535 越界 → ILLEGAL_DATA_ADDRESS，异常原样上报而不是装作读到 0
            with self.assertRaises(Exception) as ctx:
                client.read_holding_registers(65000, 100)
            self.assertEqual(ctx.exception.code, 0x02)
        finally:
            client.close()

    def test_garbage_breaks_connection(self):
        """对透传连接发垃圾字节：流已错位，服务端必须断开而不是带病解析。"""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        sock.sendall(b"\xff\xff\xff\xff\xff\xff\xff\xff")
        time.sleep(0.15)
        # 服务端断开后 recv 返回空
        sock.settimeout(1.5)
        try:
            data = sock.recv(64)
        except (ConnectionResetError, OSError):
            data = b""
        self.assertEqual(data, b"")
        sock.close()

    def test_create_server_factory(self):
        server = create_server("rtutcp", host="127.0.0.1", port=0, unit=1)
        try:
            server.start()
            self.assertTrue(server.running)
        finally:
            server.stop()
        with self.assertRaises(ValueError):
            create_server("nope")

    def test_unit_mismatch_silenced(self):
        """站号不符时不回话也不断链 —— 和 TCP 从站行为一致。"""
        client = ModbusClient(
            RtuOverTcpTransport("127.0.0.1", self.port, timeout=0.4), unit=9, retries=0)
        try:
            with self.assertRaises(TimeoutError):
                client.read_holding_registers(0, 1)
        finally:
            client.close()


class TestRtuOverTcpCore(unittest.TestCase):
    """应用层入口（ModbusCore）的 rtutcp 模式。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.app = ModbusCore(os.path.join(self.tmp.name, "config.json"))
        started = self.app.slave_start(
            {"mode": "rtutcp", "host": "127.0.0.1", "port": 0, "unit": 1, "size": 500})
        self.port = started["port"]
        self.app.connect({"mode": "rtutcp", "host": "127.0.0.1",
                          "port": self.port, "unit": 1, "timeout": 2, "retries": 0})

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def test_connect_label(self):
        self.assertTrue(self.app.link_info["connected"])
        self.assertEqual(self.app.link_info["mode"], "rtutcp")
        self.assertIn("RTU/TCP", self.app.link_info["label"])

    def test_typed_read(self):
        self.app.slave_set("holding_registers", 0, 0x435C)
        self.app.slave_set("holding_registers", 1, 0x8000)       # 220.5
        result = self.app.read(3, 0, 2, dtype="float32")
        self.assertEqual(result["rows"][0]["text"], "220.5")

    def test_write_and_readback(self):
        self.app.write(16, 10, [7, 8, 9])
        result = self.app.read(3, 10, 3)
        self.assertEqual([row["text"] for row in result["rows"]], ["7", "8", "9"])


if __name__ == "__main__":
    unittest.main()
