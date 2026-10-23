"""Modbus CRC16 校验。

多项式 0xA001（即 0x8005 的位反转形式），初值 0xFFFF，
结果按低字节在前写入帧尾 —— 这是 Modbus RTU 的标准约定。
"""

from __future__ import annotations

_POLY = 0xA001


def _build_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16(data: bytes) -> int:
    """计算 CRC16，返回 0x0000~0xFFFF 的整数。"""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def append_crc(frame: bytes) -> bytes:
    """在帧尾追加 CRC 校验字（低字节在前）。"""
    return frame + crc16(frame).to_bytes(2, "little")


def verify_crc(frame: bytes) -> bool:
    """校验带 CRC 的完整帧。"""
    if len(frame) < 4:
        return False
    return crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def strip_crc(frame: bytes) -> bytes:
    """去掉帧尾 CRC，失败时抛 ValueError。"""
    if not verify_crc(frame):
        raise ValueError("CRC 校验失败")
    return frame[:-2]
