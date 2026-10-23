"""传输层 —— 只关心"怎么把 PDU 送出去、怎么把 PDU 收回来"。

两种实现，接口一致：

    TcpTransport   MBAP 头 + PDU，走 socket
    RtuTransport   站地址 + PDU + CRC16，走串口

把外壳处理干净地隔离在这一层，上层的主站/从站完全感知不到差别。

两个工程细节值得说明：

1. TCP 粘包。Modbus TCP 的 MBAP 头里带 length 字段，所以先读 7 字节头、
   再按 length 精确读 body，几帧叠在一个 recv 里也不会错位。

2. RTU 帧边界。传统做法靠 3.5 字符静默间隔判定帧结束，但在 PC 上定时器
   精度不够（尤其 USB 转串口有几十毫秒抖动）。这里改用"按功能码推算帧长"：
   收到站地址和功能码就能唯一确定整帧长度，比等静默可靠得多。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from abc import ABC, abstractmethod

from .crc import append_crc, verify_crc

try:
    import serial
    from serial.tools import list_ports

    HAS_SERIAL = True
except ImportError:  # 没有 pyserial 时只损失 RTU 能力，TCP 照常可用
    serial = None
    list_ports = None
    HAS_SERIAL = False


class TransportError(Exception):
    """链路层错误：连不上、断开、帧格式非法。"""


def list_serial_ports() -> list[str]:
    if not HAS_SERIAL:
        return []
    return [p.device for p in list_ports.comports()]


# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------

class Transport(ABC):
    """一个请求-响应式的 Modbus 链路。"""

    label = "transport"

    def __init__(self, timeout: float = 1.0) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        # 报文探针：拿到的是**完整帧**（TCP 含 MBAP 头，RTU 含站地址和 CRC），
        # 排障时要看的正是这个原始形态
        self.tap = None

    def set_tap(self, callback) -> None:
        """callback(direction, unit, frame)，direction 为 "tx" 或 "rx"。"""
        self.tap = callback

    def _emit(self, direction: str, unit: int, frame: bytes) -> None:
        if self.tap is None:
            return
        try:
            self.tap(direction, unit, frame)
        except Exception:
            pass          # 探针自己的问题不该影响通信

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send(self, unit: int, pdu: bytes) -> None: ...

    @abstractmethod
    def recv(self) -> tuple[int, bytes]: ...

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc_info):
        self.close()


# --------------------------------------------------------------------------
# Modbus TCP
# --------------------------------------------------------------------------

class TcpTransport(Transport):
    def __init__(self, host: str = "127.0.0.1", port: int = 502, timeout: float = 3.0) -> None:
        super().__init__(timeout)
        self.host = host
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._tid = 0
        self.label = f"TCP {host}:{port}"

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise TransportError(f"无法连接 {self.host}:{self.port} —— {exc}") from exc
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_exact(self, size: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < size:
            try:
                chunk = self._sock.recv(size - len(buf))
            except socket.timeout as exc:
                raise TimeoutError(f"等待响应超时（{self.timeout}s）") from exc
            except OSError as exc:
                # 对端复位/中止（WinError 10053/10054 这类）。转成 TransportError，
                # 上层才会走重试与自动重连；否则原始 WinError 会直接冒到界面上。
                self.close()
                raise TransportError(f"连接被对端中断（{exc.strerror or exc}）") from exc
            if not chunk:
                # 对端已经走了，自己也收摊，否则下次 send 还会假成功
                self.close()
                raise TransportError("连接被对端关闭")
            buf += chunk
        return bytes(buf)

    def send(self, unit: int, pdu: bytes) -> None:
        if self._sock is None:
            raise TransportError("链路未打开")
        with self._lock:
            self._tid = (self._tid + 1) & 0xFFFF
            header = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit & 0xFF)
            try:
                self._sock.sendall(header + pdu)
            except OSError as exc:
                self.close()
                raise TransportError(f"发送失败 —— {exc}") from exc
            self._emit("tx", unit, header + pdu)

    def recv(self) -> tuple[int, bytes]:
        if self._sock is None:
            raise TransportError("链路未打开")
        with self._lock:
            deadline = time.monotonic() + self.timeout
            while True:
                header = self._recv_exact(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if pid != 0:
                    raise TransportError(f"MBAP 协议标识非零（{pid}），不是 Modbus TCP 应答")
                if not 1 <= length <= 254:
                    raise TransportError(f"MBAP 长度字段非法（{length}）")
                body = self._recv_exact(length - 1)
                if tid == self._tid:
                    self._emit("rx", unit, header + body)
                    return unit, body
                # 事务号不符，说明是上一条超时请求迟到的应答，丢弃后继续等
                if time.monotonic() > deadline:
                    raise TimeoutError("未收到事务号匹配的响应")


# --------------------------------------------------------------------------
# Modbus RTU
# --------------------------------------------------------------------------

READ_LIKE_FC = (0x01, 0x02, 0x03, 0x04, 0x17)


def _rtu_tail_length(fc: int) -> int:
    """已知 站地址+功能码 之后，本帧还剩多少字节（含 CRC）。

    读类功能码的剩余长度取决于紧随其后的字节数字段，由调用方另行处理；
    0x2B（设备标识）是变长对象列表，也由调用方逐段读。
    """
    if fc & 0x80:
        return 3                      # 异常码 + CRC
    if fc in (0x05, 0x06, 0x0F, 0x10):
        return 6                      # 地址 + 数量/值 + CRC
    raise TransportError(
        f"无法推算功能码 0x{fc:02X} 的帧长 —— RTU 链路不支持该功能码")


class RtuTransport(Transport):
    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        timeout: float = 1.0,
    ) -> None:
        super().__init__(timeout)
        if not HAS_SERIAL:
            raise TransportError("RTU 需要 pyserial：pip install pyserial")
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self._ser = None
        self.label = f"RTU {port}@{baudrate},{bytesize}{parity}{int(stopbits)}"

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        if self.connected:
            return
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
        except Exception as exc:  # pyserial 抛的异常类型较杂，统一收口
            self._ser = None
            raise TransportError(f"打开串口 {self.port} 失败 —— {exc}") from exc

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _read_exact(self, size: int) -> bytes:
        assert self._ser is not None
        buf = bytearray()
        while len(buf) < size:
            chunk = self._ser.read(size - len(buf))
            if not chunk:
                raise TimeoutError(f"串口读取超时（{self.timeout}s）")
            buf += chunk
        return bytes(buf)

    def send(self, unit: int, pdu: bytes) -> None:
        if self._ser is None:
            raise TransportError("链路未打开")
        with self._lock:
            frame = append_crc(bytes([unit & 0xFF]) + pdu)
            try:
                self._ser.reset_input_buffer()
                self._ser.write(frame)
                self._ser.flush()
            except Exception as exc:
                self.close()
                raise TransportError(f"串口写入失败 —— {exc}") from exc
            self._emit("tx", unit, frame)

    def _read_device_id_frame(self, head: bytes) -> bytes:
        """FC 0x2B 的应答是变长对象列表：MEI/读码/一致性/后续标志/下一个对象/对象数，
        然后每个对象是「对象号 + 长度 + 内容」。逐段读到 CRC 为止。

        注意对象数在头之后第 6 个字节（前面 5 个是 mei/readcode/conformity/
        more/next），少算一个字节就会把 `next` 当对象数：0 个对象 → 提前读 CRC，
        于是整帧错位、报 CRC 失败。
        """
        body = bytearray(self._read_exact(6))
        count = body[5]
        for _ in range(count):
            item_head = self._read_exact(2)
            body += item_head
            body += self._read_exact(item_head[1])
        return head + bytes(body) + self._read_exact(2)

    def recv(self) -> tuple[int, bytes]:
        if self._ser is None:
            raise TransportError("链路未打开")
        with self._lock:
            head = self._read_exact(2)
            unit, fc = head[0], head[1]
            if fc in READ_LIKE_FC and not fc & 0x80:
                byte_count = self._read_exact(1)[0]
                frame = head + bytes([byte_count]) + self._read_exact(byte_count + 2)
            elif fc == 0x2B and not fc & 0x80:
                frame = self._read_device_id_frame(head)
            else:
                frame = head + self._read_exact(_rtu_tail_length(fc))
            if not verify_crc(frame):
                self._emit("rx", unit, frame)     # 坏帧恰恰最需要留证据
                raise TransportError("CRC 校验失败，帧可能被干扰")
            self._emit("rx", unit, frame)
            return unit, frame[1:-2]
