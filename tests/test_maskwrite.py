"""FC22（0x16 Mask Write 掩码写）的回归测试。

掩码写是变频器改位域参数（运行命令字）的规范方式：只动 AND 掩码为 0 的位，
其余位保持原值。测试覆盖 PDU 编解码、从站执行语义、真实链路回环和边界。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.client import ModbusClient
from modbus_tool.core import ModbusCore
from modbus_tool.datastore import DataStore
from modbus_tool.pdu import (
    FC,
    ExceptionCode,
    ModbusError,
    Request,
    pack_request,
    pack_response,
    unpack_request,
    unpack_response,
)
from modbus_tool.server import ModbusServer, TcpServer
from modbus_tool.transport import TcpTransport


class TestMaskWritePdu(unittest.TestCase):
    def test_request_roundtrip(self):
        pdu = pack_request(Request(FC.MASK_WRITE, 0x1234, 0, [0xFFF0, 0x000F]))
        self.assertEqual(pdu, bytes([0x16, 0x12, 0x34, 0xFF, 0xF0, 0x00, 0x0F]))
        req = unpack_request(pdu)
        self.assertEqual(req.function, 0x16)
        self.assertEqual(req.address, 0x1234)
        self.assertEqual(req.values, [0xFFF0, 0x000F])

    def test_request_rejects_wrong_arity(self):
        with self.assertRaises(ValueError):
            pack_request(Request(FC.MASK_WRITE, 0, 0, [0xFF]))
        with self.assertRaises(ValueError):
            pack_request(Request(FC.MASK_WRITE, 0, 0, [1, 2, 3]))

    def test_request_rejects_out_of_range_mask(self):
        with self.assertRaises(ValueError):
            pack_request(Request(FC.MASK_WRITE, 0, 0, [0x10000, 0]))

    def test_response_roundtrip(self):
        pdu = pack_response(__import__("modbus_tool.pdu", fromlist=["Response"])
                            .Response(FC.MASK_WRITE, [0xFFF0, 0x000F], 0x1234, 0))
        self.assertEqual(pdu, bytes([0x16, 0x12, 0x34, 0xFF, 0xF0, 0x00, 0x0F]))
        resp = unpack_response(pdu, Request(FC.MASK_WRITE, 0x1234, 0, [0xFFF0, 0x000F]))
        self.assertEqual(resp.values, [0xFFF0, 0x000F])
        self.assertEqual(resp.address, 0x1234)


class TestMaskWriteStore(unittest.TestCase):
    def test_low_bits_set_others_kept(self):
        store = DataStore()
        store.set("holding_registers", 0, 0xAB12)
        result = store.mask_write_holding(0, 0xFFF0, 0x000F)
        # 低 4 位被 OR 覆盖为 0xF，其余位保持 0xAB1
        self.assertEqual(result, 0xAB1F)

    def test_and_zeroes_forced(self):
        store = DataStore()
        store.set("holding_registers", 0, 0xFFFF)
        # AND=0xFF00F? 不合法——用 AND=0x0FFF 清掉高 4 位，OR=0x0000 不置位
        result = store.mask_write_holding(0, 0x0FFF, 0x0000)
        self.assertEqual(result, 0x0FFF)

    def test_or_ones_forced(self):
        store = DataStore()
        store.set("holding_registers", 0, 0x0000)
        # AND=0x0000 全部交给 OR：等价于整字写
        result = store.mask_write_holding(0, 0x0000, 0x5AA5)
        self.assertEqual(result, 0x5AA5)

    def test_out_of_range_address(self):
        store = DataStore(size=10)
        from modbus_tool.pdu import ModbusError as _Me
        with self.assertRaises(_Me):
            store.mask_write_holding(10, 0xFFFF, 0xFFFF)


class TestMaskWriteServer(unittest.TestCase):
    """协议核心直接喂 PDU：不起端口就能跑全部异常路径。"""

    def setUp(self):
        self.server = ModbusServer(DataStore(), unit=1)
        self.server.store.set("holding_registers", 4, 0xAB12)

    def _exchange(self, pdu: bytes) -> bytes:
        out = self.server.handle_pdu(pdu)
        assert out is not None
        return out

    def test_dispatch_updates_register(self):
        resp = self._exchange(pack_request(Request(FC.MASK_WRITE, 4, 0, [0xFFF0, 0x000F])))
        self.assertEqual(resp[0], 0x16)
        self.assertEqual(self.server.store.get("holding_registers", 4), 0xAB1F)

    def test_unit_echo_and_identity(self):
        resp = self._exchange(pack_request(Request(FC.MASK_WRITE, 4, 0, [0xFFFF, 0x0000])))
        # AND=0xFFFF OR=0x0000 → 一个位都不动，但地址与回显仍正确
        self.assertEqual(resp[0], 0x16)
        self.assertEqual(resp[1:3], b"\x00\x04")
        self.assertEqual(self.server.store.get("holding_registers", 4), 0xAB12)

    def test_illegal_address(self):
        resp = self._exchange(pack_request(Request(FC.MASK_WRITE, 60000, 0, [0, 0])))
        self.assertEqual(resp[0], 0x16 | 0x80)
        self.assertEqual(resp[1], ExceptionCode.ILLEGAL_DATA_ADDRESS)

    def test_bad_request_length(self):
        resp = self._exchange(bytes([0x16, 0x00, 0x04, 0xFF]))
        self.assertEqual(resp[0], 0x16 | 0x80)


class TestMaskWriteLink(unittest.TestCase):
    """真实链路：TcpServer 回环 + 应用层入口。"""

    def setUp(self):
        self.server = TcpServer(host="127.0.0.1", port=0, unit=1)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()

    def test_client_mask_write(self):
        self.server.store.set("holding_registers", 7, 0xAB12)
        client = ModbusClient(TcpTransport("127.0.0.1", self.port, timeout=2), 1, 0)
        try:
            client.mask_write(7, 0xFFF0, 0x000F)
            self.assertEqual(client.read_holding_registers(7, 1), [0xAB1F])
        finally:
            client.close()

    def test_client_write_fc22_semantics(self):
        self.server.store.set("holding_registers", 7, 0x0000)
        client = ModbusClient(TcpTransport("127.0.0.1", self.port, timeout=2), 1, 0)
        try:
            client.write(FC.MASK_WRITE, 7, [0x0000, 0x1234])
            self.assertEqual(client.read_holding_registers(7, 1), [0x1234])
        finally:
            client.close()

    def test_core_write_fc22(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        try:
            app = ModbusCore(os.path.join(tmp.name, "config.json"))
            app.slave_start({"mode": "tcp", "host": "127.0.0.1", "port": 0,
                             "unit": 1, "size": 100})
            port = app.slave_status()["port"]
            app.connect({"mode": "tcp", "host": "127.0.0.1", "port": port,
                         "unit": 1, "timeout": 2, "retries": 0})
            app.slave_set("holding_registers", 3, 0xAB12)
            app.write(22, 3, [0xFFF0, 0x000F])
            self.assertEqual(app.read(3, 3, 1)["rows"][0]["text"], str(0xAB1F))
            app.shutdown()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
