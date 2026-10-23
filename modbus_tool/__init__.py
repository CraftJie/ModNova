"""ModNova —— 轻量级 Modbus 调试工具，主从一体、TCP / RTU / RTU over TCP 三模，零第三方依赖（RTU 串口可选 pyserial）。

分层：

    pdu.py          协议数据单元 + 数量上限，与传输无关
    codec.py        值 ↔ 寄存器、hex/bin 格式化
    addressing.py   协议地址 ↔ PLC 地址
    parsing.py      用户输入解析（界面/命令行共用）
    datastore.py    从站寄存器区
    transport.py    TCP(MBAP) / RTU(CRC) / RTU over TCP 三种链路
    client.py       主站
    server.py       从站（协议核心 + TCP 壳 + RTU 壳 + RTU over TCP 壳）
    core.py         无界面 API：字段视图、轮询任务、信号、故障、记录
    frameparser.py  报文解析器（粘贴一帧 hex 拆字段）
    applog.py       运行日志滚动落盘 + 崩溃堆栈
    cli.py          命令行
    gui.py          桌面界面
"""

__version__ = "1.5.0"

from .client import ModbusClient
from .core import ModbusCore
from .datastore import DataStore
from .pdu import (
    FC,
    ExceptionCode,
    ModbusError,
    Request,
    Response,
    pack_request,
    unpack_request,
)
from .server import ModbusServer, RtuServer, TcpServer, create_server
from .transport import (
    HAS_SERIAL,
    RtuTransport,
    TcpTransport,
    Transport,
    TransportError,
    list_serial_ports,
)

__all__ = [
    "FC",
    "DataStore",
    "ExceptionCode",
    "HAS_SERIAL",
    "ModbusClient",
    "ModbusCore",
    "ModbusError",
    "ModbusServer",
    "Request",
    "Response",
    "RtuServer",
    "RtuTransport",
    "TcpServer",
    "TcpTransport",
    "Transport",
    "TransportError",
    "create_server",
    "list_serial_ports",
    "pack_request",
    "unpack_request",
]
