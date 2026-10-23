"""RTU 功能测试（无需真实串口）。

用一对 socket 冒充串口（相当于一根 null-modem 线），把真正的
RtuTransport / RtuServer / ModbusClient 跑起来 —— 帧结构、CRC、站号过滤、
异常码这些都能验；只有电气层（波特率/校验位时序）需要真实硬件，这里不覆盖。

前置：需要一个装了 pyserial 的环境。
    pip install --target /tmp/pyser pyserial
    PYTHONPATH=/tmp/pyser <python> tests/rtu_full_test.py
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import serial  # noqa: E402  必须能导入，否则 HAS_SERIAL 为 False

from modbus_tool.client import ModbusClient
from modbus_tool.crc import append_crc
from modbus_tool.datastore import DataStore
from modbus_tool.pdu import ModbusError
from modbus_tool.server import RtuServer
from modbus_tool.transport import HAS_SERIAL, RtuTransport, TransportError


class FakeSerial:
    """把 socket 包装成 pyserial 的 Serial 接口。"""

    def __init__(self, sock: socket.socket, timeout: float = 1.0) -> None:
        self.sock = sock
        self.timeout = timeout
        self.is_open = True
        self.port = "FAKE"

    def read(self, size: int) -> bytes:
        self.sock.settimeout(self.timeout)
        try:
            return self.sock.recv(size)
        except socket.timeout:
            return b""

    def write(self, data: bytes) -> int:
        self.sock.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.sock.setblocking(False)
        try:
            while True:
                if not self.sock.recv(4096):
                    break
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.setblocking(True)

    def close(self) -> None:
        self.is_open = False
        try:
            self.sock.close()
        except OSError:
            pass


RESULTS: list[dict] = []


def case(name: str, expect: str, fn) -> None:
    started = time.perf_counter()
    try:
        actual, ok = fn()
    except Exception as exc:
        actual, ok = f"异常 {type(exc).__name__}: {exc}", False
    RESULTS.append({"name": name, "expect": expect, "actual": actual, "ok": bool(ok),
                    "ms": int((time.perf_counter() - started) * 1000)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<26} 期望：{expect}  实际：{actual}")


def make_pair() -> tuple[FakeSerial, FakeSerial]:
    left, right = socket.socketpair()
    return FakeSerial(left), FakeSerial(right)


def install_fake_serial(ports: dict[str, FakeSerial]) -> None:
    """把 serial.Serial 换成按端口名取假串口的工厂。"""
    serial.Serial = lambda port, **kwargs: ports[port]   # type: ignore[assignment]


def main() -> int:
    if not HAS_SERIAL:
        print("未安装 pyserial，RTU 无法测试")
        return 2

    master_end, slave_end = make_pair()
    install_fake_serial({"FAKE-M": master_end, "FAKE-S": slave_end})

    store = DataStore(size=500)
    store.write_holding_registers(0, [11, 22, 33, 44])
    store.write_coils(0, [True, False, True])
    server = RtuServer(port="FAKE-S", baudrate=9600, store=store, unit=1)
    server.start()
    time.sleep(0.2)

    transport = RtuTransport(port="FAKE-M", baudrate=9600, timeout=1.0)
    frames: list[tuple[str, bytes]] = []
    transport.set_tap(lambda direction, unit, frame: frames.append((direction, frame)))
    client = ModbusClient(transport, unit=1, retries=0)

    def read_regs():
        values = client.read_holding_registers(0, 4)
        return f"值={values}", values == [11, 22, 33, 44]

    case("RTU 读保持寄存器", "11,22,33,44", read_regs)

    def read_coils():
        values = client.read_coils(0, 3)
        return f"位={values}", values == [True, False, True]

    case("RTU 读线圈", "110", read_coils)

    def write_reg():
        client.write_register(10, 999)
        values = client.read_holding_registers(10, 1)
        return f"回读={values}", values == [999]

    case("RTU 写单寄存器", "写 999 回读 999", write_reg)

    def exception_code():
        try:
            client.read_holding_registers(495, 20)
        except ModbusError as exc:
            return f"异常码 0x{exc.code:02X}", exc.code == 2
        return "没有抛异常", False

    case("RTU 越界读", "异常码 0x02", exception_code)

    def crc_in_frames():
        tx = [f for d, f in frames if d == "tx"]
        rx = [f for d, f in frames if d == "rx"]
        if not tx or not rx:
            return f"TX={len(tx)} RX={len(rx)}", False
        last_tx, last_rx = tx[-1], rx[-1]
        return f"TX={last_tx.hex()} RX={last_rx[:6].hex()}…", \
            len(rx[0]) >= 5 and last_tx[-2:] == append_crc(last_tx[:-2])[-2:]

    case("RTU 报文带 CRC", "TX 帧 CRC 可复算", crc_in_frames)

    def wrong_unit():
        client.unit = 7
        try:
            client.read_holding_registers(0, 1)
        except TimeoutError:
            return "站号 7 无响应（超时）", True
        finally:
            client.unit = 1
        return "居然有响应", False

    case("RTU 站号过滤", "非本站地址不应答", wrong_unit)

    def bad_crc():
        # 另起一条链路，对端回一帧 CRC 错的响应
        left, right = socket.socketpair()
        install_fake_serial({"FAKE-BAD": FakeSerial(left, timeout=0.5)})

        def serve():
            request = right.recv(256)
            if not request:
                return
            unit, fc = request[0], request[1]
            body = bytes([unit, fc, 0x02, 0x00, 0x01])
            frame = bytearray(append_crc(body))
            frame[-1] ^= 0xFF                      # 故意打坏 CRC
            right.sendall(bytes(frame))

        threading.Thread(target=serve, daemon=True).start()
        bad = ModbusClient(RtuTransport(port="FAKE-BAD", timeout=0.5), unit=1, retries=0)
        try:
            bad.read_holding_registers(0, 1)
        except TransportError as exc:
            return f"{exc}", "CRC" in str(exc)
        finally:
            bad.close()
            right.close()
        return "没有识别出坏帧", False

    case("RTU 坏 CRC 检测", "报 CRC 校验失败", bad_crc)

    def device_identification():
        """0x2B 的应答是变长对象列表 —— 帧长没法靠功能码推算，得逐段读。"""
        info = client.read_device_id(1)
        vendor = info.get(0x00, "")
        return f"厂商={vendor!r} 对象数={len(info)}", vendor == "Modbus Tool" and len(info) >= 3

    case("RTU 设备标识 FC2B", "读到厂商名与版本", device_identification)

    def unknown_function_resync():
        """未知功能码后必须重新同步，否则后续帧全部错位。"""
        # 直接往从站的串口写一帧功能码 0x08（非法）+ 合法 CRC
        junk = append_crc(bytes([1, 0x08, 0x00, 0x00, 0x12, 0x34]))
        slave_end.write(junk)
        time.sleep(0.2)
        values = client.read_holding_registers(0, 2)
        return f"错帧后回读={values}", values == [11, 22]

    case("RTU 未知功能码后重同步", "丢帧后仍能正常读", unknown_function_resync)

    client.close()
    server.stop()

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r["ok"]]
    print("\n" + "=" * 70)
    print(f"合计 {total} 项，通过 {total - len(failed)} 项，失败 {len(failed)} 项")
    for row in failed:
        print(f"  - {row['name']}｜{row['actual']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
