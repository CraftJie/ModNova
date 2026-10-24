"""命令行入口。

设计取向：GUI 是给现场调试用的，CLI 是给脚本和远程 SSH 用的。
所以子命令只做四件事 —— 读、写、轮询、当从站，外加一个站号扫描。

类型解释和地址模式跟界面用同一套代码（codec / addressing），
命令行里看到的结果和界面上一致。
"""

from __future__ import annotations

import argparse
import sys
import time

from .addressing import AddressError, display_address, resolve_address
from .client import ModbusClient
from .codec import DataType, decode, encode, format_value, hex_of, span
from .datastore import DataStore
from .parsing import parse_write_values
from .pdu import ModbusError, ParameterError, read_limit
from .server import RtuOverTcpServer, RtuServer, TcpServer
from .transport import (HAS_SERIAL, RtuOverTcpTransport, RtuTransport, TcpTransport,
                        TransportError, list_serial_ports)


def _parse_endpoint(text: str, default_port: int) -> tuple[str, int]:
    if ":" in text:
        host, _, port = text.partition(":")
        try:
            # `:5020` 这种写法 host 是空串：客户端连不上，从站则 bind("")
            # 绑到全部网卡。统一回退 127.0.0.1，要全网段必须显式写 0.0.0.0。
            return host or "127.0.0.1", int(port)
        except ValueError:
            raise SystemExit(f"端口必须是整数：「{port}」") from None
    return text, default_port


def _add_link_args(parser: argparse.ArgumentParser) -> None:
    link = parser.add_mutually_exclusive_group(required=True)
    link.add_argument("--tcp", metavar="HOST[:PORT]", help="TCP 端点，端口默认 502")
    link.add_argument("--rtutcp", metavar="HOST[:PORT]",
                      help="RTU over TCP（串口服务器/DTU 透传），TCP 流里跑裸 RTU 帧")
    link.add_argument("--rtu", metavar="PORT", help="串口，例如 COM3 或 /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=9600, help="波特率（RTU）")
    parser.add_argument("--parity", default="N", choices=list("NEOMS"), help="校验位（RTU）")
    parser.add_argument("--stopbits", type=float, default=1, choices=[1, 1.5, 2], help="停止位（RTU）")
    parser.add_argument("--unit", type=int, default=1, help="站地址")
    parser.add_argument("--timeout", type=float, default=2.0, help="超时秒数")
    parser.add_argument("--retries", type=int, default=1, help="超时重试次数")


def _add_view_args(parser: argparse.ArgumentParser) -> None:
    """数据显示相关的参数，读、写、轮询共用。"""
    parser.add_argument("--type", dest="dtype", default="uint16",
                        choices=[t.value for t in DataType],
                        help="数据类型，默认 uint16")
    parser.add_argument("--byteorder", default="ABCD", choices=["ABCD", "BADC", "CDAB", "DCBA"],
                        help="字节序，32/64 位类型才有意义")
    parser.add_argument("--scale", type=float, default=1.0, help="显示值 = 原始值 × scale + bias")
    parser.add_argument("--bias", type=float, default=0.0, help="偏移量")
    parser.add_argument("--unit-label", dest="unit_label", default="",
                        help="单位（仅显示用，例如 ℃）；站地址请用 --unit")
    parser.add_argument("--plc", action="store_true",
                        help="地址按 PLC 记法（40001 起），而不是协议地址（0 起）")


def _make_transport(args):
    if args.tcp:
        host, port = _parse_endpoint(args.tcp, 502)
        return TcpTransport(host, port, timeout=args.timeout)
    if args.rtutcp:
        host, port = _parse_endpoint(args.rtutcp, 502)
        return RtuOverTcpTransport(host, port, timeout=args.timeout)
    if not HAS_SERIAL:
        raise SystemExit("RTU 模式需要 pyserial：pip install pyserial")
    return RtuTransport(
        args.rtu, baudrate=args.baud, parity=args.parity,
        stopbits=args.stopbits, timeout=args.timeout,
    )


def _resolve_address(address: int, fc: int, plc_mode: bool) -> int:
    """地址换算走 addressing.resolve_address（界面/命令行/应用层共用一份）。

    这里只负责把 AddressError 转成 CLI 的退出码 —— 早期版本自己拼文案，
    结果把内部区域键名 `holding_registers` 印在了中文句子里。
    """
    try:
        protocol, _area = resolve_address(fc, address, "plc" if plc_mode else "protocol")
    except AddressError as exc:
        raise SystemExit(str(exc)) from None
    return protocol


def _display_address(protocol_address: int, fc: int, plc_mode: bool) -> int:
    area = resolve_address(fc, 0, "protocol")[1]
    return display_address(protocol_address, area, "plc" if plc_mode else "protocol")


def _parse_values(text: str, fc: int, dtype: str) -> list:
    """写值解析走 parsing.parse_write_values（界面用的同一份实现）。

    共用之后 CLI 才支持 ASCII 字符串与全角/中文分隔符 —— 早期 CLI 先把值
    当 float 解析，`--type ascii --value MODBUS-TEST` 必然报「无法解析为数字」。
    """
    try:
        return parse_write_values(text, fc, dtype)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def _print_rows(values: list, address: int, fc: int, args) -> None:
    bits = fc in (1, 2)
    unit = "" if bits else (args.unit_label or "")

    header = f"{'地址':>8}  {'值':>14}  {'HEX':>18}  BIN"
    print(header)
    print("-" * (len(header) + 6))

    if bits:
        for index, bit in enumerate(values):
            shown = _display_address(address + index, fc, args.plc)
            print(f"{shown:>8}  {int(bit):>14}  {'01' if bit else '00':>18}  {'●' if bit else '○'}")
        return

    step = span(args.dtype)
    decoded = decode(values, args.dtype, args.byteorder, args.scale, args.bias)
    for index, value in enumerate(decoded):
        start = address + index * step
        chunk = values[index * step:(index + 1) * step]
        hexs = hex_of(chunk)
        bins = "".join(format(r, "016b") for r in chunk)
        shown = _display_address(start, fc, args.plc)
        text = format_value(value, args.dtype)
        suffix = f" {unit}" if unit else ""
        print(f"{shown:>8}  {text + suffix:>14}  {hexs:>18}  {bins}")


def cmd_read(args) -> int:
    # 读类功能码的判定走 pdu.read_limit（单一来源），不再维护一份本地
    # FC_NAMES —— 以前那份只含 1~4，和 frameparser 的全集是两套会漂移的表。
    if read_limit(args.fc) is None:
        raise SystemExit("读操作的功能码只能是 1/2/3/4")
    # _resolve_address 内部已把 AddressError 转成 SystemExit，无需再捕获
    address = _resolve_address(args.addr, args.fc, args.plc)

    limit = read_limit(args.fc)
    if limit is not None and not 1 <= args.count <= limit:
        raise SystemExit(f"数量必须在 1~{limit} 之间（现在填的是 {args.count}）")

    client = ModbusClient(_make_transport(args), args.unit, args.retries)
    try:
        values = client.read(args.fc, address, args.count)
    except ParameterError as exc:
        # 本地参数问题：请求根本没发出去，不能报成「从站返回异常」
        raise SystemExit(f"参数有误：{exc.message}") from None
    except ModbusError as exc:
        print(f"从站返回异常：{exc}", file=sys.stderr)
        return 2
    except (TimeoutError, TransportError) as exc:
        print(f"通信失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    _print_rows(values, address, args.fc, args)
    return 0


def cmd_write(args) -> int:
    address = _resolve_address(args.addr, args.fc, args.plc)

    if args.fc == 22:
        # 掩码写的两个值是位掩码，不是物理量 —— 固定按 uint16/整数解析，
        # 支持十进制、0x 十六进制、0b 二进制；用户选了 float32 也不该漂
        values = _parse_values(args.value, 22, "uint16")
        if len(values) != 2:
            raise SystemExit("掩码写需要两个掩码：AND,OR（例如 0xFFF0,0x000F）")
        registers = [v & 0xFFFF for v in values]
    else:
        values = _parse_values(args.value, args.fc, args.dtype)
        if args.fc in (5, 15):
            registers = values
        else:
            try:
                registers = encode(values, args.dtype, args.byteorder, args.scale, args.bias)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            if args.fc == 6 and len(registers) != 1:
                raise SystemExit(
                    f"{args.dtype} 占 {len(registers)} 个寄存器，单点写放不下，请改用 --fc 16")

    client = ModbusClient(_make_transport(args), args.unit, args.retries)
    try:
        client.write(args.fc, address, registers)
    except ParameterError as exc:
        raise SystemExit(f"参数有误：{exc.message}") from None
    except ModbusError as exc:
        print(f"从站返回异常：{exc}", file=sys.stderr)
        return 2
    except (TimeoutError, TransportError) as exc:
        print(f"通信失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    print(f"写入成功：FC{args.fc:02d} 地址 {args.addr} 值 {values} → 寄存器 {registers}")
    return 0


def cmd_poll(args) -> int:
    address = _resolve_address(args.addr, args.fc, args.plc)

    client = ModbusClient(_make_transport(args), args.unit, args.retries)
    print(f"开始轮询，间隔 {args.interval}s，Ctrl+C 结束")
    rounds = 0
    try:
        while args.limit <= 0 or rounds < args.limit:
            rounds += 1
            started = time.perf_counter()
            try:
                values = client.read(args.fc, address, args.count)
                elapsed = (time.perf_counter() - started) * 1000
                stamp = time.strftime("%H:%M:%S")
                body = " ".join(
                    format_value(v, args.dtype) for v in
                    decode(values, args.dtype, args.byteorder, args.scale, args.bias)
                ) if args.fc not in (1, 2) else " ".join(str(int(v)) for v in values)
                unit = f" {args.unit_label}" if args.unit_label else ""
                print(f"[{stamp}] #{rounds:<4} {elapsed:6.1f}ms  {body}{unit}")
            except ParameterError as exc:
                # 参数问题重试一万次也一样，直接退出
                raise SystemExit(f"参数有误：{exc.message}") from None
            except ModbusError as exc:
                print(f"[{rounds}] 异常：{exc}", file=sys.stderr)
            except (TimeoutError, TransportError) as exc:
                print(f"[{rounds}] 通信失败：{exc}", file=sys.stderr)
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        client.close()
    return 0


def cmd_serve(args) -> int:
    store = DataStore(size=args.size)
    init_values = _parse_values(args.init, 3, "uint16") if args.init else []
    if len(init_values) > args.size:
        # 以前这里靠 DataStore.set 抛 ModbusError，而 main() 只兜 ValueError，
        # 用户看到的是裸 traceback。
        raise SystemExit(
            f"--init 提供了 {len(init_values)} 个初值，超出区间长度 --size {args.size}")
    for index, value in enumerate(init_values):
        store.set("holding_registers", index, value)

    if args.tcp:
        host, port = _parse_endpoint(args.tcp, 502)
        server = TcpServer(host=host, port=port, store=store, unit=args.unit, on_log=print)
    elif args.rtutcp:
        host, port = _parse_endpoint(args.rtutcp, 502)
        server = RtuOverTcpServer(host=host, port=port, store=store, unit=args.unit, on_log=print)
    else:
        if not HAS_SERIAL:
            raise SystemExit("RTU 模式需要 pyserial：pip install pyserial")
        server = RtuServer(
            port=args.rtu, baudrate=args.baud, parity=args.parity,
            stopbits=args.stopbits, store=store, unit=args.unit, on_log=print,
        )

    server.start()
    print(f"从站运行中（{server.port}），Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.stop()
    return 0


def cmd_scan(args) -> int:
    client = ModbusClient(_make_transport(args), 1, retries=0)
    low, _, high = args.range.partition("-")
    first, last = int(low), int(high or low)
    print(f"扫描站地址 {first}~{last}…")
    found = []
    try:
        for unit in range(first, last + 1):
            if client.probe(unit):
                found.append(unit)
                print(f"  站 {unit:>3} 在线")
    finally:
        client.close()
    print(f"共发现 {len(found)} 个站：{found or '无'}")
    return 0


def cmd_ports(_args) -> int:
    """列出本机串口。以前写成一行 lambda 里塞元组取下标的技巧，可读性差。"""
    ports = list_serial_ports()
    print("\n".join(ports) or "未发现串口")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modnova",
        description="ModNova —— 轻量级 Modbus 调试工具：主从一体，TCP / RTU 双模",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_read = sub.add_parser("read", help="读线圈或寄存器")
    _add_link_args(p_read)
    _add_view_args(p_read)
    p_read.add_argument("--fc", type=int, default=3, choices=[1, 2, 3, 4], help="功能码")
    p_read.add_argument("--addr", type=int, default=0, help="起始地址（--plc 时按 PLC 记法）")
    p_read.add_argument("--count", type=int, default=10, help="数量")
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write", help="写单个或多个值")
    _add_link_args(p_write)
    _add_view_args(p_write)
    p_write.add_argument("--fc", type=int, default=6, choices=[5, 6, 15, 16, 22],
                         help="功能码（22 = 掩码写，值为 AND,OR 两个掩码）")
    p_write.add_argument("--addr", type=int, default=0, help="起始地址")
    p_write.add_argument("--value", required=True,
                         help="写入值，逗号分隔；按 --type 解释（float32 可直接写 220.5）")
    p_write.set_defaults(func=cmd_write)

    p_poll = sub.add_parser("poll", help="周期性读取")
    _add_link_args(p_poll)
    _add_view_args(p_poll)
    p_poll.add_argument("--fc", type=int, default=3, choices=[1, 2, 3, 4])
    p_poll.add_argument("--addr", type=int, default=0)
    p_poll.add_argument("--count", type=int, default=8)
    p_poll.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒")
    p_poll.add_argument("--limit", type=int, default=0, help="轮询次数，0 表示无限")
    p_poll.set_defaults(func=cmd_poll)

    p_serve = sub.add_parser("serve", help="作为从站运行")
    link = p_serve.add_mutually_exclusive_group(required=True)
    link.add_argument("--tcp", metavar="HOST[:PORT]")
    link.add_argument("--rtutcp", metavar="HOST[:PORT]",
                      help="RTU over TCP：TCP 监听，流里跑裸 RTU 帧")
    link.add_argument("--rtu", metavar="PORT")
    p_serve.add_argument("--baud", type=int, default=9600)
    p_serve.add_argument("--parity", default="N", choices=list("NEOMS"))
    p_serve.add_argument("--stopbits", type=float, default=1, choices=[1, 1.5, 2])
    p_serve.add_argument("--unit", type=int, default=1, help="站地址，0 表示不限")
    p_serve.add_argument("--size", type=int, default=10000, help="每个寄存器区的长度")
    p_serve.add_argument("--init", default="", help="保持寄存器初值，逗号分隔")
    p_serve.set_defaults(func=cmd_serve)

    p_scan = sub.add_parser("scan", help="扫描在线站号")
    _add_link_args(p_scan)
    p_scan.add_argument("--range", default="1-16", help="站号范围，例如 1-32")
    p_scan.set_defaults(func=cmd_scan)

    p_ports = sub.add_parser("ports", help="列出本机串口")
    p_ports.set_defaults(func=cmd_ports)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except ValueError as exc:
        # 参数/数据类错误：用 argparse 的格式打一行中文并退出码 2。
        # 早期这里是裸 traceback —— 用户改错一个数字，看到的是
        # 「File "...", line 153, in cmd_read」。
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
