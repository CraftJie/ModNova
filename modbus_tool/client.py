"""主站（Client / Master）。

对外提供"读什么、写什么"的语义化方法，内部统一走 请求打包 → 发送 →
接收 → 响应解析 这条流水线。

重试策略是有讲究的：超时和链路错误会重试（现场干扰多、走线松），
但**从站明确返回的异常码不重试** —— 人家已经告诉你地址越界了，
再问十遍答案还是一样，重试只会拖慢界面。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .pdu import (
    FC,
    ModbusError,
    Request,
    Response,
    build_device_id_request,
    pack_request,
    parse_device_id_response,
    read_limit,
    unpack_response,
)
from .transport import READ_LIKE_FC, Transport, TransportError


def _as_register_int(value) -> int:
    """寄存器值必须是整数。

    GUI / CLI / core 三条路径都先经 codec.encode 编码成整数寄存器序列，
    这里拦的是绕过编码直接调 API 的用法：`int(220.5)` 会静默截断成 220，
    等于把错的数据悄悄写到设备上 —— 宁可报错，也不要静默写错半个字。
    整数值的 float（5.0）放行，bool 按 0/1 处理。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(
        f"寄存器值必须是整数，收到 {value!r}；"
        "物理量请先用 codec.encode 编码成寄存器序列")


class ModbusClient:
    def __init__(
        self,
        transport: Transport,
        unit: int = 1,
        retries: int = 1,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.unit = int(unit)
        self.retries = max(0, int(retries))
        self.on_log = on_log
        # RLock：probe 需要持锁改站号，而 probe 内部的请求又要走 _exchange 的锁
        self._lock = threading.RLock()
        # 计数器有独立的锁：成功路径的累加发生在传输锁之外，
        # 两个线程同时成功时 `+=` 在字节码层面不是原子的
        self._stat_lock = threading.Lock()
        self.request_count = 0
        self.error_count = 0
        self.last_latency_ms: float | None = None

    # -- 底层事务 ----------------------------------------------------------

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _bump(self, counter: str) -> None:
        """线程安全地累加计数器。

        成功路径的累加发生在传输锁之外，两个线程同时成功时 `+=` 在
        字节码层面不是原子的，必须进 `_stat_lock`。
        显式分支而不是 setattr/getattr 拼属性名：IDE 跳转和重构工具都跟得住。
        """
        with self._stat_lock:
            if counter == "request_count":
                self.request_count += 1
            else:
                self.error_count += 1

    def _exchange(self, pdu: bytes) -> bytes:
        """发一帧收一帧，带重试和站号校验，返回原始响应 PDU。"""
        last_error: Exception | None = None
        attempts_left = max(0, int(self.retries)) + 1
        link_retried = False

        while attempts_left > 0:
            attempts_left -= 1
            started = time.perf_counter()
            try:
                with self._lock:
                    if not self.transport.connected:
                        self.transport.open()
                    self.transport.send(self.unit, pdu)
                    unit_id, response_pdu = self.transport.recv()
                if unit_id not in (self.unit, 0):
                    raise TransportError(
                        f"响应的站地址 {unit_id} 与请求的 {self.unit} 不符"
                    )
                self.last_latency_ms = (time.perf_counter() - started) * 1000
                self._bump("request_count")
                return response_pdu
            except ModbusError:
                # 从站的明确答复，不重试，直接上报
                self._bump("request_count")
                self._bump("error_count")
                raise
            except (TimeoutError, TransportError) as exc:
                last_error = exc
                self._bump("error_count")
                if attempts_left > 0:
                    self._log(f"第 {self.retries - attempts_left + 1} 次尝试失败（{exc}），重试中")
                    continue
                # 从站重启之后，socket 还「在」但对端已经关了，第一次请求会撞在
                # 一条死链路上。读请求多给一次机会：自动重连再发一遍，用户不必
                # 再点一次按钮。
                # 写请求**不重试** —— 上一帧可能已经写进去了，重发会写两遍。
                if (not link_retried and pdu and pdu[0] in READ_LIKE_FC
                        and not self.transport.connected):
                    link_retried = True
                    attempts_left = 1
                    self._log("链路已断开，自动重连后重试一次")
                    continue
                self._log(f"请求失败：{exc}")
                raise

        raise last_error if last_error else TransportError("请求失败")

    def _execute(self, request: Request) -> Response:
        pdu = self._exchange(pack_request(request))
        try:
            return unpack_response(pdu, request)
        except ModbusError:
            self._bump("error_count")
            raise

    def read_device_id(self, code: int = 1, object_id: int = 0) -> dict[int, str]:
        """FC 0x2B 读设备标识：厂商名、产品代码、版本号。

        响应是变长对象列表，不走 Request/Response 那套寄存器结构，
        所以直接用原始 PDU 收发。
        """
        response = self._exchange(build_device_id_request(code, object_id))
        if response and response[0] & 0x80:
            self._bump("error_count")
            raise ModbusError(response[1], "从站拒绝读设备标识")
        return parse_device_id_response(response)

    def probe_device_id(self, timeout: float = 0.4) -> dict[int, str]:
        """FC 0x2B 的一次性探测：短超时、不重试、不计数、不写日志。

        0x2B 在协议里是可选项，廉价的变频器/温控表多半直接回异常码，
        或者干脆不应答。连接成功时「对面是谁」值得顺手问一句，但用常规
        超时去问，每次连接都要白白等满一整个超时周期 —— 那等于拿所有人
        的连接速度去换少数设备的一个便利。这里压成单帧、单次、短超时：
        答就答，不答就当没问过，由调用方静默处理。

        失败同样不进 request/error 计数：用户没发这个请求，统计里凭空
        多一次失败只会让人回头翻自己刚才点错了什么。
        """
        old_timeout = self.transport.timeout
        old_retries = self.retries
        old_log = self.on_log
        before = (self.request_count, self.error_count, self.last_latency_ms)
        self.transport.timeout = min(float(old_timeout), float(timeout))
        self.retries = 0
        # 探测的失败不该出现在消息日志里：那是内部行为，不是用户动作
        self.on_log = None
        try:
            return self.read_device_id()
        except Exception:
            with self._stat_lock:
                self.request_count, self.error_count = before[0], before[1]
            self.last_latency_ms = before[2]
            raise
        finally:
            self.transport.timeout = old_timeout
            self.retries = old_retries
            self.on_log = old_log

    # -- 读 ----------------------------------------------------------------

    def read(self, function: int, address: int, count: int = 1) -> list:
        fc = int(function)
        limit = read_limit(fc)
        if limit is None:
            raise ValueError(f"功能码 0x{fc:02X} 不是读操作")
        if not 1 <= count <= limit:
            raise ValueError(f"读取数量必须在 1~{limit} 之间")
        request = Request(fc, address, count)
        response = self._execute(request)
        self._log(
            f"读 FC{fc:02d} 地址 {address} 数量 {count} → "
            f"{self._format_values(response.values)}"
        )
        return response.values

    def read_coils(self, address: int, count: int = 1) -> list[bool]:
        return self.read(FC.READ_COILS, address, count)

    def read_discrete_inputs(self, address: int, count: int = 1) -> list[bool]:
        return self.read(FC.READ_DISCRETE_INPUTS, address, count)

    def read_holding_registers(self, address: int, count: int = 1) -> list[int]:
        return self.read(FC.READ_HOLDING_REGISTERS, address, count)

    def read_input_registers(self, address: int, count: int = 1) -> list[int]:
        return self.read(FC.READ_INPUT_REGISTERS, address, count)

    # -- 写 ----------------------------------------------------------------

    def write(self, function: int, address: int, values: list) -> Response:
        """写操作。values 必须是寄存器整数序列（线圈为布尔）——

        物理量（220.5 ℃、float32）请先经 codec.encode 编码；这里对
        带小数的值直接报错而不是静默截断（见 _as_register_int）。
        """
        fc = int(function)
        if fc in (FC.WRITE_SINGLE_COIL, FC.WRITE_SINGLE_REGISTER):
            if len(values) != 1:
                raise ValueError("单点写入只接受一个值")
            if fc == FC.WRITE_SINGLE_COIL:
                values = [bool(values[0])]
            else:
                values = [_as_register_int(values[0])]
            request = Request(fc, address, 1, list(values))
        elif fc == FC.WRITE_MULTIPLE_COILS:
            request = Request(fc, address, len(values), [bool(v) for v in values])
        elif fc == FC.WRITE_MULTIPLE_REGISTERS:
            request = Request(fc, address, len(values),
                              [_as_register_int(v) for v in values])
        elif fc == FC.MASK_WRITE:
            if len(values) != 2:
                raise ValueError("掩码写需要 AND 和 OR 两个掩码")
            request = Request(fc, address, 0,
                              [_as_register_int(values[0]), _as_register_int(values[1])])
        else:
            raise ValueError(f"功能码 0x{fc:02X} 不是写操作")

        response = self._execute(request)
        self._log(f"写 FC{fc:02d} 地址 {address} ← {self._format_values(values)}")
        return response

    def write_coil(self, address: int, value: bool) -> Response:
        return self.write(FC.WRITE_SINGLE_COIL, address, [value])

    def write_register(self, address: int, value: int) -> Response:
        return self.write(FC.WRITE_SINGLE_REGISTER, address, [value])

    def write_coils(self, address: int, values: list[bool]) -> Response:
        return self.write(FC.WRITE_MULTIPLE_COILS, address, values)

    def write_registers(self, address: int, values: list[int]) -> Response:
        return self.write(FC.WRITE_MULTIPLE_REGISTERS, address, values)

    def mask_write(self, address: int, and_mask: int, or_mask: int) -> Response:
        """FC22 掩码写：只改一个寄存器的某几位，不惊动其他位。

        and_mask 为 1 的位保留原值，为 0 的位被 or_mask 的对应位覆盖。
        例：把低 4 位置 1 而其余位不动 → mask_write(addr, 0xFFF0, 0x000F)。
        """
        return self.write(FC.MASK_WRITE, address, [and_mask, or_mask])

    # -- 杂项 --------------------------------------------------------------

    def probe(self, unit: int | None = None) -> bool:
        """试探某站是否在线：随便读一个保持寄存器，能应答即视为在线。

        必须自己把 `last_latency_ms` 清空再判：早期实现用
        「`last_latency_ms is not None`」区分「异常响应」和「没应答」，
        但这个值可能是**上一次成功请求**留下的 —— 从站已经下线，probe
        仍然返回 True。这种「靠调用方记得清理」的接口迟早出错
        （cmd_scan 恰好在外层清了，所以一直没暴露）。

        换站号的整个过程持锁：probe 中途若有并发请求进来，会把读请求
        发到刚被改掉的站号上（RLock 允许内部的 _exchange 重入同一把锁）。
        """
        with self._lock:
            original = self.unit
            if unit is not None:
                self.unit = int(unit)
            self.last_latency_ms = None
            try:
                self.read_holding_registers(0, 1)
                return True
            except ModbusError:
                # 从站回了一个明确的异常码：它活着，而且在说话
                return True
            except (TimeoutError, TransportError):
                return False
            finally:
                self.unit = original

    @staticmethod
    def _format_values(values: list) -> str:
        if not values:
            return "（空）"
        if isinstance(values[0], bool):
            head = "".join("1" if v else "0" for v in values[:64])
            return head + (f" …共 {len(values)} 个" if len(values) > 64 else "")
        # 读 125 个寄存器时整行有 600 多个字符，真正要看的其实是前几个和总数
        if len(values) > 8:
            head = " ".join(str(v) for v in values[:8])
            return f"{head} … 共 {len(values)} 个"
        return " ".join(str(v) for v in values)

    def close(self) -> None:
        self.transport.close()
