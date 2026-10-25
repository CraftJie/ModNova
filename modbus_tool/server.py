"""从站（Server / Slave）。

结构上跟主站对称：一个与传输无关的协议核心 ModbusServer，外面套两个壳——
TcpServer 和 RtuServer。核心只做一件事：吃进 PDU，吐出 PDU。

把"核心"独立出来的好处很直接：单元测试里不需要开任何端口就能把
全部功能码和异常路径跑一遍。
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from dataclasses import dataclass

from . import __version__ as _VERSION
from .crc import append_crc, verify_crc
from .datastore import DataStore
from .pdu import (
    FC,
    MAX_RW_WRITE_REGS,
    READ_LIMIT,
    WRITE_LIMIT,
    ExceptionCode,
    ModbusError,
    Request,
    Response,
    build_device_id_response,
    pack_exception,
    pack_response,
    unpack_request,
)
from .transport import HAS_SERIAL, TransportError

if HAS_SERIAL:
    import serial


# 从站默认对外报的身份信息。用 0x2B 能读出来，联调时确认对面是不是自己的模拟器。
# 版本号取包版本，别在这里另写一个（以前是 V1.3，而包根是 1.4.0）。
DEFAULT_IDENTITY = {
    0x00: "ModNova",
    0x01: "SIM",
    0x02: f"V{_VERSION}",
}


@dataclass
class FaultConfig:
    """故障注入。

    用途不是让通信更可靠，恰恰相反 —— 是**故意让它出错**，
    用来验证自己写的上位机在超时、异常、坏帧面前会不会崩、会不会误判。
    真实设备出问题时往往不给你机会复现，这里可以把问题按需造出来。
    """

    drop_rate: float = 0.0          # 概率性完全不响应（最像真的掉线）
    delay: float = 0.0              # 每次响应前延迟多少秒
    exception_rate: float = 0.0     # 概率性返回异常码
    exception_code: int = 4         # 用哪个异常码（4 = 从站设备故障）
    truncate_rate: float = 0.0      # 概率性截断响应帧
    bad_crc_rate: float = 0.0       # RTU 专属：概率性破坏 CRC

    def as_dict(self) -> dict:
        return {
            "drop_rate": self.drop_rate,
            "delay": self.delay,
            "exception_rate": self.exception_rate,
            "exception_code": self.exception_code,
            "truncate_rate": self.truncate_rate,
            "bad_crc_rate": self.bad_crc_rate,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> FaultConfig:
        data = data or {}

        def clamp(v) -> float:
            return min(1.0, max(0.0, float(v)))

        # 异常码是帧里的一个字节。配成 300 会让 pack_exception 抛 struct.error，
        # 而那一行在从站的 try 之外 —— 每注入一次异常就静默断一条连接。
        code = int(data.get("exception_code", 4))
        if not 1 <= code <= 0xFF:
            code = 4
        return cls(
            drop_rate=clamp(data.get("drop_rate", 0.0)),
            delay=max(0.0, float(data.get("delay", 0.0))),
            exception_rate=clamp(data.get("exception_rate", 0.0)),
            exception_code=code,
            truncate_rate=clamp(data.get("truncate_rate", 0.0)),
            bad_crc_rate=clamp(data.get("bad_crc_rate", 0.0)),
        )

    @property
    def active(self) -> bool:
        return any((self.drop_rate, self.delay, self.exception_rate,
                    self.truncate_rate, self.bad_crc_rate))


class ModbusServer:
    """协议核心：请求分发 + 异常响应生成。"""

    def __init__(self, store: DataStore | None = None, unit: int = 1,
                 identity: dict[int, str] | None = None,
                 faults: FaultConfig | None = None) -> None:
        self.store = store if store is not None else DataStore()
        self.unit = int(unit)
        self.identity = dict(identity or DEFAULT_IDENTITY)
        self.faults = faults or FaultConfig()
        self.request_count = 0
        self.error_count = 0
        self.last_request = ""
        self.last_error = ""       # 最近一次「意外异常」的原文，别让兜底把缺陷吞掉
        self._lock = threading.Lock()

    def handle_pdu(self, pdu: bytes) -> bytes | None:
        """输入一个请求 PDU，返回响应 PDU。

        返回 None 表示「这次不回」—— 故障注入里的不响应模式，
        真实设备掉线时就是这个表现：帧发出去了，永远等不到回应。
        """
        with self._lock:
            self.request_count += 1
        if not pdu:
            return pack_exception(0, ExceptionCode.ILLEGAL_FUNCTION)

        faults = self.faults
        if faults.drop_rate and random.random() < faults.drop_rate:
            self.last_request = "（丢弃，未响应）"
            return None

        if faults.exception_rate and random.random() < faults.exception_rate:
            with self._lock:
                self.error_count += 1
            self.last_request = f"（注入异常 0x{faults.exception_code:02X}）"
            return pack_exception(pdu[0], faults.exception_code)

        try:
            request = unpack_request(pdu)
            self.last_request = self._describe(request)
            result = self.dispatch(request)
            response = result if isinstance(result, bytes) else pack_response(result)
        except ModbusError as exc:
            with self._lock:
                self.error_count += 1
            return pack_exception(pdu[0], exc.code)
        except Exception as exc:  # 数据模型里的意外错误，按设备故障上报，别让从站线程死掉
            with self._lock:
                self.error_count += 1
                # 按设备故障回码是对的（从站不能因为一个坏请求就断线），
                # 但原因必须留下来 —— 否则「参数校验漏了」这类真实缺陷
                # 会一直伪装成「设备故障」，谁也不知道。
                self.last_error = f"{type(exc).__name__}: {exc}"
            return pack_exception(pdu[0], ExceptionCode.SERVER_DEVICE_FAILURE)

        if faults.delay:
            time.sleep(faults.delay)
        if faults.truncate_rate and random.random() < faults.truncate_rate:
            response = response[:max(1, len(response) // 2)]
        return response

    def maybe_corrupt(self, frame: bytes) -> bytes:
        """RTU 壳在发出前调用，按概率故意破坏 CRC。"""
        if self.faults.bad_crc_rate and random.random() < self.faults.bad_crc_rate:
            return frame[:-1] + bytes([frame[-1] ^ 0xFF])
        return frame

    @staticmethod
    def _validate(request: Request) -> None:
        """按功能码校验数量，越界回 ILLEGAL_DATA_VALUE。

        不能在客户端校验完就当从站不用校验：第三方主站发 200 个寄存器过来，
        `pack_response` 的字节数字段（1 字节）装不下 400，于是抛 struct.error、
        被兜底成 0x04「设备故障」—— 报错码是错的，主站会去重启设备，
        而真实原因是请求参数非法。
        """
        fc = int(request.function)
        if fc in READ_LIMIT:
            limit = READ_LIMIT[fc]
            if not 1 <= request.count <= limit:
                raise ModbusError(
                    ExceptionCode.ILLEGAL_DATA_VALUE,
                    f"数量 {request.count} 超出 1~{limit}")
            return
        if fc in (FC.WRITE_MULTIPLE_COILS, FC.WRITE_MULTIPLE_REGISTERS):
            limit = WRITE_LIMIT[fc]
            if not 1 <= len(request.values) <= limit:
                raise ModbusError(
                    ExceptionCode.ILLEGAL_DATA_VALUE,
                    f"数量 {len(request.values)} 超出 1~{limit}")
            return
        if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
            if not 1 <= request.count <= 125:
                raise ModbusError(
                    ExceptionCode.ILLEGAL_DATA_VALUE, f"读数量 {request.count} 超出 1~125")
            if not 1 <= len(request.values) <= MAX_RW_WRITE_REGS:
                raise ModbusError(
                    ExceptionCode.ILLEGAL_DATA_VALUE,
                    f"写数量 {len(request.values)} 超出 1~{MAX_RW_WRITE_REGS}")

    def dispatch(self, request: Request) -> Response | bytes:
        """常规功能码返回 Response，0x2B 这种变长结构直接返回 PDU 字节。"""
        fc = int(request.function)
        store = self.store
        self._validate(request)

        if fc == FC.READ_COILS:
            return Response(fc, store.read_coils(request.address, request.count))
        if fc == FC.READ_DISCRETE_INPUTS:
            return Response(fc, store.read_discrete_inputs(request.address, request.count))
        if fc == FC.READ_HOLDING_REGISTERS:
            return Response(fc, store.read_holding_registers(request.address, request.count))
        if fc == FC.READ_INPUT_REGISTERS:
            return Response(fc, store.read_input_registers(request.address, request.count))

        if fc == FC.WRITE_SINGLE_COIL:
            raw = request.values[0]
            if raw not in (0x0000, 0xFF00):
                raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, f"线圈值 0x{raw:04X} 非法")
            value = raw == 0xFF00
            store.write_coils(request.address, [value])
            return Response(fc, [value], request.address, 1)

        if fc == FC.WRITE_SINGLE_REGISTER:
            value = request.values[0]
            store.write_holding_registers(request.address, [value])
            return Response(fc, [value], request.address, 1)

        if fc == FC.WRITE_MULTIPLE_COILS:
            store.write_coils(request.address, request.values)
            return Response(fc, [], request.address, len(request.values))

        if fc == FC.WRITE_MULTIPLE_REGISTERS:
            store.write_holding_registers(request.address, request.values)
            return Response(fc, [], request.address, len(request.values))

        if fc == FC.MASK_WRITE:
            and_mask, or_mask = request.values
            store.mask_write_holding(request.address, and_mask, or_mask)
            return Response(fc, [and_mask, or_mask], request.address, 0)

        if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
            # 读写复合必须原子：两次分别拿 DataStore 锁的话，并发主站会在
            # 写和读之间插入自己的写操作，本主站读到的就不是刚写的值
            read_values = store.rw_holding_registers(
                request.write_address, request.values,
                request.address, request.count)
            return Response(fc, read_values)

        if fc == FC.READ_DEVICE_IDENTIFICATION:
            # 响应是变长的对象列表，不是寄存器数组，所以直接吐原始 PDU
            code = request.values[1] if len(request.values) > 1 else 1
            return build_device_id_response(self.identity, code)

        raise ModbusError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持功能码 0x{fc:02X}")

    @staticmethod
    def _describe(request: Request) -> str:
        fc = int(request.function)
        if fc in (FC.WRITE_SINGLE_COIL, FC.WRITE_SINGLE_REGISTER,
                  FC.WRITE_MULTIPLE_COILS, FC.WRITE_MULTIPLE_REGISTERS):
            return f"FC{fc:02d} 写 地址{request.address} 数量{len(request.values)}"
        if fc == FC.MASK_WRITE:
            if len(request.values) >= 2:
                return (f"FC22 掩码写 地址{request.address} "
                        f"AND 0x{request.values[0]:04X} OR 0x{request.values[1]:04X}")
            return f"FC22 掩码写 地址{request.address}"
        if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
            return f"FC23 读{request.address}x{request.count} 写{request.write_address}x{len(request.values)}"
        return f"FC{fc:02d} 读 地址{request.address} 数量{request.count}"


# --------------------------------------------------------------------------
# TCP 从站
# --------------------------------------------------------------------------

class TcpServer:
    def __init__(
        self,
        # 默认只听本机：Modbus 没有鉴权，绑 0.0.0.0 等于把一块可读写的内存
        # 放到整个网段上。要联调远端设备必须显式传入 0.0.0.0。
        host: str = "127.0.0.1",
        port: int = 502,
        store: DataStore | None = None,
        unit: int = 1,
        on_log=None,
        identity: dict[int, str] | None = None,
        faults: FaultConfig | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.core = ModbusServer(store, unit, identity, faults)
        self.on_log = on_log
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._running = threading.Event()
        # 每个连接一个线程，不封顶的话局域网里谁都能开几百条把进程拖垮。
        # 8 条对调试场景绰绰有余（Modbus Poll + 自己的主站 + 几台上位机）。
        self.max_clients = 8

    @property
    def store(self) -> DataStore:
        return self.core.store

    @property
    def unit(self) -> int:
        return self.core.unit

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(8)
        except OSError as exc:
            sock.close()
            raise TransportError(f"监听 {self.host}:{self.port} 失败 —— {exc}") from exc
        sock.settimeout(0.3)
        self._sock = sock
        self.port = sock.getsockname()[1]          # 传 0 时拿到实际分配端口
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop, name="modbus-tcp-accept", daemon=True)
        self._thread.start()
        self._log(f"从站已启动，监听 {self.host}:{self.port}")

    def stop(self) -> None:
        if not self.running:
            return
        self._running.clear()
        for conn in list(self._clients):
            # 先 shutdown 再 close，才能把阻塞在 recv 上的服务线程叫醒
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._clients.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._log("从站已停止")

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        try:
            while self._running.is_set():
                try:
                    conn, address = self._sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(self._clients) >= self.max_clients:
                    # 拒绝时给个明确的日志：现场连不上要知道是「从站满了」，
                    # 而不是去查网络。
                    self._log(f"连接数已达上限 {self.max_clients}，拒绝 {address[0]}:{address[1]}")
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                conn.settimeout(None)      # Modbus TCP 是长连接，空闲是常态，不能按超时断
                # 半开连接（对端拔网线/断电，不发 FIN）会让服务线程永远阻塞在
                # recv 上，8 个槽被占满后新主站全部被拒。keepalive 让系统在
                # 空闲约 30 秒后开始探测，几十秒内把死连接收走。
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "SIO_KEEPALIVE_VALS"):       # Windows：默认要等 2 小时
                    try:
                        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 3_000))
                    except OSError:
                        pass
                elif hasattr(socket, "TCP_KEEPIDLE"):           # Linux/macOS
                    try:
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                    except OSError:
                        pass
                self._clients.add(conn)
                # 先把处理线程拉起来，再打日志。日志回调可能去拿调用方的锁，
                # 万一卡住也只是少一行记录，不会让这条连接没人应答。
                threading.Thread(
                    target=self._serve_client, args=(conn, address), daemon=True
                ).start()
                self._log(f"主站接入 {address[0]}:{address[1]}")
        finally:
            # 监听套接字异常关闭时循环会 break，但 running 标志还亮着 ——
            # 界面会一直显示「运行中」，实际已经不应答了。
            self._running.clear()

    def _recv_exact(self, conn: socket.socket, size: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < size:
            try:
                chunk = conn.recv(size - len(buf))
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _serve_client(self, conn: socket.socket, address) -> None:
        try:
            while self._running.is_set():
                header = self._recv_exact(conn, 7)
                if header is None:
                    break
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if pid != 0 or not 1 <= length <= 254:
                    self._log(f"丢弃非法 MBAP 头 pid={pid} len={length}")
                    break
                body = self._recv_exact(conn, length - 1)
                if body is None:
                    break
                # unit 为 0 表示本机不区分站号
                if self.core.unit != 0 and unit not in (self.core.unit, 0):
                    continue
                pdu_response = self.core.handle_pdu(body)
                if pdu_response is None:
                    continue      # 注入的不响应：这一帧不回，让主站自己超时
                conn.sendall(
                    struct.pack(">HHHB", tid, 0, len(pdu_response) + 1, unit & 0xFF) + pdu_response
                )
        except OSError:
            pass
        finally:
            self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self._log(f"主站断开 {address[0]}:{address[1]}")


# --------------------------------------------------------------------------
# RTU 从站
# --------------------------------------------------------------------------

class RtuServer:
    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        store: DataStore | None = None,
        unit: int = 1,
        on_log=None,
        identity: dict[int, str] | None = None,
        faults: FaultConfig | None = None,
    ) -> None:
        if not HAS_SERIAL:
            raise TransportError("RTU 需要 pyserial：pip install pyserial")
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.core = ModbusServer(store, unit, identity, faults)
        self.on_log = on_log
        self._ser = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    @property
    def store(self) -> DataStore:
        return self.core.store

    @property
    def unit(self) -> int:
        return self.core.unit

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def start(self) -> None:
        if self.running:
            return
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=0.1,
            )
        except Exception as exc:
            self._ser = None
            raise TransportError(f"打开串口 {self.port} 失败 —— {exc}") from exc
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="modbus-rtu-slave", daemon=True)
        self._thread.start()
        self._log(f"RTU 从站已启动 {self.port}@{self.baudrate}")

    def stop(self) -> None:
        if not self.running:
            return
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
        self._log("RTU 从站已停止")

    def _read_exact(self, size: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < size and self._running.is_set():
            chunk = self._ser.read(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf) if len(buf) == size else None

    def _resync(self, reason: str) -> None:
        """丢掉无法识别的帧并清空接收缓冲。

        只把已读的字节丢掉是不够的：未知功能码后面还跟着长度不明的数据，
        那些字节会被当成下一帧的帧头，之后每帧都错位，直到总线彻底静默。
        """
        self._log(f"丢弃无法识别的请求（{reason}），重新同步")
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

    def _read_request(self) -> bytes | None:
        try:
            return read_rtu_request(self._read_exact)
        except TransportError as exc:
            # 未知功能码后面跟着长度不明的数据，必须连缓冲一起清掉重新同步
            self._resync(str(exc))
            return None

    def _loop(self) -> None:
        try:
            while self._running.is_set():
                try:
                    frame = self._read_request()
                except OSError as exc:
                    # 串口被拔出/驱动报错时 pyserial 抛 SerialException（OSError
                    # 子类）。以前这里只捕 TransportError，读路径的异常会穿透
                    # _loop —— 线程带 traceback 死掉、不留日志，界面还停留在
                    # 「运行中」。收口成：记日志后退出循环，finally 清 running。
                    self._log(f"串口读取异常，从站线程退出：{exc}")
                    break
                if frame is None:
                    continue
                if len(frame) < 4 or not verify_crc(frame):
                    self._log("丢弃 CRC 错误的帧")
                    self._resync("CRC 校验失败")
                    continue
                unit = frame[0]
                # 非本站号的请求静默丢弃、不回帧：模拟器的既定行为 —— 总线上
                # 别的从站本来就不应答，主站按超时处理即可（需要网关语义的
                # 「回 GATEWAY_TARGET_DEVICE_FAILED」不在此列）。
                if self.core.unit != 0 and unit != self.core.unit:
                    continue
                response = self.core.handle_pdu(frame[1:-2])
                if response is None:
                    continue          # 注入的不响应
                try:
                    out = self.core.maybe_corrupt(append_crc(bytes([unit]) + response))
                    self._ser.write(out)
                    self._ser.flush()
                except Exception as exc:
                    self._log(f"串口写入失败 —— {exc}")
                    break
        finally:
            # 写失败 break 之后 running 还亮着，界面会显示「运行中」但已不应答
            self._running.clear()


# --------------------------------------------------------------------------
# RTU over TCP 从站（串口服务器 / DTU 透传模式的对面）
# --------------------------------------------------------------------------

class RtuOverTcpServer(TcpServer):
    """TCP 监听，但流里跑裸 RTU 帧（站地址 + PDU + CRC，无 MBAP）。

    用途：模拟串口服务器/DTU 背后挂的那台设备，验证上位机的透传链路。
    连接管理（监听、keepalive、连接数上限）继承自 TcpServer；
    只有请求读取和应答发送换成 RTU 帧。
    """

    def _serve_client(self, conn: socket.socket, address) -> None:
        try:
            while self._running.is_set():
                try:
                    frame = read_rtu_request(lambda n: self._recv_exact(conn, n))
                except TransportError as exc:
                    # 流错位（垃圾数据/半帧截断）在 TCP 上无法恢复，
                    # 断开让主站重连，比带着错位的流继续解析可靠
                    self._log(f"丢弃无法识别的请求（{exc}），断开连接")
                    break
                if frame is None:
                    break
                if len(frame) < 4 or not verify_crc(frame):
                    self._log("丢弃 CRC 错误的帧，断开连接")
                    break
                unit = frame[0]
                if self.core.unit != 0 and unit != self.core.unit:
                    continue
                response = self.core.handle_pdu(frame[1:-2])
                if response is None:
                    continue          # 注入的不响应：这一帧不回，让主站自己超时
                try:
                    out = self.core.maybe_corrupt(append_crc(bytes([unit]) + response))
                    conn.sendall(out)
                except OSError:
                    break
        finally:
            self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self._log(f"主站断开 {address[0]}:{address[1]}")


def create_server(mode: str, **kwargs):
    """按模式造一个从站，GUI 和 CLI 共用这个入口。"""
    mode = (mode or "").lower()
    if mode == "tcp":
        return TcpServer(**kwargs)
    if mode == "rtu":
        return RtuServer(**kwargs)
    if mode == "rtutcp":
        return RtuOverTcpServer(**kwargs)
    raise ValueError(f"未知模式 {mode}")


def read_rtu_request(read_exact):
    """读一个完整的 RTU 请求帧（含站地址与 CRC），按功能码推算帧长。

    `read_exact(size) -> bytes | None`：读不满返回 None（流中断）。
    未知功能码抛 TransportError —— 串口侧丢字节重新同步，TCP 侧直接断开：
    流已经错位，继续收只会把后面的帧全部拼错。

    串口从站（RtuServer）与 RTU over TCP 从站共用这一份，帧长推算
    才不会各自漂移。
    """
    head = read_exact(2)
    if head is None:
        return None
    fc = head[1]
    if fc in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        rest = read_exact(6)               # 地址 2 + 数量/值 2 + CRC 2
        return head + rest if rest else None
    if fc == 0x16:
        rest = read_exact(8)               # 地址 2 + AND 2 + OR 2 + CRC 2
        return head + rest if rest else None
    if fc in (0x0F, 0x10):
        mid = read_exact(5)                # 地址 2 + 数量 2 + 字节数 1
        if mid is None:
            return None
        payload = read_exact(mid[4] + 2)   # 数据 + CRC
        return head + mid + payload if payload else None
    if fc == 0x17:
        mid = read_exact(9)                # 读区 4 + 写区 4 + 字节数 1
        if mid is None:
            return None
        payload = read_exact(mid[8] + 2)
        return head + mid + payload if payload else None
    if fc == 0x2B:
        # 设备标识请求固定 5 字节：站号 + FC + MEI + 读码 + 对象号，再加 CRC。
        # 头已经读了 2 个字节，还剩 MEI/读码/对象号 + 2 字节 CRC = 5。
        rest = read_exact(5)
        return head + rest if rest else None
    raise TransportError(f"功能码 0x{fc:02X} 不是合法的 RTU 请求")
