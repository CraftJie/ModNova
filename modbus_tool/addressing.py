"""协议地址与 PLC 地址的换算。

这是 Modbus 最容易把人绊倒的地方，没有之一。

协议里的地址是 0 基的 —— 读保持寄存器，起始地址 0 就是第一个寄存器。
但设备手册几乎都用「PLC 地址」：40001 表示第一个保持寄存器。
于是同一件事出现两种写法：

    手册写 40001  →  协议要发 0
    手册写 40108  →  协议要发 107      （不是 108，差的是那个 1）

更麻烦的是，不同厂商还有 6 位写法（400001 起步）。这里是纯数值换算，
不做猜测：输入一个 PLC 地址，返回协议地址和它所属的区域。
"""

from __future__ import annotations

# PLC 地址区基址。按数值从大到小排列，先匹配大的，否则 40001 会被 1 先吃掉。
_PLC_AREAS: tuple[tuple[int, str, int], ...] = (
    (400001, "holding_registers", 6),
    (300001, "input_registers", 6),
    (100001, "discrete_inputs", 6),
    (40001, "holding_registers", 4),
    (30001, "input_registers", 4),
    (10001, "discrete_inputs", 4),
    (1, "coils", 4),
)

_BASE_4 = {
    "coils": 1,
    "discrete_inputs": 10001,
    "input_registers": 30001,
    "holding_registers": 40001,
}
_BASE_6 = {
    "coils": 1,
    "discrete_inputs": 100001,
    "input_registers": 300001,
    "holding_registers": 400001,
}

AREA_BY_FC = {
    1: "coils",
    2: "discrete_inputs",
    3: "holding_registers",
    4: "input_registers",
    5: "coils",
    6: "holding_registers",
    15: "coils",
    16: "holding_registers",
    23: "holding_registers",      # 0x17 读写复合：读区和写区都在保持寄存器
}

AREA_LABEL = {
    "coils": "线圈",
    "discrete_inputs": "离散输入",
    "input_registers": "输入寄存器",
    "holding_registers": "保持寄存器",
}


class AddressError(ValueError):
    """PLC 地址不合法。"""


def plc_to_protocol(address: int) -> tuple[int, str]:
    """PLC 地址 → (协议地址, 区域)。

    >>> plc_to_protocol(40001)
    (0, 'holding_registers')
    >>> plc_to_protocol(40108)
    (107, 'holding_registers')
    >>> plc_to_protocol(30005)
    (4, 'input_registers')
    """
    address = int(address)
    if address < 1:
        raise AddressError(f"PLC 地址从 1 开始，收到 {address}")
    for base, area, digits in _PLC_AREAS:
        if address < base:
            continue
        offset = address - base
        # 4 位记法的区间是 base ~ base+9998，6 位是 base ~ base+65535。
        # 不查上界的话 99999 会被当成「4 位基址 + 59998」，
        # 再反算回去就变成了 6 位的 459998 —— 往返不闭合。
        limit = 65535 if digits == 6 else 9998
        if offset > limit:
            raise AddressError(
                f"PLC 地址 {address} 超出 {digits} 位记法范围"
                f"（{base} ~ {base + limit}）")
        return offset, area
    raise AddressError(f"无法识别的 PLC 地址 {address}")


def protocol_to_plc(address: int, area: str, digits: int = 0) -> int:
    """协议地址 → PLC 地址。digits 传 0 表示按量程自动选 4 位或 6 位。"""
    address = int(address)
    if area not in _BASE_4:
        raise AddressError(f"未知区域 {area}")
    if address < 0:
        raise AddressError(f"协议地址不能为负：{address}")
    if digits == 6 or (digits == 0 and address > 9998):
        return address + _BASE_6[area]
    return address + _BASE_4[area]


def area_of_fc(fc: int) -> str | None:
    return AREA_BY_FC.get(int(fc))


def resolve_address(fc: int, address: int, address_mode: str = "protocol") -> tuple[int, str]:
    """把用户填的地址统一换成协议地址，并校验它和功能码是否同区。

    这段逻辑原来在 core.py / cli.py / gui.py 各有一份，错误文案已经漂移
    （CLI 那版会把内部键名 `holding_registers` 拼进中文句子）。收敛到这里。

    返回 (协议地址, 区域)。`address_mode` 为 "plc" 时按 PLC 记法换算；
    其他取值（"protocol" / "dec" / "hex"）一律当协议地址直传。
    """
    fc = int(fc)
    area = area_of_fc(fc) or "holding_registers"
    address = int(address)
    if address_mode != "plc":
        return address, area

    protocol, parsed_area = plc_to_protocol(address)
    expected = area_of_fc(fc)
    if expected and parsed_area != expected:
        raise AddressError(
            f"PLC 地址 {address} 属于{AREA_LABEL[parsed_area]}，"
            f"与当前功能码（{AREA_LABEL[expected]}）不在同一区域")
    return protocol, area


def display_address(protocol_address: int, area: str,
                    address_mode: str = "protocol") -> int:
    """协议地址 → 显示用地址（PLC 记法时换算，换算不了就原样给出）。"""
    if address_mode != "plc":
        return int(protocol_address)
    try:
        return protocol_to_plc(protocol_address, area)
    except AddressError:
        return int(protocol_address)


def describe_plc(address: int) -> str:
    """把 PLC 地址说成人话，用于界面提示。"""
    try:
        protocol, area = plc_to_protocol(address)
    except AddressError:
        return "地址无效"
    return f"{AREA_LABEL[area]} · 协议地址 {protocol}"
