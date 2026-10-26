"""设备会话的应用层：字段视图、轮询任务、信号、故障注入、CSV 记录、设备识别。

这一层不知道界面长什么样。Tk 界面（`gui.py`）和 CLI 共用它，
所以同一套语义不会在两边各自漂移。

（历史说明：这个模块原来是 `web.py`，同时挂着本地 HTTP 服务和前端单页。
网页版已经下线，只留下与界面无关的逻辑，改名为 `core.py`。）
"""

from __future__ import annotations

import csv
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

# 版本号只有一个来源（包根的 __version__）。以前 core.meta() 写死 "1.2.1"、
# 包根是 1.4.0、从站对 FC2B 报 "V1.3"，三处各说各话。
from . import __version__ as _VERSION
from .addressing import AREA_LABEL, display_address, resolve_address
from .client import ModbusClient
from .codec import (
    DTYPE_LABEL,
    ByteOrder,
    DataType,
    bin_of,
    decode,
    encode,
    format_value,
    hex_of,
    is_integer,
    span,
)
from .config import Config
from .datastore import BIT_AREAS, SIGNAL_LABEL, SIGNAL_MODES, DataStore
from .pdu import DEVICE_ID_OBJECTS, describe_error
from .server import FaultConfig, RtuOverTcpServer, RtuServer, TcpServer
from .transport import (
    HAS_SERIAL,
    RtuOverTcpTransport,
    RtuTransport,
    TcpTransport,
    Transport,
    list_serial_ports,
)
from .view import Field, decode_fields, parse_fields, total_span, validate_fields

# 数据类型下拉的显示名收敛到 codec.DTYPE_LABEL（导入即 re-export，
# `core.DTYPE_LABEL` 对外仍然可用）。



def _safe_number(value: float):
    """JSON 不认 NaN / Infinity，转成 null；显示文本另存。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass
class PollTask:
    """一个独立的轮询任务。

    现场同时盯两三台设备、或者一台设备的几段地址是常事，所以任务之间必须互不干扰：
    各自有线程、各自的地址和类型参数、各自的最新快照。
    """

    id: str
    fc: int
    address: int
    count: int
    interval: float
    params: dict = dc_field(default_factory=dict)
    fields: list = dc_field(default_factory=list)
    stop: threading.Event = dc_field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    latest: dict = dc_field(default_factory=dict)
    seq: int = 0
    label: str = ""

    def brief(self) -> dict:
        return {
            "id": self.id,
            "label": self.label or f"FC{self.fc:02d} · {self.address} × {self.count}",
            "fc": self.fc,
            "address": self.address,
            "count": self.count,
            "interval": self.interval,
            "params": dict(self.params),
            "fields": [f.to_dict() for f in self.fields],
            "latest": dict(self.latest),
        }


class ModbusCore:
    """设备会话的持有者。所有设备操作串行 —— Modbus 本来就是一问一答。"""

    def __init__(self, config_path=None) -> None:
        self._lock = threading.RLock()
        self.client: ModbusClient | None = None
        # 三种壳子（TCP / RTU / RTU over TCP）对外接口一致，类型上写明联合
        self.slave: TcpServer | RtuServer | RtuOverTcpServer | None = None
        self.link_info: dict = {"connected": False, "label": "未连接", "mode": "tcp", "unit": 1}
        self.logs: deque = deque(maxlen=800)
        self._log_seq = 0
        self.frames: deque = deque(maxlen=600)
        self._frame_seq = 0
        self.tasks: dict[str, PollTask] = {}
        self._task_seq = 0
        self.recorder: dict | None = None
        self.last_seen = time.time()
        self.config = Config(config_path)

    # -- 日志 --------------------------------------------------------------

    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._log_seq += 1
            self.logs.append({
                "id": self._log_seq,
                "t": time.strftime("%H:%M:%S"),
                "text": text,
                "level": level,
            })

    def logs_after(self, after: int) -> list[dict]:
        with self._lock:
            return [entry for entry in self.logs if entry["id"] > after]

    # -- 报文 --------------------------------------------------------------

    def _on_frame(self, direction: str, unit: int, frame: bytes) -> None:
        with self._lock:
            self._frame_seq += 1
            now = time.time()
            stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000 % 1000):03d}"
            self.frames.append({
                "id": self._frame_seq,
                "t": stamp,
                "dir": direction,
                "unit": unit,
                "hex": " ".join(f"{b:02X}" for b in frame),
                "len": len(frame),
            })

    def frames_after(self, after: int) -> list[dict]:
        with self._lock:
            return [f for f in self.frames if f["id"] > after]

    # -- 地址 --------------------------------------------------------------

    @staticmethod
    def _resolve_address(fc: int, address: int, address_mode: str) -> tuple[int, str]:
        """把用户填的地址统一换成协议地址（实现见 addressing.resolve_address）。"""
        return resolve_address(fc, address, address_mode)

    @staticmethod
    def _display_address(protocol_address: int, area: str, address_mode: str) -> int:
        return display_address(protocol_address, area, address_mode)

    # -- 链路 --------------------------------------------------------------

    def connect(self, cfg: dict) -> dict:
        mode = cfg.get("mode", "tcp")
        timeout = float(cfg.get("timeout", 2.0))
        unit = int(cfg.get("unit", 1))

        transport: Transport
        if mode == "tcp":
            transport = TcpTransport(cfg.get("host") or "127.0.0.1",
                                     int(cfg.get("port", 502)), timeout=timeout)
        elif mode == "rtutcp":
            transport = RtuOverTcpTransport(cfg.get("host") or "127.0.0.1",
                                            int(cfg.get("port", 502)), timeout=timeout)
        else:
            if not HAS_SERIAL:
                raise RuntimeError("RTU 模式需要 pyserial")
            transport = RtuTransport(
                cfg.get("port_name") or "COM3",
                baudrate=int(cfg.get("baud", 9600)),
                parity=cfg.get("parity", "N"),
                stopbits=float(cfg.get("stopbits", 1)),
                timeout=timeout,
            )

        client = ModbusClient(transport, unit, int(cfg.get("retries", 1)),
                              on_log=lambda m: self.log(m))
        transport.set_tap(self._on_frame)
        transport.open()                       # 连不上就在这里抛；同样放在锁外

        with self._lock:
            if self.client is not None:
                self.client.close()
            self.client = client
            self.link_info = {
                "connected": True,
                "label": transport.label,
                "mode": mode,
                "unit": unit,
                "host": cfg.get("host"),
                "port": cfg.get("port"),
            }
            self.log(f"已连接 {transport.label}，站地址 {unit}", "ok")

        self.config.update_link({k: v for k, v in cfg.items() if k in self.config.link})
        try:
            self.config.save()                 # 落盘也别占着锁
        except OSError:
            pass
        return self.link_info

    def disconnect(self) -> dict:
        self.stop_poll()
        with self._lock:
            if self.client is not None:
                self.client.close()
                self.client = None
            self.link_info = {"connected": False, "label": "未连接", "mode": "tcp", "unit": 1}
            self.log("链路已断开")
        return self.link_info

    def _require_client(self) -> ModbusClient:
        with self._lock:
            if self.client is None:
                raise RuntimeError("尚未连接")
            return self.client

    # -- 读写 --------------------------------------------------------------

    def _build_rows(self, registers: list[int], address: int, area: str,
                    dtype: str, byteorder: str, scale: float, bias: float,
                    address_mode: str, bits: bool) -> list[dict]:
        rows: list[dict] = []
        if bits:
            for index, bit in enumerate(registers):
                rows.append({
                    "address": address + index,
                    "display_address": self._display_address(address + index, area, address_mode),
                    "raw": [int(bit)],
                    "value": int(bit),
                    "text": "1" if bit else "0",
                    "hex": "01" if bit else "00",
                    "bin": "●" if bit else "○",
                })
            return rows

        step = span(dtype)
        decoded = decode(registers, dtype, byteorder, scale, bias)
        for index, value in enumerate(decoded):
            start = address + index * step
            chunk = registers[index * step:(index + 1) * step]
            rows.append({
                "address": start,
                "display_address": self._display_address(start, area, address_mode),
                "raw": chunk,
                "value": _safe_number(float(value) if isinstance(value, float) else value),
                "text": format_value(value, dtype),
                "hex": hex_of(chunk),
                "bin": bin_of(chunk),
            })
        return rows

    def _build_field_rows(self, registers: list[int], address: int, area: str,
                          fields: list[Field], address_mode: str) -> list[dict]:
        rows = decode_fields(registers, fields)
        for row in rows:
            row["address"] = address + row["offset"]
            row["display_address"] = self._display_address(
                address + row["offset"], area, address_mode)
            row["value"] = _safe_number(row["value"])
        return rows

    def read(self, fc: int, address: int, count: int, dtype: str = "uint16",
             byteorder: str = "ABCD", scale: float = 1.0, bias: float = 0.0,
             unit_text: str = "", address_mode: str = "protocol",
             fields: list | None = None) -> dict:
        fc = int(fc)
        address, area = self._resolve_address(fc, address, address_mode)
        bits = fc in (1, 2)
        field_list = [] if bits else parse_fields(fields)
        if field_list:
            # 字段定义本身就是需求，读多少由它决定
            count = total_span(field_list)

        # 注意：不把 self._lock 套在网络 I/O 外层。从站的日志回调（「主站接入」）
        # 也要拿这把锁，一旦读请求持锁等响应、从站线程又在等锁打日志，
        # 请求就会一直等到超时，而且表现为「偶发、难以复现」。
        client = self._require_client()
        registers = client.read(fc, int(address), int(count))
        latency = round(client.last_latency_ms or 0, 2)

        if field_list:
            rows = self._build_field_rows(registers, address, area, field_list, address_mode)
        else:
            rows = self._build_rows(registers, address, area,
                                    "uint16" if bits else dtype, byteorder,
                                    scale, bias, address_mode, bits)
        return {
            "fc": fc,
            "address": address,
            "area": area,
            "bits": bits,
            "dtype": dtype,
            "unit": unit_text,
            "values": [int(v) for v in registers],
            "rows": rows,
            "latency": latency,
            "warnings": validate_fields(field_list, count) if field_list else [],
        }

    def write(self, fc: int, address: int, values: list, dtype: str = "uint16",
              byteorder: str = "ABCD", scale: float = 1.0, bias: float = 0.0,
              address_mode: str = "protocol") -> dict:
        fc = int(fc)
        address, _area = self._resolve_address(fc, address, address_mode)

        payload: list[bool] | list[int]
        if fc in (5, 15):
            payload = [bool(v) for v in values]
        elif fc == 22:
            # 掩码写的值是位掩码不是物理量，不走 dtype 编码
            if len(values) != 2:
                raise ValueError("掩码写需要 AND 和 OR 两个掩码")
            payload = [int(v) & 0xFFFF for v in values]
        else:
            # 用户按物理量填（比如 220.5），这里负责编码成寄存器序列
            payload = encode(values, dtype, byteorder, scale, bias)
            if fc == 6 and len(payload) != 1:
                raise ValueError(
                    f"{dtype} 占 {len(payload)} 个寄存器，单点写放不下，请改用「写多寄存器」(16)"
                )

        client = self._require_client()
        client.write(fc, int(address), payload)
        latency = round(client.last_latency_ms or 0, 2)
        return {"ok": True, "registers": payload, "latency": latency}

    # -- 轮询任务（可并行）--------------------------------------------------

    def add_task(self, fc: int, address: int, count: int, interval: float,
                 dtype: str = "uint16", byteorder: str = "ABCD", scale: float = 1.0,
                 bias: float = 0.0, unit_text: str = "", address_mode: str = "protocol",
                 fields: list | None = None, label: str = "") -> dict:
        with self._lock:
            self._task_seq += 1
            task = PollTask(
                id=f"t{self._task_seq}",
                fc=int(fc), address=int(address), count=int(count),
                interval=max(0.05, float(interval)),
                label=label or "",
                params={"dtype": dtype, "byteorder": byteorder, "scale": scale,
                        "bias": bias, "unit_text": unit_text,
                        "address_mode": address_mode},
                fields=parse_fields(fields),
            )
            self.tasks[task.id] = task

        task.thread = threading.Thread(target=self._task_loop, args=(task,),
                                       name=f"modbus-poll-{task.id}", daemon=True)
        task.thread.start()
        self.log(f"新增轮询 {task.id}：FC{task.fc:02d} 地址 {task.address} "
                 f"共 {task.count} 点，间隔 {int(task.interval * 1000)}ms")
        return task.brief()

    def _task_loop(self, task: PollTask) -> None:
        while not task.stop.is_set():
            try:
                result = self.read(task.fc, task.address, task.count,
                                   fields=task.fields or None, **task.params)
                task.seq += 1
                task.latest = {"seq": task.seq, "rows": result["rows"],
                               "latency": result["latency"], "error": None,
                               "at": time.strftime("%H:%M:%S")}
                self._record(result["rows"])
            except Exception as exc:          # 一条链路出问题不该拖死其他任务
                task.seq += 1
                task.latest = {"seq": task.seq, "rows": None, "latency": None,
                               "error": self._describe(exc),
                               "at": time.strftime("%H:%M:%S")}
            task.stop.wait(task.interval)

    def remove_task(self, task_id: str) -> dict:
        with self._lock:
            task = self.tasks.pop(task_id, None)
        if task is None:
            return {"ok": False}
        task.stop.set()
        if task.thread is not None:
            task.thread.join(timeout=1.0)
        self.log(f"轮询 {task_id} 已停止")
        return {"ok": True}

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [task.brief() for task in self.tasks.values()]

    def stop_poll(self) -> dict:
        """停掉全部轮询任务。"""
        with self._lock:
            tasks = list(self.tasks.values())
            self.tasks.clear()
        for task in tasks:
            task.stop.set()
        for task in tasks:
            if task.thread is not None:
                task.thread.join(timeout=1.0)
        if tasks:
            self.log(f"已停止 {len(tasks)} 个轮询任务")
        return {"stopped": len(tasks)}

    # -- CSV 记录 ----------------------------------------------------------

    @staticmethod
    def _default_csv_path() -> Path:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "ModbusTool" / "logs"
        return base / f"modbus-{time.strftime('%Y%m%d-%H%M%S')}.csv"

    def record_start(self, path: str = "") -> dict:
        with self._lock:
            self._close_recorder()
            target = Path(path).expanduser() if path else self._default_csv_path()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # utf-8-sig：带 BOM，Excel 打开中文表头才不会乱码
                handle = open(target, "w", newline="", encoding="utf-8-sig")
            except OSError as exc:
                raise RuntimeError(f"无法写入 {target}：{exc}") from exc
            self.recorder = {"path": str(target), "file": handle,
                             "writer": csv.writer(handle), "rows": 0}
            self.log(f"开始记录到 {target}")
            return {"recording": True, "path": str(target), "rows": 0}

    def record_stop(self) -> dict:
        with self._lock:
            info = ({"path": self.recorder["path"], "rows": self.recorder["rows"]}
                    if self.recorder else {})
            self._close_recorder()
        return {"recording": False, **info}

    def record_status(self) -> dict:
        with self._lock:
            if self.recorder is None:
                return {"recording": False}
            return {"recording": True, "path": self.recorder["path"],
                    "rows": self.recorder["rows"]}

    def _record(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._lock:
            rec = self.recorder
            if rec is None:
                return
            try:
                if rec["rows"] == 0:
                    header = ["时间"]
                    for row in rows:
                        name = row.get("name") or row.get("display_address")
                        unit = f"({row['unit']})" if row.get("unit") else ""
                        header.append(f"{name}{unit}")
                    rec["writer"].writerow(header)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                rec["writer"].writerow([stamp] + [row.get("text", "") for row in rows])
                rec["rows"] += 1
                if rec["rows"] % 10 == 0:
                    rec["file"].flush()
            except (OSError, ValueError):
                self._close_recorder()

    def _close_recorder(self) -> None:
        rec = self.recorder
        self.recorder = None
        if rec is None:
            return
        try:
            rec["file"].flush()
            rec["file"].close()
        except OSError:
            pass

    # -- 从站 --------------------------------------------------------------

    def slave_start(self, cfg: dict) -> dict:
        # 先摘掉旧的，再在锁外停 —— stop() 会 join 线程（最多 1.5s），
        # 握着自己的锁等它，等于给所有读状态/写日志的线程加上同样的延迟。
        with self._lock:
            previous = self.slave
            self.slave = None
        if previous is not None:
            previous.stop()

        store = DataStore(size=int(cfg.get("size", 10000)))
        faults = FaultConfig.from_dict(cfg.get("faults"))
        identity = {int(k): str(v)
                    for k, v in (cfg.get("identity") or {}).items()} or None
        mode = cfg.get("mode", "tcp")
        server: TcpServer | RtuServer
        if mode in ("tcp", "rtutcp"):
            # rtutcp：TCP 监听但流里跑裸 RTU 帧，模拟串口服务器/DTU 背后的设备
            server_cls = TcpServer if mode == "tcp" else RtuOverTcpServer
            server = server_cls(
                # 空地址回退 127.0.0.1 而不是 0.0.0.0：要暴露到网段必须显式填
                host=cfg.get("host") or "127.0.0.1",
                port=int(cfg.get("port", 5020)),
                store=store,
                unit=int(cfg.get("unit", 1)),
                on_log=lambda m: self.log(m),
                identity=identity,
                faults=faults,
            )
        else:
            if not HAS_SERIAL:
                raise RuntimeError("RTU 模式需要 pyserial")
            server = RtuServer(
                port=cfg.get("port_name", "COM3"),
                baudrate=int(cfg.get("baud", 9600)),
                parity=cfg.get("parity", "N"),
                store=store,
                unit=int(cfg.get("unit", 1)),
                on_log=lambda m: self.log(m),
                identity=identity,
                faults=faults,
            )
        for signal in (cfg.get("signals") or []):
            store.set_signal(
                signal.get("area", "input_registers"), int(signal.get("index", 0)),
                signal.get("mode", "sine"), float(signal.get("period", 10)),
                int(signal.get("low", 0)), int(signal.get("high", 1000)))
        server.start()          # bind/listen 可能阻塞，不占锁
        with self._lock:
            self.slave = server
        return {"running": True, "port": server.port}

    def slave_stop(self) -> dict:
        with self._lock:
            server = self.slave
            self.slave = None
        if server is not None:
            server.stop()
        return {"running": False}

    def slave_status(self) -> dict:
        with self._lock:
            if self.slave is None:
                return {"running": False}
            core = self.slave.core
            return {
                "running": True,
                "port": self.slave.port,
                "unit": core.unit,
                "requests": core.request_count,
                "errors": core.error_count,
                "last": core.last_request,
                # 兜底异常（结构体溢出、数据模型出错）的原因必须能看见，
                # 否则它们会一直伪装成「设备故障」
                "last_error": getattr(core, "last_error", ""),
                "size": core.store.size,
            }

    def slave_table(self, area: str, start: int, count: int) -> dict:
        with self._lock:
            if self.slave is None:
                return {"running": False, "rows": []}
            store = self.slave.store
            start = max(0, min(start, store.size - 1))
            count = max(1, min(count, store.size - start))
            rows = store.read_area(area, start, count)
            return {"running": True, "start": start, "rows": rows,
                    "bits": area in BIT_AREAS, "size": store.size}

    def slave_set(self, area: str, index: int, value) -> dict:
        with self._lock:
            if self.slave is None:
                raise RuntimeError("从站未启动")
            value = bool(value) if area in BIT_AREAS else int(value) & 0xFFFF
            self.slave.store.set(area, index, value)
            return {"ok": True}

    def slave_clear(self) -> dict:
        with self._lock:
            if self.slave is not None:
                self.slave.store.clear()
        return {"ok": True}

    # -- 设备识别 / 故障注入 / 数据源 --------------------------------------

    def read_device_id(self, code: int = 1) -> dict:
        # 与 read() 同一条纪律：锁只保护状态，网络 I/O 在锁外。
        # 从站线程打日志时也要拿这把锁，持锁等应答会把「偶发卡顿」变成常态。
        client = self._require_client()
        info = client.read_device_id(int(code))
        latency = round(client.last_latency_ms or 0, 2)
        return {
            "latency": latency,
            "items": [
                {"id": oid, "name": DEVICE_ID_OBJECTS.get(oid, f"对象 {oid}"),
                 "value": value}
                for oid, value in sorted(info.items())
            ],
        }

    def slave_faults(self, cfg: dict | None = None) -> dict:
        with self._lock:
            if self.slave is None:
                return {"running": False, "active": False}
            if cfg is not None:
                self.slave.core.faults = FaultConfig.from_dict(cfg)
                self.log(f"故障注入已更新：{self.slave.core.faults.as_dict()}")
            return dict(self.slave.core.faults.as_dict(), running=True,
                        active=self.slave.core.faults.active)

    def slave_signals(self, cfg: dict | None = None) -> dict:
        with self._lock:
            if self.slave is None:
                return {"running": False, "signals": [], "modes": SIGNAL_LABEL}
            if cfg is not None:
                action = cfg.get("action", "set")
                area = cfg.get("area", "input_registers")
                index = int(cfg.get("index", 0))
                if action == "clear":
                    self.slave.store.clear_signal(area, index)
                elif action == "clear_all":
                    self.slave.store.clear_signals()
                else:
                    if cfg.get("mode") not in SIGNAL_MODES:
                        raise ValueError(f"未知信号模式 {cfg.get('mode')}")
                    self.slave.store.set_signal(
                        area, index, cfg.get("mode", "sine"),
                        float(cfg.get("period", 10)), int(cfg.get("low", 0)),
                        int(cfg.get("high", 1000)))
                    self.log(f"信号：{area}[{index}] ← {SIGNAL_LABEL[cfg['mode']]}")
            return {"running": True, "signals": self.slave.store.list_signals(),
                    "modes": SIGNAL_LABEL}

    # -- 配置 --------------------------------------------------------------

    def config_snapshot(self) -> dict:
        return {
            "link": dict(self.config.link),
            "view": dict(self.config.view),
            "presets": self.config.list_presets(),
        }

    def config_update(self, link: dict | None = None, view: dict | None = None) -> dict:
        if link:
            self.config.update_link(link)
        if view:
            self.config.update_view(view)
        try:
            self.config.save()
        except OSError as exc:
            raise RuntimeError(f"配置写入失败：{exc}") from exc
        return self.config_snapshot()

    # -- 杂项 --------------------------------------------------------------

    @staticmethod
    def _describe(exc: Exception) -> str:
        return describe_error(exc)

    def meta(self) -> dict:
        return {
            "serial_ports": list_serial_ports(),
            "has_serial": HAS_SERIAL,
            "version": _VERSION,
            "dtypes": [
                {
                    "value": t.value,
                    "label": DTYPE_LABEL[t],
                    "span": span(t),
                    "integer": is_integer(t),
                }
                for t in DataType
            ],
            "byteorders": [o.value for o in ByteOrder],
            "areas": AREA_LABEL,
        }

    def state(self) -> dict:
        with self._lock:
            stats: dict[str, Any] = {"requests": 0, "errors": 0, "latency": None}
            if self.client is not None:
                stats = {
                    "requests": self.client.request_count,
                    "errors": self.client.error_count,
                    # 延迟必须用 is not None 判：0.0ms 是合法读数（本地回环极快时），
                    # truthiness 判断会把它丢成 None，和 gui._refresh_stats 口径一致
                    "latency": (round(self.client.last_latency_ms, 1)
                                if self.client.last_latency_ms is not None else None),
                }
            return {"link": dict(self.link_info), "stats": stats, "slave": self.slave_status()}

    def shutdown(self) -> None:
        self.stop_poll()
        with self._lock:
            # 记录器每 10 行才 flush 一次，不显式关掉就会丢掉最后几行，
            # 并且留下一个占着文件的句柄（Windows 上删不掉、移不动）。
            self._close_recorder()
            client, slave = self.client, self.slave
            self.client = None
            self.slave = None
        if client is not None:
            client.close()
        if slave is not None:
            slave.stop()

