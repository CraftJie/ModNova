"""报文解析器 —— 把一帧十六进制报文拆成「人能读」的字段说明。

联调时经常要对着抓包看「这帧到底在说什么」：手上有一串 hex（日志里抄的、
Wireshark 抓的、对面厂商发来的），想知道每个字段是什么。这个模块只做
解析、不做通信，输入输出都是纯文本，方便单独测试。

自动识别的顺序：
1. MBAP 头成立（协议标识为 0 且长度字段自洽）→ 按 Modbus TCP 拆；
2. CRC 校验通过 → 按 RTU 拆（站地址 + PDU + CRC）；
3. 都不成立时尝试「裸 PDU」（比如从日志里只抄了 PDU 段）；
4. 再不行就如实报错，绝不猜。
"""

from __future__ import annotations

import struct

from .crc import verify_crc
from .pdu import (
    DEVICE_ID_OBJECTS,
    EXCEPTION_TEXT,
    ExceptionCode,
)

FC_NAMES = {
    0x01: "读线圈",
    0x02: "读离散输入",
    0x03: "读保持寄存器",
    0x04: "读输入寄存器",
    0x05: "写单线圈",
    0x06: "写单寄存器",
    0x0F: "写多线圈",
    0x10: "写多寄存器",
    0x16: "掩码写寄存器",
    0x17: "读写多寄存器",
    0x2B: "读设备标识",
}

READ_FCS = (0x01, 0x02, 0x03, 0x04)
WRITE_SINGLE_FCS = (0x05, 0x06)
WRITE_MULTI_FCS = (0x0F, 0x10)


def _hex_to_bytes(text: str) -> bytes:
    """容忍空格/逗号/连字符/0x 前缀/全角数字的十六进制输入。"""
    from .parsing import normalize_number
    cleaned = normalize_number(text)
    cleaned = (cleaned.replace("0x", " ").replace("0X", " ")
               .replace(",", " ").replace("，", " ").replace("-", " ")
               .replace(";", " ").replace("；", " ").replace("、", " ")
               .replace("：", " ").replace(":", " "))
    parts = cleaned.split()
    if not parts:
        raise ValueError("没有可解析的内容，请粘贴十六进制报文")
    out = bytearray()
    for part in parts:
        if len(part) % 2:                    # 连写的 hex 可能整体没有空格
            raise ValueError(f"「{part}」不是成对的十六进制字节")
        # 4 位一组的书写（Wireshark data 列、设备手册常见「0106 0005 BEEF」）
        # 必须按两字符一组切开。早期实现 int(part,16) 后 &0xFF，会把
        # "01 03 1234" 解析成 01 03 34 —— 高位字节静默丢失，对一个
        # 「把报文拆给人看」的工具，给错结果还不报警是最糟的失败模式。
        for i in range(0, len(part), 2):
            chunk = part[i:i + 2]
            try:
                out.append(int(chunk, 16))
            except ValueError as exc:
                raise ValueError(f"「{chunk}」不是合法的十六进制字节") from exc
    if len(out) < 2:
        raise ValueError("报文太短，连功能码都放不下")
    return bytes(out)


def _fc_plausible(fc: int) -> bool:
    """这个字节像不像一个合法功能码。

    注意不能只看 `fc & 0x80`：RTU 的 CRC 尾字节（如 0xC4）也带高位，
    会把垃圾认成「异常响应」。异常响应的功能码去掉高位后必须是已知 FC。
    """
    return fc in FC_NAMES or (bool(fc & 0x80) and (fc & 0x7F) in FC_NAMES)


def _looks_like_mbap(frame: bytes) -> bool:
    """MBAP 自洽 + 站号/功能码合理。

    只查「协议标识为 0 且长度字段自洽」会把 RTU 帧认成 TCP：RTU 的
    地址字段常常就是 0x0000，CRC 尾字节又能撞上自洽的长度（真实案例：
    `01 03 00 00 00 02 C4 0B` 恰好 length=2 = len-6）。必须再看一眼
    站号 ≤ 247、功能码像话 —— TCP 帧里这两个字节真的承载站号和功能码。
    """
    if not (len(frame) >= 8 and frame[2:4] == b"\x00\x00"
            and int.from_bytes(frame[4:6], "big") == len(frame) - 6):
        return False
    unit, fc = frame[6], frame[7]
    return unit <= 247 and _fc_plausible(fc)


def _split_frame(frame: bytes, mode: str) -> tuple[str, list[str], bytes]:
    """返回 (判定方式, 外壳字段说明, PDU)。mode: auto/tcp/rtu/pdu。

    auto 的优先级：CRC 通过是 1/65536 的强证据，先按 RTU；
    再看 MBAP 自洽；都不成立时若功能码位置像话，按 CRC 坏的 RTU 强拆。
    """
    if mode != "tcp" and len(frame) >= 4 and verify_crc(frame):
        notes = [f"RTU 外壳：站地址 {frame[0]} · CRC 正确"]
        return "Modbus RTU", notes, frame[1:-2]

    if mode == "tcp" or (mode == "auto" and _looks_like_mbap(frame)):
        if len(frame) < 8:
            raise ValueError("帧短于 8 字节，装不下 MBAP 头")
        tid, pid, length, unit = struct.unpack(">HHHB", frame[:7])
        if pid != 0:
            raise ValueError(f"MBAP 协议标识非零（{pid}），不是 Modbus TCP 帧")
        if unit > 247 or not _fc_plausible(frame[7]):
            raise ValueError("这不是 Modbus TCP 帧（站号或功能码不合理）"
                             "—— 请改用「自动」或「Modbus RTU」模式")
        notes = [f"MBAP：事务号 {tid} · 协议标识 {pid} · 长度 {length} · 站号 {unit}"]
        if length != len(frame) - 6:
            notes.append(f"⚠ 长度字段 {length} 与实际不符（应为 {len(frame) - 6}）")
        return "Modbus TCP", notes, frame[7:]

    if mode == "rtu" or (mode == "auto" and len(frame) >= 4 and verify_crc(frame)):
        if len(frame) < 4:
            raise ValueError("帧短于 4 字节，装不下 RTU 外壳")
        notes = [f"RTU 外壳：站地址 {frame[0]} · CRC "
                 + ("正确" if verify_crc(frame) else "⚠ 错误")]
        return "Modbus RTU", notes, frame[1:-2]

    if mode == "pdu":
        return "裸 PDU", [], frame

    # auto 但两种外壳都不成立：CRC 坏的 RTU 帧仍值得强拆（干扰/复制不全
    # 是常态，坏帧恰恰最需要看），其余按裸 PDU 碰运气
    if len(frame) >= 4 and _fc_plausible(frame[1]):
        notes = ["⚠ RTU 外壳但 CRC 校验失败 —— 帧可能被干扰或复制不完整，以下按 RTU 强行解析"]
        return "Modbus RTU（CRC 错误）", notes, frame[1:-2]
    if len(frame) >= 2:
        notes = ["⚠ 帧外壳不确定（MBAP 不自洽、CRC 也不对），按裸 PDU 解析；"
                 "若这其实是完整帧，请检查是否漏抄了首尾字节"]
        return "裸 PDU（猜测）", notes, frame
    raise ValueError("帧太短且无法识别外壳 —— 请确认复制完整")


def _pdu_lines(pdu: bytes) -> list[str]:
    """把 PDU 拆成一行行的字段说明。"""
    lines: list[str] = []
    fc = pdu[0]
    body = pdu[1:]

    if fc & 0x80:
        code = body[0] if body else -1
        text = EXCEPTION_TEXT.get(ExceptionCode(code)) if code >= 0 else None
        lines.append(f"功能码 0x{fc - 0x80:02X} 的**异常响应**：异常码 "
                     f"0x{code:02X}（{text or '未知'}）")
        return lines

    name = FC_NAMES.get(fc)
    lines.append(f"功能码 0x{fc:02X}（{name or '未实现/私有功能码'}）" +
                 ("" if name else f" · 数据 {len(body)} 字节"))
    if name is None:
        return lines

    def u16(offset: int) -> int:
        return int.from_bytes(body[offset:offset + 2], "big")

    if fc in READ_FCS and len(body) == 4:
        addr, count = u16(0), u16(2)
        lines.append(f"起始地址 {addr} · 数量 {count}"
                     f"（{'线圈/位' if fc in (1, 2) else '寄存器'}）")
    elif fc in WRITE_SINGLE_FCS and len(body) == 4:
        addr, value = u16(0), u16(2)
        if fc == 0x05:
            lines.append(f"线圈地址 {addr} → "
                         + ("ON (0xFF00)" if value == 0xFF00 else f"OFF (0x{value:04X})"))
        else:
            lines.append(f"寄存器地址 {addr} ← 0x{value:04X}（{value}）")
    elif fc in WRITE_MULTI_FCS and len(body) >= 5:
        addr, count = u16(0), u16(2)
        byte_count = body[4]
        payload = body[5:]
        if byte_count != len(payload):
            lines.append(f"⚠ 字节数字段 {byte_count} 与实际 {len(payload)} 不符")
        lines.append(f"起始地址 {addr} · 数量 {count} · 数据 {byte_count} 字节")
        if fc == 0x0F:
            bits = "".join("1" if payload[i // 8] >> (i % 8) & 1 else "0"
                           for i in range(min(count, 64)))
            lines.append(f"位值（前 {min(count, 64)} 个）：{bits}"
                         + ("…" if count > 64 else ""))
        else:
            regs = struct.unpack(f">{min(count, 16)}H", payload[:min(count, 16) * 2]) \
                if len(payload) >= 2 else ()
            shown = " ".join(f"0x{r:04X}" for r in regs)
            lines.append(f"寄存器值（前 {len(regs)} 个）：{shown}"
                         + ("…" if count > 16 else ""))
    elif fc == 0x16 and len(body) == 6:
        addr, and_mask, or_mask = u16(0), u16(2), u16(4)
        lines.append(f"寄存器地址 {addr} · AND 0x{and_mask:04X} · OR 0x{or_mask:04X}")
        lines.append(f"效果：AND 为 1 的位保留原值，AND 为 0 的位变成 "
                     f"OR 的对应位 → 结果 = (原值 AND 0x{and_mask:04X}) "
                     f"OR 0x{or_mask & ~and_mask & 0xFFFF:04X}")
    elif fc == 0x17 and len(body) >= 9:
        r_addr, r_count = u16(0), u16(2)
        w_addr, w_count = u16(4), u16(6)
        byte_count = body[8]
        lines.append(f"读区 起始 {r_addr} × {r_count} · 写区 起始 {w_addr} × {w_count}"
                     f" · 写数据 {byte_count} 字节")
    elif fc == 0x2B and body:
        mei = body[0]
        lines.append(f"MEI 类型 0x{mei:02X}")
        if mei == 0x0E and len(body) >= 6:
            read_code, object_id = body[1], body[2]
            lines.append(f"读设备标识：一致性等级 {read_code} · 起始对象 {object_id}")
            # 对象数在 body 第 5 个字节（前面是 mei/读码/一致性/后续标志/下一个对象），
            # 少算一个字节就会把 next 当对象数 —— pdu.parse_device_id_response 的注释里
            # 警告过这个坑，解析器这里再踩一次就没脸了
            if len(body) > 5:
                count = body[5]
                pos = 6
                for _ in range(count):
                    if pos + 2 > len(body):
                        break
                    oid, length = body[pos], body[pos + 1]
                    value = body[pos + 2:pos + 2 + length]
                    label = DEVICE_ID_OBJECTS.get(oid, f"对象 {oid}")
                    try:
                        shown = value.decode("ascii")
                    except UnicodeDecodeError:
                        shown = value.hex(" ")
                    lines.append(f"  对象 {oid}（{label}）= {shown!r}（{length} 字节）")
                    pos += 2 + length
    elif fc in READ_FCS and len(body) >= 2 and body[0] == len(body) - 1:
        # 读应答：字节数 + 数据。注意 body 恰好 4 字节时与读请求无法区分
        # （FC01/02 的 17~24 位应答长这样），这里按更常见的「请求」优先，
        # 上面 len(body)==4 的分支已经接住了；落到这里的都是明确的应答形态
        byte_count = body[0]
        payload = body[1:]
        lines.append(f"应答数据 {byte_count} 字节")
        if fc in (0x03, 0x04):
            regs = struct.unpack(f">{byte_count // 2}H", payload[:byte_count]) \
                if byte_count % 2 == 0 and byte_count >= 2 else ()
            shown = " ".join(f"0x{r:04X}" for r in regs[:16])
            lines.append(f"寄存器（前 {min(len(regs), 16)} 个）：{shown}"
                         + ("…" if len(regs) > 16 else ""))
        else:
            bits = "".join("1" if payload[i // 8] >> (i % 8) & 1 else "0"
                           for i in range(min(byte_count * 8, 64)))
            lines.append(f"位值（前 {min(byte_count * 8, 64)} 个）：{bits}")
    elif fc in (0x05, 0x06) and len(body) == 4:
        addr, value = u16(0), u16(2)
        lines.append(f"写回显：地址 {addr} 值 0x{value:04X}")
    elif fc in (0x0F, 0x10) and len(body) == 4:
        lines.append(f"写回显：地址 {u16(0)} 数量 {u16(2)}")
    else:
        lines.append(f"数据 {len(body)} 字节：{body.hex(' ') or '（空）'}"
                     "（长度与该功能码的标准结构对不上，可能是非标准帧）")
    return lines


def parse_frame(text: str, mode: str = "auto") -> list[str]:
    """解析一帧报文，返回逐行说明。解析不了抛 ValueError（中文文案）。"""
    frame = _hex_to_bytes(text)
    shell, notes, pdu = _split_frame(frame, mode)
    lines = [f"判定：{shell} · 共 {len(frame)} 字节"]
    lines.extend(notes)
    if not pdu:
        lines.append("PDU 为空")
        return lines
    lines.extend(_pdu_lines(pdu))
    return lines
