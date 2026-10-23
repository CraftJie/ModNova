"""Web 应用层的集成测试：真起一个 TCP 从站，走完整链路。

不测 HTTP 协议本身（那是标准库的活），测的是「参数进来 → 帧出去 → 结果解析」
这条业务链路，包括地址换算、类型解释、报文捕获、配置持久化。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.core import ModbusCore


class WebAppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        self.app = ModbusCore(self.cfg_path)

        started = self.app.slave_start(
            {"mode": "tcp", "host": "127.0.0.1", "port": 0, "unit": 1, "size": 500})
        self.port = started["port"]
        self.app.connect({"mode": "tcp", "host": "127.0.0.1",
                          "port": self.port, "unit": 1, "timeout": 2, "retries": 0})

    def tearDown(self):
        self.app.shutdown()
        self.tmp.cleanup()

    def put_registers(self, address: int, values: list[int]) -> None:
        for offset, value in enumerate(values):
            self.app.slave_set("holding_registers", address + offset, value)


class TestTypedRead(WebAppCase):
    def test_uint16_default(self):
        self.put_registers(0, [2200, 501])
        result = self.app.read(3, 0, 2)
        self.assertEqual([row["text"] for row in result["rows"]], ["2200", "501"])

    def test_float32_abcd(self):
        self.put_registers(0, [0x435C, 0x8000])       # 220.5
        result = self.app.read(3, 0, 2, dtype="float32")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["text"], "220.5")
        self.assertEqual(result["rows"][0]["hex"], "0x435C8000")

    def test_float32_cdab(self):
        self.put_registers(0, [0x8000, 0x435C])
        result = self.app.read(3, 0, 2, dtype="float32", byteorder="CDAB")
        self.assertEqual(result["rows"][0]["text"], "220.5")

    def test_int16_negative(self):
        self.put_registers(0, [0xFF9C])
        result = self.app.read(3, 0, 1, dtype="int16")
        self.assertEqual(result["rows"][0]["text"], "-100")

    def test_scaling_and_unit(self):
        self.put_registers(0, [1234])
        result = self.app.read(3, 0, 1, dtype="uint16", scale=0.1, unit_text="℃")
        self.assertEqual(result["rows"][0]["text"], "123.4")
        self.assertEqual(result["unit"], "℃")

    def test_partial_span_not_shown(self):
        self.put_registers(0, [0x435C, 0x8000, 0x1111])   # 3 个寄存器，float32 只有 1 个完整值
        result = self.app.read(3, 0, 3, dtype="float32")
        self.assertEqual(len(result["rows"]), 1)

    def test_coils_stay_bits(self):
        self.app.slave_set("coils", 0, True)
        self.app.slave_set("coils", 1, False)
        result = self.app.read(1, 0, 2, dtype="float32")   # 位区不该被类型解释影响
        self.assertTrue(result["bits"])
        self.assertEqual([row["text"] for row in result["rows"]], ["1", "0"])


class TestAddressMode(WebAppCase):
    def test_plc_address(self):
        self.put_registers(0, [77])
        result = self.app.read(3, 40001, 1, address_mode="plc")
        self.assertEqual(result["address"], 0)             # 实际发的是 0
        self.assertEqual(result["rows"][0]["text"], "77")
        self.assertEqual(result["rows"][0]["display_address"], 40001)

    def test_plc_address_offset(self):
        self.put_registers(107, [88])
        result = self.app.read(3, 40108, 1, address_mode="plc")
        self.assertEqual(result["rows"][0]["text"], "88")

    def test_plc_area_mismatch(self):
        with self.assertRaises(ValueError) as ctx:
            self.app.read(3, 30001, 1, address_mode="plc")   # 30001 是输入寄存器
        self.assertIn("输入寄存器", str(ctx.exception))

    def test_protocol_mode_untouched(self):
        self.put_registers(0, [5])
        result = self.app.read(3, 0, 1, address_mode="protocol")
        self.assertEqual(result["rows"][0]["display_address"], 0)


class TestTypedWrite(WebAppCase):
    def test_write_float32(self):
        self.app.write(16, 10, [220.5], dtype="float32")
        self.assertEqual(self.app.slave_table("holding_registers", 10, 2)["rows"],
                         [0x435C, 0x8000])

    def test_write_with_scaling(self):
        self.app.write(6, 0, [123.4], dtype="uint16", scale=0.1)
        self.assertEqual(self.app.slave_table("holding_registers", 0, 1)["rows"], [1234])

    def test_single_register_too_narrow(self):
        with self.assertRaises(ValueError) as ctx:
            self.app.write(6, 0, [220.5], dtype="float32")
        self.assertIn("写多寄存器", str(ctx.exception))

    def test_write_coils_ignores_type(self):
        self.app.write(15, 0, [True, False, True], dtype="float32")
        self.assertEqual(self.app.slave_table("coils", 0, 3)["rows"], [True, False, True])


class TestFrames(WebAppCase):
    def test_tcp_frames_captured(self):
        self.app.read(3, 0, 1)
        frames = self.app.frames_after(0)
        directions = [f["dir"] for f in frames]
        self.assertIn("tx", directions)
        self.assertIn("rx", directions)

        tx = next(f for f in frames if f["dir"] == "tx")
        # MBAP(7) + PDU(5)：01 03 00 00 00 01
        self.assertEqual(tx["hex"], "00 01 00 00 00 06 01 03 00 00 00 01")
        self.assertEqual(tx["len"], 12)

        rx = next(f for f in frames if f["dir"] == "rx")
        self.assertTrue(rx["hex"].startswith("00 01 00 00 00 05 01 03 02"))

    def test_frames_incremental(self):
        self.app.read(3, 0, 1)
        first = self.app.frames_after(0)
        last_id = first[-1]["id"]
        self.assertEqual(self.app.frames_after(last_id), [])
        self.app.read(3, 0, 1)
        self.assertTrue(len(self.app.frames_after(last_id)) >= 2)


class TestConfigApi(WebAppCase):
    def test_connect_saves_link(self):
        with open(self.cfg_path, encoding="utf-8") as handle:
            import json
            data = json.load(handle)
        self.assertEqual(data["link"]["port"], self.port)

    def test_update_and_preset(self):
        self.app.config_update(view={"dtype": "float32", "byteorder": "CDAB"})
        self.assertEqual(self.app.config.view["dtype"], "float32")

        self.app.config.save_preset("一号机")
        self.app.config_update(view={"dtype": "int16"})
        self.app.config.apply_preset("一号机")
        self.assertEqual(self.app.config.view["dtype"], "float32")


class TestErrors(WebAppCase):
    def test_out_of_range(self):
        from modbus_tool.pdu import ModbusError
        with self.assertRaises(ModbusError) as ctx:
            self.app.read(3, 490, 50)
        # 应用层抛原始异常，翻成人话是 HTTP 层的事（_describe）
        self.assertEqual(ctx.exception.code, 2)

    def test_not_connected(self):
        self.app.disconnect()
        with self.assertRaises(RuntimeError):
            self.app.read(3, 0, 1)


class TestConcurrency(WebAppCase):
    """锁的范围正确性。

    回归用：读请求曾经把整段网络 I/O 套在应用锁里，而从站的日志回调（「主站接入」）
    也要拿这把锁 —— 从站线程于是被卡住，没人应答，主站一直等到超时。
    表现是「偶发、重跑就好」，非常难查。
    """

    def test_log_from_other_thread_is_not_blocked_during_read(self):
        import threading

        logged = threading.Event()
        original = self.app.client.read

        def read_and_check(*args, **kwargs):
            worker = threading.Thread(target=lambda: (self.app.log("并发日志"), logged.set()),
                                      daemon=True)
            worker.start()
            # 旧实现里这里会等满超时；现在应当立刻写入
            self.assertTrue(logged.wait(0.5), "读请求期间另一线程写日志被阻塞了")
            return original(*args, **kwargs)

        self.app.client.read = read_and_check
        try:
            result = self.app.read(3, 0, 1)
        finally:
            self.app.client.read = original
        self.assertEqual(len(result["rows"]), 1)

    def test_write_does_not_block_logging(self):
        import threading

        logged = threading.Event()
        original = self.app.client.write

        def write_and_check(*args, **kwargs):
            worker = threading.Thread(target=lambda: (self.app.log("并发日志"), logged.set()),
                                      daemon=True)
            worker.start()
            self.assertTrue(logged.wait(0.5), "写请求期间另一线程写日志被阻塞了")
            return original(*args, **kwargs)

        self.app.client.write = write_and_check
        try:
            self.app.write(6, 0, [7])
        finally:
            self.app.client.write = original
        self.assertEqual(self.app.slave_table("holding_registers", 0, 1)["rows"][0], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
