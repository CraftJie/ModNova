"""新增能力的测试：字段视图、数据源信号、故障注入、设备识别、多任务与 CSV。

这些功能的共同点是「不改变协议本身，但改变使用体验」，所以测试重点放在
边界与副作用上：字段越界怎么处理、手动改值会不会被信号覆盖回去、
故障注入的概率参数会不会被传成非法值。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tool.codec import DataType
from modbus_tool.datastore import DataStore, Signal
from modbus_tool.pdu import (
    ExceptionCode,
    ModbusError,
    build_device_id_request,
    build_device_id_response,
    parse_device_id_response,
)
from modbus_tool.client import ModbusClient
from modbus_tool.server import FaultConfig, ModbusServer
from modbus_tool.view import Field, decode_fields, parse_fields, total_span, validate_fields
from modbus_tool.core import ModbusCore
from modbus_tool.transport import Transport


class TestFieldView(unittest.TestCase):
    def test_from_dict_defaults(self):
        item = Field.from_dict({"offset": 2})
        self.assertEqual(item.dtype, "uint16")
        self.assertEqual(item.byteorder, "ABCD")
        self.assertEqual(item.scale, 1.0)

    def test_span(self):
        self.assertEqual(Field(offset=0, dtype="float32").span, 2)
        self.assertEqual(Field(offset=0, dtype="float64").span, 4)

    def test_total_span(self):
        fields = parse_fields([{"offset": 0, "dtype": "float32"},
                               {"offset": 4, "dtype": "uint16"}])
        self.assertEqual(total_span(fields), 5)

    def test_decode_mixed(self):
        # 0..1 是 float32(220.5)，2 是 uint16(3)，4..5 是 float32(3.14)
        registers = [0x435C, 0x8000, 3, 0, 0x4048, 0xF5C3]
        fields = parse_fields([
            {"offset": 0, "dtype": "float32", "name": "温度", "unit": "℃"},
            {"offset": 2, "dtype": "uint16", "name": "状态"},
            {"offset": 4, "dtype": "float32", "name": "设定值"},
        ])
        rows = decode_fields(registers, fields)
        self.assertEqual([row["text"] for row in rows], ["220.5", "3", "3.14"])
        self.assertEqual(rows[0]["unit"], "℃")
        self.assertEqual(rows[0]["hex"], "0x435C8000")

    def test_scaling_per_field(self):
        fields = parse_fields([{"offset": 0, "dtype": "uint16", "scale": 0.1, "name": "电压"}])
        self.assertEqual(decode_fields([2200], fields)[0]["text"], "220")

    def test_field_beyond_data_is_skipped(self):
        # 只有 2 个寄存器，float64 字段要 4 个，跳过而不是拼半个
        fields = parse_fields([{"offset": 0, "dtype": "float64"}])
        self.assertEqual(decode_fields([0x1234, 0x5678], fields), [])

    def test_validate_overlap(self):
        fields = parse_fields([
            {"offset": 0, "dtype": "uint32", "name": "A"},
            {"offset": 1, "dtype": "uint16", "name": "B"},
        ])
        warnings = validate_fields(fields, 10)
        self.assertTrue(any("重叠" in w for w in warnings))

    def test_validate_out_of_range(self):
        fields = parse_fields([{"offset": 9, "dtype": "uint32", "name": "X"}])
        warnings = validate_fields(fields, 10)
        self.assertTrue(any("超出" in w for w in warnings))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            Field(offset=-1)
        with self.assertRaises(ValueError):
            Field(offset=0, dtype="not-a-type")


class TestSignals(unittest.TestCase):
    def test_sine_stays_in_range(self):
        signal = Signal("holding_registers", 0, "sine", period=1, low=100, high=200)
        values = [signal.value_at(t / 20) for t in range(20)]
        self.assertTrue(all(100 <= v <= 200 for v in values))
        self.assertGreater(max(values) - min(values), 50)   # 确实在动

    def test_ramp_is_monotonic_within_period(self):
        signal = Signal("holding_registers", 0, "ramp", period=4, low=0, high=100)
        values = [signal.value_at(0.1 + i * 0.3) for i in range(8)]
        self.assertEqual(values, sorted(values))

    def test_square_two_levels(self):
        signal = Signal("holding_registers", 0, "square", period=2, low=0, high=500)
        values = {signal.value_at(t / 10) for t in range(20)}
        self.assertEqual(values, {0, 500})

    def test_bits_become_boolean(self):
        signal = Signal("coils", 0, "square", period=2, low=0, high=100)
        values = {signal.value_at(t / 10) for t in range(20)}
        self.assertEqual(values, {True, False})

    def test_store_applies_signal_on_read(self):
        store = DataStore(size=10)
        store.set_signal("input_registers", 0, "ramp", period=10, low=0, high=1000)
        first = store.read_input_registers(0, 1)[0]
        time.sleep(0.05)
        second = store.read_input_registers(0, 1)[0]
        self.assertNotEqual(first, second)

    def test_manual_set_takes_over(self):
        store = DataStore(size=10)
        store.set_signal("holding_registers", 0, "sine", period=1)
        store.set("holding_registers", 0, 42)
        self.assertEqual(store.read_holding_registers(0, 1)[0], 42)
        self.assertEqual(store.list_signals(), [])

    def test_clear_removes_signals(self):
        store = DataStore(size=10)
        store.set_signal("holding_registers", 0, "sine")
        store.set_signal("holding_registers", 1, "ramp")
        self.assertEqual(len(store.list_signals()), 2)
        store.clear()
        self.assertEqual(store.list_signals(), [])

    def test_unknown_mode(self):
        store = DataStore(size=10)
        with self.assertRaises(ValueError):
            store.set_signal("holding_registers", 0, "triangular")


class TestFaultInjection(unittest.TestCase):
    def core(self, **kwargs):
        return ModbusServer(DataStore(size=10), faults=FaultConfig(**kwargs))

    def test_default_inactive(self):
        self.assertFalse(FaultConfig().active)

    def test_drop_returns_none(self):
        core = self.core(drop_rate=1.0)
        self.assertIsNone(core.handle_pdu(bytes([0x03, 0, 0, 0, 1])))

    def test_exception_injection(self):
        core = self.core(exception_rate=1.0, exception_code=int(ExceptionCode.SERVER_DEVICE_BUSY))
        response = core.handle_pdu(bytes([0x03, 0, 0, 0, 1]))
        self.assertEqual(response[0], 0x83)
        self.assertEqual(response[1], 0x06)
        self.assertEqual(core.error_count, 1)

    def test_truncate(self):
        core = self.core(truncate_rate=1.0)
        response = core.handle_pdu(bytes([0x03, 0, 0, 0, 2]))
        self.assertEqual(len(response), 3)          # 完整是 6 字节

    def test_delay(self):
        core = self.core(delay=0.12)
        started = time.perf_counter()
        core.handle_pdu(bytes([0x03, 0, 0, 0, 1]))
        self.assertGreaterEqual(time.perf_counter() - started, 0.11)

    def test_normal_when_rates_zero(self):
        core = self.core()
        response = core.handle_pdu(bytes([0x03, 0, 0, 0, 1]))
        self.assertEqual(response[0], 0x03)

    def test_from_dict_clamps(self):
        cfg = FaultConfig.from_dict({"drop_rate": 5, "exception_rate": -3, "delay": -1})
        self.assertEqual(cfg.drop_rate, 1.0)
        self.assertEqual(cfg.exception_rate, 0.0)
        self.assertEqual(cfg.delay, 0.0)

    def test_from_dict_empty(self):
        self.assertFalse(FaultConfig.from_dict(None).active)

    def test_bad_crc_only_touches_last_byte(self):
        core = self.core(bad_crc_rate=1.0)
        frame = bytes([0x01, 0x03, 0x02, 0x00, 0x01, 0xAA, 0xBB])
        corrupted = core.maybe_corrupt(frame)
        self.assertEqual(corrupted[:-1], frame[:-1])
        self.assertNotEqual(corrupted[-1], frame[-1])


class TestDeviceIdentification(unittest.TestCase):
    def test_request_shape(self):
        self.assertEqual(build_device_id_request(1), bytes([0x2B, 0x0E, 0x01, 0x00]))

    def test_roundtrip_basic(self):
        identity = {0x00: "Acme", 0x01: "PM-100", 0x02: "V2.1"}
        pdu = build_device_id_response(identity, code=1)
        self.assertEqual(parse_device_id_response(pdu), identity)

    def test_regular_has_more_objects(self):
        identity = {0x00: "Acme", 0x01: "PM-100", 0x02: "V2.1",
                    0x04: "Power Meter", 0x05: "PM-100X"}
        parsed = parse_device_id_response(build_device_id_response(identity, code=2))
        self.assertEqual(parsed[0x04], "Power Meter")
        self.assertIn(0x05, parsed)

    def test_server_answers(self):
        core = ModbusServer(DataStore(size=10), identity={0x00: "Test"})
        response = core.handle_pdu(bytes([0x2B, 0x0E, 0x01, 0x00]))
        self.assertEqual(parse_device_id_response(response)[0x00], "Test")

    def test_wrong_mei_type(self):
        core = ModbusServer(DataStore(size=10))
        response = core.handle_pdu(bytes([0x2B, 0x0D, 0x01, 0x00]))
        self.assertEqual(response[0], 0xAB)          # 异常响应

    def test_malformed_response(self):
        with self.assertRaises(ModbusError):
            parse_device_id_response(b"\x2b\x0e\x01")
        with self.assertRaises(ModbusError):
            parse_device_id_response(b"\x2b\x0f\x01\x01\x00\x00\x00")

    def test_truncated_object(self):
        # 声称有 1 个对象，但数据不够
        with self.assertRaises(ModbusError):
            parse_device_id_response(b"\x2b\x0e\x01\x01\x00\x00\x01\x00\x10abc")


class TestWebFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = ModbusCore(os.path.join(cls.tmp.name, "cfg.json"))
        started = cls.app.slave_start(
            {"mode": "tcp", "host": "127.0.0.1", "port": 0, "unit": 1, "size": 500})
        cls.port = started["port"]
        cls.app.connect({"mode": "tcp", "host": "127.0.0.1",
                         "port": cls.port, "unit": 1, "timeout": 2, "retries": 0})
        for index, value in enumerate([2200, 501, 3, 0, 0x435C, 0x8000]):
            cls.app.slave_set("holding_registers", index, value)

    @classmethod
    def tearDownClass(cls):
        # 先把记录文件关掉，再停从站，最后留一点时间让 accept/服务线程真正退干净，
        # 否则下一个测试模块起从站时偶发抢不到响应
        cls.app.record_stop()
        cls.app.shutdown()
        time.sleep(0.3)
        cls.tmp.cleanup()

    def setUp(self):
        self.app.stop_poll()
        self.app.record_stop()
        self.app.slave_faults({"drop_rate": 0, "delay": 0, "exception_rate": 0,
                               "truncate_rate": 0, "bad_crc_rate": 0})

    # -- 字段视图 ----------------------------------------------------------

    def test_read_with_fields(self):
        data = self.app.read(3, 0, 6, fields=[
            {"offset": 0, "dtype": "uint16", "name": "电压", "scale": 0.1, "unit": "V"},
            {"offset": 1, "dtype": "uint16", "name": "频率", "scale": 0.1},
            {"offset": 4, "dtype": "float32", "name": "温度", "unit": "℃"},
        ])
        self.assertEqual([row["name"] for row in data["rows"]], ["电压", "频率", "温度"])
        self.assertEqual(data["rows"][0]["text"], "220")
        self.assertEqual(data["rows"][2]["text"], "220.5")
        self.assertEqual(data["rows"][0]["display_address"], 0)

    def test_fields_override_count(self):
        # 传 count=1，但字段需要 6 个寄存器，应该自动扩到 6
        data = self.app.read(3, 0, 1, fields=[{"offset": 4, "dtype": "float32"}])
        self.assertEqual(data["rows"][0]["text"], "220.5")

    def test_fields_ignored_for_bits(self):
        self.app.slave_set("coils", 0, True)
        data = self.app.read(1, 0, 1, fields=[{"offset": 0, "dtype": "float32"}])
        self.assertTrue(data["bits"])
        self.assertEqual(data["rows"][0]["text"], "1")

    # -- 多任务 ------------------------------------------------------------

    def test_parallel_tasks_independent(self):
        first = self.app.add_task(3, 0, 1, 0.1, label="电压")
        second = self.app.add_task(3, 4, 2, 0.1, dtype="float32", label="温度")
        time.sleep(0.5)
        tasks = {task["id"]: task for task in self.app.list_tasks()}
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[first["id"]]["latest"]["rows"][0]["text"], "2200")
        self.assertEqual(tasks[second["id"]]["latest"]["rows"][0]["text"], "220.5")
        self.assertEqual(tasks[first["id"]]["label"], "电压")

    def test_remove_task(self):
        task = self.app.add_task(3, 0, 1, 0.1)
        time.sleep(0.25)
        self.assertTrue(self.app.remove_task(task["id"])["ok"])
        self.assertEqual(self.app.list_tasks(), [])
        self.assertFalse(self.app.remove_task(task["id"])["ok"])

    def test_stop_all(self):
        self.app.add_task(3, 0, 1, 0.1)
        self.app.add_task(3, 1, 1, 0.1)
        time.sleep(0.25)
        self.assertEqual(self.app.stop_poll()["stopped"], 2)
        self.assertEqual(self.app.list_tasks(), [])

    # -- CSV ---------------------------------------------------------------

    def test_csv_record(self):
        path = os.path.join(self.tmp.name, "out.csv")
        self.app.record_start(path)
        self.app.add_task(3, 0, 2, 0.1)
        time.sleep(0.45)
        self.app.stop_poll()
        info = self.app.record_stop()
        self.assertGreaterEqual(info["rows"], 2)

        lines = Path(path).read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertTrue(lines[0].startswith("时间"))
        self.assertEqual(len(lines[0].split(",")), 3)     # 时间 + 两个寄存器
        self.assertGreaterEqual(len(lines) - 1, 2)
        self.assertRegex(lines[1].split(",")[0], r"^\d{4}-\d{2}-\d{2}")

    def test_record_status_reflects_state(self):
        self.assertFalse(self.app.record_status()["recording"])
        self.app.record_start(os.path.join(self.tmp.name, "s.csv"))
        self.assertTrue(self.app.record_status()["recording"])
        self.app.record_stop()
        self.assertFalse(self.app.record_status()["recording"])

    # -- 信号与故障 --------------------------------------------------------

    def test_signal_api(self):
        self.app.slave_signals({"action": "set", "area": "input_registers",
                                "index": 2, "mode": "ramp", "period": 5,
                                "low": 0, "high": 100})
        signals = self.app.slave_signals()["signals"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["mode"], "ramp")

        self.app.slave_signals({"action": "clear", "area": "input_registers", "index": 2})
        self.assertEqual(self.app.slave_signals()["signals"], [])

    def test_signal_rejects_bad_mode(self):
        with self.assertRaises(ValueError):
            self.app.slave_signals({"action": "set", "area": "coils",
                                    "index": 0, "mode": "triangle"})

    def test_fault_api(self):
        data = self.app.slave_faults({"delay": 0.05})
        self.assertTrue(data["active"])
        self.assertAlmostEqual(data["delay"], 0.05)
        started = time.perf_counter()
        self.app.read(3, 0, 1)
        self.assertGreaterEqual(time.perf_counter() - started, 0.045)

        data = self.app.slave_faults({"delay": 0})
        self.assertFalse(data["active"])

    # -- 设备识别 ----------------------------------------------------------

    def test_device_id(self):
        info = self.app.read_device_id()
        names = {item["name"]: item["value"] for item in info["items"]}
        self.assertIn("厂商名", names)
        self.assertTrue(names["厂商名"])

    def test_probe_device_id_is_silent(self):
        """探测失败不能留下任何痕迹：计数、日志、超时、重试次数都要复原。

        0x2B 是协议里的可选项，多数廉价设备不支持。若探测走常规超时并计入
        request/error，用户每次连接都会看到凭空的失败统计，还得等满一整个
        超时周期才看到第一屏数据。
        """
        class _Dead(Transport):
            def __init__(self):
                super().__init__(timeout=3.0)
                self._conn = False
                self.logs = []

            @property
            def connected(self):
                return self._conn

            def open(self):
                self._conn = True

            def close(self):
                self._conn = False

            def send(self, unit, pdu):
                pass

            def recv(self):
                raise TimeoutError("no answer")

        link = _Dead()
        client = ModbusClient(link, unit=1, retries=2, on_log=link.logs.append)
        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            client.probe_device_id(timeout=0.2)
        elapsed = time.perf_counter() - started
        self.assertEqual(link.timeout, 3.0)       # 超时设回去，后续读不受影响
        self.assertEqual(client.retries, 2)       # 重试次数设回去
        self.assertEqual((client.request_count, client.error_count), (0, 0))
        self.assertEqual(link.logs, [])           # 探测失败不进消息日志
        self.assertLess(elapsed, 1.0)             # 用的是短超时，不是 3 s


if __name__ == "__main__":
    unittest.main(verbosity=2)
