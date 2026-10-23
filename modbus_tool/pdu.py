"""Modbus PDU 层 —— 与传输方式无关的应用数据单元。

PDU 里没有站地址、没有 CRC、也没有 MBAP 头，这些都是"外壳"，由传输层负责
加上或拆掉。所以同一套编解码可以同时服务 TCP 和 RTU，这也是本项目分层的
关键：换传输方式不用碰协议逻辑。

字节序统一大端；线圈打包低位在前（第一个线圈位于 byte0 的 bit0）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum


class FC(IntEnum):
    """功能码。只实现工程上真正用得着的，不做全集。"""

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    READ_WRITE_MULTIPLE_REGISTERS = 0x17
    READ_DEVICE_IDENTIFICATION = 0x2B


class ExceptionCode(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SERVER_DEVICE_FAILURE = 0x04
    ACKNOWLEDGE = 0x05
    SERVER_DEVICE_BUSY = 0x06
    MEMORY_PARITY_ERROR = 0x08
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_DEVICE_FAILED = 0x0B


class ModbusError(Exception):
    """从站返回的异常响应。

    只用于**线上**语义：读到从站的异常帧、或解析从站应答失败。
    本地参数不合法（地址越界、数量超限）请用 `ParameterError` ——
    两者混用会让界面把「我自己填错了」报成「从站拒绝了」，
    现场排查方向直接跑偏。
    """

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = int(code)
        self.detail = detail
        try:
            name = ExceptionCode(self.code).name
        except ValueError:
            name = f"UNKNOWN_0x{self.code:02X}"
        super().__init__(f"{name}(0x{self.code:02X}) {detail}".strip())


class ParameterError(ModbusError, ValueError):
    """本地参数不合法：请求根本没有发出去。

    同时是 `ValueError`，所以「用户输入有误 → 提示改参数」这条路径可以按
    ValueError 捕获；同时是 `ModbusError`，所以既有的
    `except ModbusError` 兜底与 `exc.code` 读取仍然成立（向后兼容）。
    """

    @property
    def message(self) -> str:
        """给人看的一句话，不带 `ILLEGAL_DATA_ADDRESS(0x02)` 这种码名。

        本地错误没有「从站回的异常码」这回事，把码名印出来只会让用户
        以为设备拒绝了请求。
        """
        return self.detail or str(self)


# 异常码 → 人话。界面、命令行、应用层都从这里取，免得各写一份还对不上。
EXCEPTION_TEXT = {
    ExceptionCode.ILLEGAL_FUNCTION: "从站不支持这个功能码",
    ExceptionCode.ILLEGAL_DATA_ADDRESS: "地址或数量超出从站范围",
    ExceptionCode.ILLEGAL_DATA_VALUE: "参数值不合法",
    ExceptionCode.SERVER_DEVICE_FAILURE: "从站设备故障",
    ExceptionCode.ACKNOWLEDGE: "从站已受理，需要轮询结果",
    ExceptionCode.SERVER_DEVICE_BUSY: "从站忙",
    ExceptionCode.MEMORY_PARITY_ERROR: "从站存储校验错误",
    ExceptionCode.GATEWAY_PATH_UNAVAILABLE: "网关路径不可用",
    ExceptionCode.GATEWAY_TARGET_DEVICE_FAILED: "网关目标无响应",
}


def describe_error(exc: BaseException) -> str:
    """把底层异常翻成能直接给人看的一句话。"""
    if isinstance(exc, ParameterError):
        # 本地参数错误先于 ModbusError 判断：它也是 ModbusError 的子类，
        # 但语义是「你填错了」，不是「从站拒绝了」。
        return exc.message
    if isinstance(exc, ModbusError):
        try:
            code = ExceptionCode(exc.code)
        except ValueError:
            return str(exc)
        return EXCEPTION_TEXT.get(code, str(exc))
    if isinstance(exc, TimeoutError):
        return "等待响应超时，检查链路和站地址"
    return str(exc)


# ---- 数量上限，取自 Modbus 应用协议规范 ----
MAX_READ_BITS = 2000
MAX_READ_REGS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGS = 123
MAX_RW_READ_REGS = 125
MAX_RW_WRITE_REGS = 121

READ_BITS_FC = (FC.READ_COILS, FC.READ_DISCRETE_INPUTS)
READ_REGS_FC = (FC.READ_HOLDING_REGISTERS, FC.READ_INPUT_REGISTERS)
WRITE_SINGLE_FC = (FC.WRITE_SINGLE_COIL, FC.WRITE_SINGLE_REGISTER)

# 各功能码单帧能带的最大数量。客户端打包要用它做前置校验，
# 从站分发也要用它拦超规请求 —— 只在一边校验的话，第三方主站发
# 200 个寄存器过来，从站会拼出一个 400 字节的应答（字节数字段存不下）。
READ_LIMIT = {
    FC.READ_COILS: MAX_READ_BITS,
    FC.READ_DISCRETE_INPUTS: MAX_READ_BITS,
    FC.READ_HOLDING_REGISTERS: MAX_READ_REGS,
    FC.READ_INPUT_REGISTERS: MAX_READ_REGS,
}

WRITE_LIMIT = {
    FC.WRITE_SINGLE_COIL: 1,
    FC.WRITE_SINGLE_REGISTER: 1,
    FC.WRITE_MULTIPLE_COILS: MAX_WRITE_BITS,
    FC.WRITE_MULTIPLE_REGISTERS: MAX_WRITE_REGS,
}


def read_limit(fc: int) -> int | None:
    """读类功能码的数量上限；不是读操作时返回 None。"""
    return READ_LIMIT.get(fc)


def write_limit(fc: int) -> int | None:
    """写类功能码的数量上限；不是写操作时返回 None。"""
    return WRITE_LIMIT.get(fc)


def limit_of(fc: int) -> int | None:
    """按功能码给出数量上限（读或写）。0x17 读写复合取读区上限。"""
    if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
        return MAX_RW_READ_REGS
    return READ_LIMIT.get(fc) or WRITE_LIMIT.get(fc)


@dataclass
class Request:
    """一个请求。读操作用 address/count；写操作用 address/values。

    0x17 读写复合：address/count 是读区，write_address/values 是写区。
    """

    function: int
    address: int = 0
    count: int = 0
    values: list = field(default_factory=list)
    write_address: int = 0


@dataclass
class Response:
    """一个响应。values 是读结果；写操作靠 address/count 回显。"""

    function: int
    values: list = field(default_factory=list)
    address: int = 0
    count: int = 0


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------

def _check_address(address: int) -> None:
    if not 0 <= address <= 0xFFFF:
        raise ParameterError(ExceptionCode.ILLEGAL_DATA_ADDRESS, f"地址 {address} 越界")


def _check_quantity(count: int, low: int, high: int) -> None:
    if not low <= count <= high:
        raise ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, f"数量 {count} 超出 {low}~{high}")


def _pack_bits(bits: list) -> bytes:
    buf = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            buf[i // 8] |= 1 << (i % 8)
    return bytes(buf)


def _unpack_bits(data: bytes, count: int) -> list[bool]:
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def _pack_regs(values: list) -> bytes:
    for v in values:
        if not 0 <= int(v) <= 0xFFFF:
            raise ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, f"寄存器值 {v} 超出 0~65535")
    return struct.pack(f">{len(values)}H", *[int(v) for v in values])


def _unpack_regs(data: bytes) -> list[int]:
    return list(struct.unpack(f">{len(data) // 2}H", data))


# --------------------------------------------------------------------------
# 请求编解码
# --------------------------------------------------------------------------

def describe_write_frame(frame: bytes) -> str:
    """把写请求帧读成「写了几个」。

    界面上「填了几个值」和「真正写了几个寄存器」不是一回事：一个 float32
    占 2 个寄存器、float64 占 4 个。报文里的数量字段才是发出去的事实。

    TCP 帧 = 7 字节 MBAP 头 + PDU；RTU 帧 = 站地址 + PDU + CRC。
    返回空串表示这不是写请求或帧不完整。
    """
    # 最短的写帧是 RTU 的 FC05/FC06：站号 + 功能码 + 地址 2 + 数据 2 + CRC 2 = 8 字节
    if len(frame) < 8:
        return ""
    # 光看「协议标识为 0」不够：RTU 帧的地址字段常常就是 0x0000，会撞上。
    # 再加一条长度校验 —— MBAP 的长度字段等于「本帧长度 - 6」。
    is_tcp = (frame[2:4] == b"\x00\x00"
              and int.from_bytes(frame[4:6], "big") == len(frame) - 6)
    pdu = frame[7:] if is_tcp else frame[1:]
    if len(pdu) < 5:
        return ""
    fc = pdu[0]
    address = int.from_bytes(pdu[1:3], "big")
    if fc == 0x10 and len(pdu) >= 7:
        count = int.from_bytes(pdu[3:5], "big")
        data_bytes = pdu[5]
        line = f"FC16 写多寄存器 · 数量 {count} 个寄存器（{data_bytes} 字节）· 起始 {address}"
        if data_bytes != count * 2:
            line += f" ⚠ 字节数应为 {count * 2}，报文结构可疑"
        return line
    if fc == 0x0F and len(pdu) >= 7:
        count = int.from_bytes(pdu[3:5], "big")
        return f"FC15 写多线圈 · 数量 {count} 个线圈（{pdu[5]} 字节）· 起始 {address}"
    if fc == 0x06:
        return f"FC06 写单寄存器 · 1 个寄存器（2 字节）· 地址 {address}"
    if fc == 0x05:
        return f"FC05 写单线圈 · 1 个线圈 · 地址 {address}"
    return ""


def _check_range(address: int, count: int) -> None:
    """起始地址 + 数量不能越过 0xFFFF。

    只校验 `address <= 0xFFFF` 是不够的：65535 起步读 10 个，最后一个地址
    落到 65544，协议里根本表示不出来。从站会回 ILLEGAL_DATA_ADDRESS，
    但主站本来就不该把这一帧发出去。
    """
    if count > 0 and address + count - 1 > 0xFFFF:
        raise ParameterError(
            ExceptionCode.ILLEGAL_DATA_ADDRESS,
            f"起始 {address} 读/写 {count} 个会越过 65535")


def pack_request(req: Request) -> bytes:
    fc = int(req.function)

    if fc in READ_BITS_FC or fc in READ_REGS_FC:
        _check_address(req.address)
        _check_quantity(req.count, 1, READ_LIMIT[fc])
        _check_range(req.address, req.count)
        return struct.pack(">BHH", fc, req.address, req.count)

    if fc == FC.WRITE_SINGLE_COIL:
        _check_address(req.address)
        if not req.values:
            raise ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, "缺少线圈值")
        return struct.pack(">BHH", fc, req.address, 0xFF00 if req.values[0] else 0x0000)

    if fc == FC.WRITE_SINGLE_REGISTER:
        _check_address(req.address)
        if not req.values:
            raise ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, "缺少寄存器值")
        value = int(req.values[0])
        if not 0 <= value <= 0xFFFF:
            raise ParameterError(ExceptionCode.ILLEGAL_DATA_VALUE, f"寄存器值 {value} 超出 0~65535")
        return struct.pack(">BHH", fc, req.address, value)

    if fc == FC.WRITE_MULTIPLE_COILS:
        _check_address(req.address)
        _check_quantity(len(req.values), 1, MAX_WRITE_BITS)
        _check_range(req.address, len(req.values))
        payload = _pack_bits(req.values)
        return struct.pack(">BHHB", fc, req.address, len(req.values), len(payload)) + payload

    if fc == FC.WRITE_MULTIPLE_REGISTERS:
        _check_address(req.address)
        _check_quantity(len(req.values), 1, MAX_WRITE_REGS)
        _check_range(req.address, len(req.values))
        payload = _pack_regs(req.values)
        return struct.pack(">BHHB", fc, req.address, len(req.values), len(payload)) + payload

    if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
        _check_address(req.address)
        _check_address(req.write_address)
        _check_quantity(req.count, 1, MAX_RW_READ_REGS)
        _check_quantity(len(req.values), 1, MAX_RW_WRITE_REGS)
        _check_range(req.address, req.count)
        _check_range(req.write_address, len(req.values))
        payload = _pack_regs(req.values)
        return struct.pack(
            ">BHHHHB", fc, req.address, req.count,
            req.write_address, len(req.values), len(payload),
        ) + payload

    raise ParameterError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持功能码 0x{fc:02X}")


def unpack_request(pdu: bytes) -> Request:
    if len(pdu) < 2:
        raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "PDU 过短")
    fc = pdu[0]

    if fc in READ_BITS_FC or fc in READ_REGS_FC or fc in WRITE_SINGLE_FC:
        if len(pdu) != 5:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "请求长度错误")
        _, address, value = struct.unpack(">BHH", pdu)
        if fc in WRITE_SINGLE_FC:
            return Request(fc, address, 1, [value])
        return Request(fc, address, value)

    if fc == FC.WRITE_MULTIPLE_COILS:
        if len(pdu) < 6:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "请求长度错误")
        _, address, count, byte_count = struct.unpack(">BHHB", pdu[:6])
        body = pdu[6:]
        if byte_count != (count + 7) // 8 or len(body) != byte_count:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "字节数与数量不符")
        return Request(fc, address, count, _unpack_bits(body, count))

    if fc == FC.WRITE_MULTIPLE_REGISTERS:
        if len(pdu) < 6:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "请求长度错误")
        _, address, count, byte_count = struct.unpack(">BHHB", pdu[:6])
        body = pdu[6:]
        if byte_count != count * 2 or len(body) != byte_count:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "字节数与数量不符")
        return Request(fc, address, count, _unpack_regs(body))

    if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
        if len(pdu) < 10:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "请求长度错误")
        _, r_addr, r_count, w_addr, w_count, byte_count = struct.unpack(">BHHHHB", pdu[:10])
        body = pdu[10:]
        if byte_count != w_count * 2 or len(body) != byte_count:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "字节数与数量不符")
        return Request(fc, r_addr, r_count, _unpack_regs(body), write_address=w_addr)

    if fc == FC.READ_DEVICE_IDENTIFICATION:
        if len(pdu) != 4:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, "设备标识请求应为 4 字节")
        if pdu[1] != 0x0E:
            raise ModbusError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持的 MEI 类型 0x{pdu[1]:02X}")
        return Request(fc, 0, 0, [pdu[1], pdu[2], pdu[3]])

    raise ModbusError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持功能码 0x{fc:02X}")


# --------------------------------------------------------------------------
# 响应编解码
# --------------------------------------------------------------------------

def pack_exception(fc: int, code: int) -> bytes:
    """异常响应：功能码最高位置 1，后随异常码。

    两个字节都要夹住 —— 故障注入把 `exception_code` 配成 300 这类值时，
    原来会抛 `struct.error`，而这一行在从站的 try 之外，整个连接线程
    会带 traceback 退出，连接被静默断开。
    """
    return struct.pack(">BB", (int(fc) | 0x80) & 0xFF, int(code) & 0xFF)


def _counted(fc: int, payload: bytes) -> bytes:
    """功能码 + 字节数 + 数据。字节数是 1 字节字段，超了就是非法应答。"""
    if len(payload) > 0xFF:
        raise ParameterError(
            ExceptionCode.ILLEGAL_DATA_VALUE,
            f"应答数据 {len(payload)} 字节，超出单帧 255 字节上限")
    return struct.pack(">BB", fc, len(payload)) + payload


def pack_response(resp: Response) -> bytes:
    fc = int(resp.function)

    if fc in READ_BITS_FC:
        return _counted(fc, _pack_bits(resp.values))

    if fc in READ_REGS_FC:
        return _counted(fc, _pack_regs(resp.values))

    if fc == FC.WRITE_SINGLE_COIL:
        return struct.pack(">BHH", fc, resp.address, 0xFF00 if resp.values[0] else 0x0000)

    if fc == FC.WRITE_SINGLE_REGISTER:
        return struct.pack(">BHH", fc, resp.address, int(resp.values[0]))

    if fc in (FC.WRITE_MULTIPLE_COILS, FC.WRITE_MULTIPLE_REGISTERS):
        return struct.pack(">BHH", fc, resp.address, resp.count)

    if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
        return _counted(fc, _pack_regs(resp.values))

    raise ParameterError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持功能码 0x{fc:02X}")


def unpack_response(pdu: bytes, request: Request) -> Response:
    """解析响应。必须传入对应请求，因为写操作的回显要靠请求语义解释。"""
    if len(pdu) < 2:
        raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "响应过短")

    fc = pdu[0]
    if fc & 0x80:
        raise ModbusError(pdu[1] if len(pdu) > 1 else ExceptionCode.SERVER_DEVICE_FAILURE,
                          f"从站拒绝了功能码 0x{request.function:02X}")

    if fc != int(request.function):
        raise ModbusError(ExceptionCode.GATEWAY_TARGET_DEVICE_FAILED,
                          f"功能码回显不符：期望 0x{request.function:02X}，收到 0x{fc:02X}")

    if fc in READ_BITS_FC:
        byte_count = pdu[1]
        body = pdu[2:]
        if len(body) != byte_count:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "字节数字段与实际长度不符")
        # 应答的字节数必须够解析 request.count 个位。少了就报错，
        # 不能直接 _unpack_bits(body, count)：那会抛 IndexError，
        # 而 IndexError 不被任何 except 接住 —— 既不重试，也不计入失败数。
        need = (request.count + 7) // 8 if request.count else 0
        if byte_count < need:
            raise ModbusError(
                ExceptionCode.SERVER_DEVICE_FAILURE,
                f"线圈应答只有 {byte_count} 字节，解析 {request.count} 个位需要 {need} 字节")
        return Response(fc, _unpack_bits(body, request.count))

    if fc in READ_REGS_FC:
        byte_count = pdu[1]
        body = pdu[2:]
        if len(body) != byte_count or byte_count % 2:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "字节数字段与实际长度不符")
        if request.count and byte_count < request.count * 2:
            raise ModbusError(
                ExceptionCode.SERVER_DEVICE_FAILURE,
                f"寄存器应答只有 {byte_count // 2} 个，请求了 {request.count} 个")
        return Response(fc, _unpack_regs(body))

    if fc in WRITE_SINGLE_FC:
        if len(pdu) != 5:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "响应长度错误")
        _, address, value = struct.unpack(">BHH", pdu)
        return Response(fc, [value], address, 1)

    if fc in (FC.WRITE_MULTIPLE_COILS, FC.WRITE_MULTIPLE_REGISTERS):
        if len(pdu) != 5:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "响应长度错误")
        _, address, count = struct.unpack(">BHH", pdu)
        if count != request.count and request.count:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "写回显数量不符")
        return Response(fc, [], address, count)

    if fc == FC.READ_WRITE_MULTIPLE_REGISTERS:
        byte_count = pdu[1]
        body = pdu[2:]
        if len(body) != byte_count or byte_count % 2:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "字节数字段与实际长度不符")
        if request.count and byte_count < request.count * 2:
            raise ModbusError(
                ExceptionCode.SERVER_DEVICE_FAILURE,
                f"FC23 应答只有 {byte_count // 2} 个寄存器，请求了 {request.count} 个")
        return Response(fc, _unpack_regs(body))

    raise ModbusError(ExceptionCode.ILLEGAL_FUNCTION, f"不支持功能码 0x{fc:02X}")


# --------------------------------------------------------------------------
# FC 0x2B 读设备标识（MEI type 0x0E）
# --------------------------------------------------------------------------
# 联调时经常先要确认「对面到底是谁」。靠这个功能码能读出厂商名、型号、版本，
# 比翻手册或猜寄存器快得多。请求固定 4 字节，响应是一串变长对象。

DEVICE_ID_OBJECTS = {
    0x00: "厂商名",
    0x01: "产品代码",
    0x02: "主次版本",
    0x03: "厂商网址",
    0x04: "产品名称",
    0x05: "型号名称",
    0x06: "应用名称",
}

_BASIC_OBJECTS = (0x00, 0x01, 0x02)
_REGULAR_OBJECTS = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06)


def build_device_id_request(code: int = 1, object_id: int = 0) -> bytes:
    return bytes([FC.READ_DEVICE_IDENTIFICATION, 0x0E, code & 0xFF, object_id & 0xFF])


def build_device_id_response(identity: dict[int, str], code: int = 1) -> bytes:
    wanted = _REGULAR_OBJECTS if code >= 2 else _BASIC_OBJECTS
    items = []
    for object_id in wanted:
        if object_id not in identity:
            continue
        value = str(identity[object_id]).encode("ascii", "replace")[:255]
        items.append(bytes([object_id, len(value)]) + value)
    conformity = 0x01 if code == 1 else (0x02 if code == 2 else 0x03)
    header = bytes([
        FC.READ_DEVICE_IDENTIFICATION, 0x0E, code & 0xFF,
        conformity,   # 一致性等级
        0x00,         # more follows
        0x00,         # next object id
        len(items),
    ])
    return header + b"".join(items)


def parse_device_id_response(pdu: bytes) -> dict[int, str]:
    if len(pdu) < 7:
        raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "设备标识响应过短")
    if pdu[1] != 0x0E:
        raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, f"未知 MEI 类型 0x{pdu[1]:02X}")
    count = pdu[6]
    position = 7
    result: dict[int, str] = {}
    for _ in range(count):
        if position + 2 > len(pdu):
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "设备标识对象被截断")
        object_id = pdu[position]
        length = pdu[position + 1]
        value = pdu[position + 2:position + 2 + length]
        if len(value) != length:
            raise ModbusError(ExceptionCode.SERVER_DEVICE_FAILURE, "设备标识对象长度不符")
        result[object_id] = value.decode("ascii", "replace")
        position += 2 + length
    return result
