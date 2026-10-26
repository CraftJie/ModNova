"""寄存器值的类型解释。

Modbus 寄存器本身只有 16 位无符号整数，但设备里装的可能是温度、功率、流量 ——
有符号数、32 位整数、IEEE754 浮点。同一个 16 位值按不同类型解释，结果能差出几个
数量级，这是现场「数据不对」最常见的原因，比通信故障本身还常见。

用法：

    decode([0x435C, 0x8000], DataType.FLOAT32)        -> [220.5]
    decode(regs, DataType.INT16, scale=0.1, bias=0)   -> [-12.3]
    encode([220.5], DataType.FLOAT32)                 -> [0x435C, 0x8000]

四种字节序对应的是「寄存器内的字」和「寄存器之间的字」两级排列：

    ABCD  00 01 02 03   大端，标准做法
    BADC  01 00 03 02   字节交换（部分电表）
    CDAB  02 03 00 01   字交换（大量 PLC 的默认）
    DCBA  03 02 01 00   完全小端

这四种变换都是自逆的（做两次回到原样），所以 encode 和 decode 共用同一个函数。
"""

from __future__ import annotations

import math
import struct
from enum import Enum


class DataType(str, Enum):
    UINT16 = "uint16"
    INT16 = "int16"
    UINT32 = "uint32"
    INT32 = "int32"
    FLOAT32 = "float32"
    UINT64 = "uint64"
    INT64 = "int64"
    FLOAT64 = "float64"
    ASCII = "ascii"          # 一个寄存器 = 2 个 ASCII 字符（设备型号、序列号这类字符串）


class ByteOrder(str, Enum):
    ABCD = "ABCD"
    BADC = "BADC"
    CDAB = "CDAB"
    DCBA = "DCBA"


# 每种类型占几个寄存器
SPAN: dict[DataType, int] = {
    DataType.UINT16: 1,
    DataType.INT16: 1,
    DataType.UINT32: 2,
    DataType.INT32: 2,
    DataType.FLOAT32: 2,
    DataType.UINT64: 4,
    DataType.INT64: 4,
    DataType.FLOAT64: 4,
    # 一个寄存器装两个字符：0x4142 → "AB"。不跨寄存器，所以按 1 算 ——
    # 这样网格里一格就是两个字符，和设备手册「每个寄存器两个字符」对得上。
    DataType.ASCII: 1,
}

_FORMAT: dict[DataType, str] = {
    DataType.UINT16: ">H",
    DataType.INT16: ">h",
    DataType.UINT32: ">I",
    DataType.INT32: ">i",
    DataType.FLOAT32: ">f",
    DataType.UINT64: ">Q",
    DataType.INT64: ">q",
    DataType.FLOAT64: ">d",
}

INTEGER_TYPES = {
    DataType.UINT16, DataType.INT16, DataType.UINT32,
    DataType.INT32, DataType.UINT64, DataType.INT64,
}

# 数据类型的中文显示名。core.meta() 的词汇表、界面「数据类型」下拉都从
# 这里取 —— 以前 core 和 gui 各有一份，加一个类型要改两处还会漂移。
# 顺序即 DataType 枚举序（ASCII 殿后），界面下拉的前 8 项下标依赖它。
DTYPE_LABEL = {
    DataType.UINT16: "无符号 16 位",
    DataType.INT16: "有符号 16 位",
    DataType.UINT32: "无符号 32 位",
    DataType.INT32: "有符号 32 位",
    DataType.FLOAT32: "单精度浮点",
    DataType.UINT64: "无符号 64 位",
    DataType.INT64: "有符号 64 位",
    DataType.FLOAT64: "双精度浮点",
    DataType.ASCII: "ASCII 字符",
}


def span(dtype: DataType | str) -> int:
    """该类型占用几个寄存器。"""
    return SPAN[DataType(dtype)]


def is_integer(dtype: DataType | str) -> bool:
    return DataType(dtype) in INTEGER_TYPES


def _reorder(raw: bytes, order: ByteOrder) -> bytes:
    """按字节序重排。四种变换都自逆，encode/decode 通用。"""
    if order is ByteOrder.ABCD:
        return raw
    if order is ByteOrder.BADC:
        buf = bytearray(raw)
        for i in range(0, len(buf) - 1, 2):
            buf[i], buf[i + 1] = buf[i + 1], buf[i]
        return bytes(buf)
    if order is ByteOrder.CDAB:
        half = len(raw) // 2
        return raw[half:] + raw[:half]
    if order is ByteOrder.DCBA:
        return raw[::-1]
    raise ValueError(f"未知字节序 {order}")


NONPRINT = "\u00b7"      # 「·」：不可打印字节的占位符


def _ascii_char(byte: int) -> str:
    """一个字节显示成什么。

    可打印 ASCII 原样给出；控制字符（0x00 结尾填充、0x0D 换行）显示成「·」——
    否则整格是一个看不见的 NUL，会被当成「这里没读到」。
    特意用「·」而不是「.」：句点是合法 ASCII 字符，用句点做占位会让人
    分不清「这里是 0x00」还是「这里真的是个小圆点」。
    空格保留原样，它是字符串的一部分（"ABC 123"）。
    """
    return chr(byte) if 0x20 <= byte <= 0x7E else NONPRINT


def decode_ascii(registers: list[int],
                 byteorder: ByteOrder | str = ByteOrder.ABCD) -> list[str]:
    """每个寄存器解出两个字符。高字节在前，除非字节序选了交换。"""
    order = ByteOrder(byteorder)
    out: list[str] = []
    for reg in registers:
        high, low = _reorder(int(reg).to_bytes(2, "big"), order)
        out.append(_ascii_char(high) + _ascii_char(low))
    return out


def encode_ascii(text: str, byteorder: ByteOrder | str = ByteOrder.ABCD) -> list[int]:
    """把字符串编码成寄存器序列。

    奇数长度补一个 0x00（寄存器必须是两字节）。设备一般要求定长字符串，
    但补 0x00 还是补空格因厂商而异 —— 补 0x00 是更常见的约定，
    需要补空格的场合在「值」里手工补到偶数位即可。
    """
    order = ByteOrder(byteorder)
    try:
        data = str(text).encode("ascii")
    except UnicodeEncodeError as exc:
        bad = exc.object[exc.start:exc.end]
        raise ValueError(
            f"ASCII 类型只能写 ASCII 字符，「{bad}」不行（中文要用设备支持的编码约定）"
        ) from None
    if len(data) % 2:
        data += b"\x00"
    out: list[int] = []
    for i in range(0, len(data), 2):
        raw = _reorder(data[i:i + 2], order)        # 自逆，方向和 decode 相同
        out.append(int.from_bytes(raw, "big"))
    return out


def patch_ascii(text: str, original: int,
                byteorder: ByteOrder | str = ByteOrder.ABCD) -> int:
    """把「改了一格的字符串」合并回一个寄存器值。

    表格里不可打印的字节显示成「·」，它只是个占位符。用户双击改一格、
    直接把「·A」回车提交时，那个「·」必须还原成原来的字节 —— 否则
    0x00 会被写成 0x2E 或 0x3F，等于悄悄改了数据。

    要真写一个中点符号，用工具条的「写入」——ASCII 类型也写不了它（会报错）。

    **缺位视同占位符**：格子里本来就只显示两个字符，用户把「AB」删成一个
    「A」再回车，意思是「只改第一个字符」，不是「把第二个字符改成空格」。
    早期实现用 `(text + "  ")[:2]` 补空格，会把相邻字节悄悄写成 0x20 ——
    设备里的字符串被改坏而界面上看不出来，属于最糟的一类静默错误。
    """
    order = ByteOrder(byteorder)
    orig = _reorder(int(original).to_bytes(2, "big"), order)
    chars = list(str(text))[:2]
    out = bytearray()
    for index in range(2):
        ch = chars[index] if index < len(chars) else NONPRINT
        if ch == NONPRINT:              # 占位符（或没输入）→ 保留原字节
            out.append(orig[index])
            continue
        code = ord(ch)
        if not 0x20 <= code <= 0x7E:
            raise ValueError(f"ASCII 一格只能是可打印字符，「{ch}」不行")
        out.append(code)
    return int.from_bytes(_reorder(bytes(out), order), "big")


def _register_bytes(value) -> bytes:
    """一个寄存器值 → 两个字节（大端）。

    寄存器只可能是 0~65535。越界时给一句中文，而不是让
    `int.to_bytes` 抛 `OverflowError: int too big to convert`。
    """
    number = int(value)
    if not 0 <= number <= 0xFFFF:
        raise ValueError(f"寄存器值 {value} 超出 0~65535")
    return number.to_bytes(2, "big")


def _check_scale(scale: float) -> None:
    """scale=0 无物理意义，必须显式拒绝。

    decode 侧 `value * 0 + bias` 会把所有读数**静默归零**，看起来像
    「设备读不到数据」；encode 侧 `(value - bias) / scale` 直接抛
    ZeroDivisionError，而界面只捕 ValueError，用户看到的是
    「操作失败（内部错误）」。两条路都远不如一句中文报错。
    """
    if scale == 0:
        raise ValueError("系数不能为 0：读数会全部归零，写入无法反推原始值，请检查「× 系数」输入框")


def decode(
    registers: list[int],
    dtype: DataType | str = DataType.UINT16,
    byteorder: ByteOrder | str = ByteOrder.ABCD,
    scale: float = 1.0,
    bias: float = 0.0,
) -> list[float]:
    """把寄存器序列按类型解释成物理量。

    末尾不足一个完整跨度时直接丢弃 —— 比如 5 个寄存器按 float32 解释只得到 2 个值，
    剩下的那个半边值没有意义，硬拼出来只会误导。
    """
    dtype = DataType(dtype)
    _check_scale(float(scale))
    if dtype is DataType.ASCII:
        # ASCII 返回的是 str 列表（一个寄存器两个字符）。这个「返回什么类型
        # 取决于 dtype」的分支用现有注解表达不了，也不值得为它写一堆 overload：
        # 上层拿到什么就按什么格式化，这里明确标出来是同一个约定的另一半。
        return decode_ascii(registers, byteorder)  # type: ignore[return-value]
    order = ByteOrder(byteorder)
    step = SPAN[dtype]
    fmt = _FORMAT[dtype]

    out: list = []
    for start in range(0, len(registers) - step + 1, step):
        chunk = registers[start:start + step]
        raw = b"".join(_register_bytes(v) for v in chunk)
        value = struct.unpack(fmt, _reorder(raw, order))[0]
        if scale != 1.0 or bias != 0.0:
            value = value * scale + bias
        out.append(value)
    return out


def encode(
    values: list | str,
    dtype: DataType | str = DataType.UINT16,
    byteorder: ByteOrder | str = ByteOrder.ABCD,
    scale: float = 1.0,
    bias: float = 0.0,
) -> list[int]:
    """把物理量编码回寄存器序列，是 decode 的逆运算。

    ASCII 类型要写字符串：`encode("MODBUS", "ascii")` 或 `encode(["MODBUS"], "ascii")`。
    早期实现把列表里的每个元素 `str()` 起来再拼，`encode([220.5], "ascii")`
    会得到 `"220.5"` 的三个寄存器 —— 看着能用，实际是错的数据。
    """
    dtype = DataType(dtype)
    _check_scale(float(scale))
    if dtype is DataType.ASCII:
        if isinstance(values, str):
            return encode_ascii(values, byteorder)
        items = list(values)
        if len(items) == 1 and isinstance(items[0], str):
            return encode_ascii(items[0], byteorder)
        raise ValueError("ASCII 类型请传字符串，例如 encode(\"MODBUS\", \"ascii\")")

    order = ByteOrder(byteorder)
    fmt = _FORMAT[dtype]
    width = SPAN[dtype] * 2

    out: list[int] = []
    for value in values:
        raw_value: int | float
        if dtype in INTEGER_TYPES and scale == 1.0 and bias == 0.0:
            # 64 位整数绝不能过 float：float 尾数只有 53 位，0x4444333322221111
            # 这种值会被静默改掉低位，而且错得很隐蔽
            raw_value = int(value)
        else:
            scaled = ((float(value) - bias) / scale
                      if (scale != 1.0 or bias != 0.0) else float(value))
            raw_value = int(round(scaled)) if dtype in INTEGER_TYPES else scaled
        try:
            raw = struct.pack(fmt, raw_value)
        except struct.error:
            raise ValueError(_range_hint(value, dtype)) from None
        raw = _reorder(raw, order)          # 自逆，方向与 decode 相同
        if len(raw) != width:
            raise ValueError("内部错误：字节数与类型跨度不一致")
        out.extend(int.from_bytes(raw[i:i + 2], "big") for i in range(0, width, 2))
    return out


# 各整数类型的取值范围：报错时直接告诉用户「能填多少」，
# 而不是把 struct 的英文原文抛给人看。
INT_RANGE = {
    DataType.UINT16: (0, 0xFFFF),
    DataType.INT16: (-0x8000, 0x7FFF),
    DataType.UINT32: (0, 0xFFFFFFFF),
    DataType.INT32: (-0x80000000, 0x7FFFFFFF),
    DataType.UINT64: (0, 0xFFFFFFFFFFFFFFFF),
    DataType.INT64: (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
}


def _range_hint(value, dtype: DataType) -> str:
    low_high = INT_RANGE.get(dtype)
    if low_high is None:
        return f"{value} 超出 {dtype.value} 能表示的范围"
    low, high = low_high
    hint = f"{value} 超出 {dtype.value} 的范围（{low} ~ {high}）"
    # 不能用 float(value) 判负：10**400 这种整数转 float 会 OverflowError，
    # 于是「值超范围」的最后一步反倒抛了另一个异常。
    negative = value < 0 if isinstance(value, (int, float)) else False
    if dtype in (DataType.UINT16, DataType.UINT32, DataType.UINT64) and negative:
        signed = {DataType.UINT16: "int16", DataType.UINT32: "int32",
                  DataType.UINT64: "int64"}[dtype]
        hint += f"；要写负数请把类型改成 {signed}"
    return hint


def as_signed16(value: int) -> int:
    """16 位寄存器按 int16 看：0xFFFB → -5。只影响显示，存储仍是原值。"""
    value = int(value) & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def format_value(value: float, dtype: DataType | str) -> str:
    """按类型给出适合展示的字符串。

    注意不能只看 dtype —— 一个 uint16 乘上 0.1 的缩放之后就是小数了，
    按类型决定「显示成整数」会把 123.4 显示成 123。
    """
    if isinstance(value, str):          # ASCII 解出来的就是字符，不用格式化
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if value != value else ("∞" if value > 0 else "-∞")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if DataType(dtype) is DataType.FLOAT32:
        # 单精度的有效位是 7 位（24 位二进制尾数 ≈ 7.22 位十进制）。
        # 用 6 位会丢掉真实的末位：123456.703 会显示成 123457。
        return f"{value:.7g}"
    return f"{value:.10g}"


# --------------------------------------------------------------------------
# 寄存器序列的十六进制 / 二进制文本
#
# 界面、命令行、字段视图、报文日志四处都要把寄存器串打印成人能核对的样子，
# 各写一遍的结果就是格式慢慢漂移（有的带 0x、有的不带，单寄存器是否带前缀
# 各行其是）。统一放这里。
# --------------------------------------------------------------------------

def hex_of(registers: list[int], prefix: bool = True) -> str:
    """`[0x435C, 0x8000]` → `0x435C8000`；单个 → `0x435C`。"""
    if not registers:
        return ""
    body = "".join("%04X" % (int(r) & 0xFFFF) for r in registers)
    return f"0x{body}" if prefix else body


def bin_of(registers: list[int], group: int = 0) -> str:
    """`[0x8000]` → `1000000000000000`；group=4 → `1000 0000 0000 0000`。

    分组是为了让「第几位」数得清 —— 16 个连续 0/1 靠眼睛定位几乎不可能。
    """
    if not registers:
        return ""
    chunks = []
    for reg in registers:
        bits = format(int(reg) & 0xFFFF, "016b")
        chunks.append(" ".join(bits[i:i + group] for i in range(0, 16, group))
                      if group > 0 else bits)
    return "".join(chunks) if group == 0 else " ".join(chunks)

