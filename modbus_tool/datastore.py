"""从站侧寄存器数据模型。

Modbus 把数据分成四个互相独立的区，地址都从 0 开始：

    coils              线圈         读写  位
    discrete_inputs    离散输入     只读  位
    holding_registers  保持寄存器   读写  16 位
    input_registers    输入寄存器   只读  16 位

这里只负责存和取，不掺任何协议解析；越界统一抛 ILLEGAL_DATA_ADDRESS，
这是从站应有的行为，比悄悄返回 0 更容易在联调时发现问题。
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass

from .pdu import ExceptionCode, ModbusError

# 四个数据区。凡是接受 area 参数的公开方法都要先过这一关 ——
# 直接 `getattr(self, area)` 会把 `_lock`、`signals` 也当成数据区。
AREAS = ("coils", "discrete_inputs", "holding_registers", "input_registers")

BIT_AREAS = ("coils", "discrete_inputs")

SIGNAL_MODES = ("sine", "ramp", "square", "random")

SIGNAL_LABEL = {
    "sine": "正弦",
    "ramp": "斜坡",
    "square": "方波",
    "random": "随机",
}


@dataclass
class Signal:
    """给某个寄存器绑定一个随时间变化的信号，模拟真实设备的动态量。

    真实的温度不会一直停在 220.5 —— 它会在某个区间里缓慢漂移。模拟器如果只会
    输出定值，联调时上位机的趋势、报警、滤波逻辑根本没法验证。
    """

    area: str
    index: int
    mode: str = "sine"
    period: float = 10.0
    low: int = 0
    high: int = 1000
    phase: float = 0.0

    def value_at(self, now: float) -> int | bool:
        span = max(0, self.high - self.low)
        period = max(0.1, self.period)
        position = ((now + self.phase) % period) / period      # 0 ~ 1

        if self.mode == "sine":
            raw = self.low + span * (0.5 + 0.5 * math.sin(2 * math.pi * position))
        elif self.mode == "ramp":
            raw = self.low + span * position
        elif self.mode == "square":
            raw = self.high if position < 0.5 else self.low
        elif self.mode == "random":
            raw = random.randint(self.low, self.high)
        else:
            raw = self.low

        if self.area in ("coils", "discrete_inputs"):
            return raw > self.low + span / 2
        return int(round(raw)) & 0xFFFF


class DataStore:
    def __init__(self, size: int = 10000) -> None:
        if not 1 <= size <= 65536:
            raise ValueError("寄存器区间大小必须在 1~65536 之间")
        self.size = size
        self.coils: list[bool] = [False] * size
        self.discrete_inputs: list[bool] = [False] * size
        self.holding_registers: list[int] = [0] * size
        self.input_registers: list[int] = [0] * size
        self.signals: dict[tuple[str, int], Signal] = {}
        self._lock = threading.RLock()

    # -- 内部 --------------------------------------------------------------

    def _area_list(self, area: str) -> list:
        """区域名 → 对应的列表。非法名字直接报错，别让 getattr 拿到别的东西。"""
        if area not in AREAS:
            raise ValueError(f"未知数据区 {area!r}，可选：{'/'.join(AREAS)}")
        return getattr(self, area)

    def _check(self, address: int, count: int) -> None:
        if count <= 0:
            raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, f"数量 {count} 无效")
        if address < 0 or address + count > self.size:
            raise ModbusError(
                ExceptionCode.ILLEGAL_DATA_ADDRESS,
                f"区间 {address}~{address + count - 1} 超出 0~{self.size - 1}",
            )

    def _apply_signals(self, area: str, address: int, count: int) -> None:
        """读取前把绑了信号的格子刷到当前时刻。

        按需计算，不起后台线程 —— 不会有个线程在看不见的地方改数据，
        也省掉线程生命周期管理。
        """
        if not self.signals:
            return
        now = time.time()
        target = self._area_list(area)
        for index in range(address, address + count):
            signal = self.signals.get((area, index))
            if signal is not None:
                target[index] = signal.value_at(now)

    # -- 信号（模拟动态量）--------------------------------------------------

    def set_signal(self, area: str, index: int, mode: str = "sine",
                   period: float = 10.0, low: int = 0, high: int = 1000) -> None:
        with self._lock:
            self._area_list(area)
            self._check(index, 1)
            if mode not in SIGNAL_MODES:
                raise ValueError(f"未知信号模式 {mode}")
            self.signals[(area, index)] = Signal(
                area, index, mode, max(0.1, float(period)), int(low), int(high))

    def clear_signal(self, area: str, index: int) -> None:
        with self._lock:
            self.signals.pop((area, index), None)

    def list_signals(self) -> list[dict]:
        with self._lock:
            return [dict(vars(s)) for s in self.signals.values()]

    def clear_signals(self) -> None:
        with self._lock:
            self.signals.clear()

    # -- 读 ----------------------------------------------------------------

    def read_coils(self, address: int, count: int) -> list[bool]:
        with self._lock:
            self._check(address, count)
            self._apply_signals("coils", address, count)
            return self.coils[address:address + count]

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        with self._lock:
            self._check(address, count)
            self._apply_signals("discrete_inputs", address, count)
            return self.discrete_inputs[address:address + count]

    def read_holding_registers(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._check(address, count)
            self._apply_signals("holding_registers", address, count)
            return self.holding_registers[address:address + count]

    def read_input_registers(self, address: int, count: int) -> list[int]:
        with self._lock:
            self._check(address, count)
            self._apply_signals("input_registers", address, count)
            return self.input_registers[address:address + count]

    def read_area(self, area: str, address: int, count: int) -> list:
        """按区域批量读一段。界面刷新用这个，别逐格 `get()`。"""
        with self._lock:
            target = self._area_list(area)
            self._check(address, count)
            self._apply_signals(area, address, count)
            return target[address:address + count]

    # -- 写 ----------------------------------------------------------------

    def write_coils(self, address: int, values: list[bool]) -> None:
        with self._lock:
            self._check(address, len(values))
            self.coils[address:address + len(values)] = [bool(v) for v in values]

    def write_holding_registers(self, address: int, values: list[int]) -> None:
        with self._lock:
            self._check(address, len(values))
            for value in values:
                if not 0 <= int(value) <= 0xFFFF:
                    raise ModbusError(ExceptionCode.ILLEGAL_DATA_VALUE, f"值 {value} 超出 0~65535")
            self.holding_registers[address:address + len(values)] = [int(v) for v in values]

    def rw_holding_registers(self, write_address: int, values: list[int],
                             read_address: int, count: int) -> list[int]:
        """FC23（读写复合）专用：先写后读必须在**同一个锁周期**内完成。

        分成 write_holding_registers + read_holding_registers 两次拿锁的话，
        两个主站并发连接时，写与读之间可能插入别人的写操作 —— 主站会读到
        「不是自己刚写」的值，行为和真实设备对不上。锁是 RLock，内部的
        单区操作可以安全重入。
        """
        with self._lock:
            self.write_holding_registers(write_address, values)
            self._check(read_address, count)
            self._apply_signals("holding_registers", read_address, count)
            return self.holding_registers[read_address:read_address + count]

    def mask_write_holding(self, address: int, and_mask: int, or_mask: int) -> int:
        """FC22（掩码写）：只改一个寄存器的某几位。

        结果 = (当前值 AND and_mask) OR (or_mask AND NOT and_mask)。
        and_mask 里为 1 的位保留原值，为 0 的位被 or_mask 对应位覆盖 ——
        变频器的位域参数（运行命令字）就是这么改的，整字重写会误伤别的位。
        读改写必须在同一个锁周期内完成，否则并发主站会插进自己的写操作。
        """
        with self._lock:
            self._check(address, 1)
            current = self.holding_registers[address]
            updated = ((current & and_mask) | (or_mask & ~and_mask)) & 0xFFFF
            self.holding_registers[address] = updated
            return updated

    # -- 便于 GUI 直接操作的单元格接口 --------------------------------------

    def get(self, area: str, index: int) -> int | bool:
        with self._lock:
            target = self._area_list(area)
            self._check(index, 1)
            self._apply_signals(area, index, 1)
            return target[index]

    def set(self, area: str, index: int, value) -> None:
        with self._lock:
            target = self._area_list(area)
            self._check(index, 1)
            # 手动改值就接管这个格子，否则下一个读取周期又被信号覆盖回去
            self.signals.pop((area, index), None)
            if area in BIT_AREAS:
                target[index] = bool(value)
            else:
                target[index] = int(value) & 0xFFFF

    def clear(self) -> None:
        with self._lock:
            self.coils = [False] * self.size
            self.discrete_inputs = [False] * self.size
            self.holding_registers = [0] * self.size
            self.input_registers = [0] * self.size
            self.signals.clear()
