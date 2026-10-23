"""字段级视图：把一段寄存器按「一行一个字段」来解释。

整段套同一种类型只能应付简单设备。真实设备的一段地址里往往混着不同量纲：

    0x0000  220.5  ℃   母线温度    float32  CDAB
    0x0002   50.1  Hz  输出频率    float32  CDAB
    0x0004   3         运行状态    uint16
    0x0005   12        故障码      uint16

视图就是这张表。每个字段有自己的偏移、类型、字节序、缩放、别名和单位。
存进配置后能随项目走，也能发给同事 —— 这是「别名」为什么不该单独做一个功能：
它本来就是字段定义的一部分。
"""

from __future__ import annotations

from dataclasses import dataclass

from .codec import DataType, bin_of, decode, format_value, hex_of, span


@dataclass
class Field:
    """一个字段：从起始地址算起，偏移多少、按什么解释、叫什么名字。"""

    offset: int = 0
    dtype: str = "uint16"
    byteorder: str = "ABCD"
    scale: float = 1.0
    bias: float = 0.0
    name: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        self.offset = int(self.offset)
        self.dtype = DataType(self.dtype).value
        self.scale = float(self.scale)
        self.bias = float(self.bias)
        if self.offset < 0:
            raise ValueError(f"字段偏移不能为负：{self.offset}")

    @property
    def span(self) -> int:
        return span(self.dtype)

    def to_dict(self) -> dict:
        return {
            "offset": self.offset, "dtype": self.dtype, "byteorder": self.byteorder,
            "scale": self.scale, "bias": self.bias, "name": self.name, "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Field":
        return cls(
            offset=int(data.get("offset", 0)),
            dtype=data.get("dtype", "uint16"),
            byteorder=data.get("byteorder", "ABCD"),
            scale=float(data.get("scale", 1.0)),
            bias=float(data.get("bias", 0.0)),
            name=str(data.get("name", "")),
            unit=str(data.get("unit", "")),
        )


def parse_fields(items: list | None) -> list[Field]:
    """把「dict 或 Field」的混合列表统一成 Field 列表。

    幂等很重要：`add_task()` 存的是 Field 对象，轮询循环又把它们交回
    `read(fields=...)`。早期实现无条件 `Field.from_dict(item)`，
    于是 `item.get(...)` 在 Field 上抛 AttributeError，多任务轮询 + 字段视图
    这个组合从来没跑通过（异常还被轮询线程吞成一行 error 文案）。
    """
    if not items:
        return []
    return [item if isinstance(item, Field) else Field.from_dict(item) for item in items]


def total_span(fields: list[Field]) -> int:
    """这批字段一共要读多少个寄存器。"""
    return max((f.offset + f.span for f in fields), default=0)


def decode_fields(registers: list[int], fields: list[Field]) -> list[dict]:
    """按字段切分解码。越界的字段直接跳过，不硬拼半个值。"""
    rows: list[dict] = []
    for item in fields:
        chunk = registers[item.offset:item.offset + item.span]
        if len(chunk) < item.span:
            continue
        value = decode(chunk, item.dtype, item.byteorder, item.scale, item.bias)[0]
        rows.append({
            "offset": item.offset,
            "name": item.name or f"@{item.offset}",
            "dtype": item.dtype,
            "value": value,
            "text": format_value(value, item.dtype),
            "unit": item.unit,
            "raw": chunk,
            "hex": hex_of(chunk),
            "bin": bin_of(chunk),
        })
    return rows


def validate_fields(fields: list[Field], size: int) -> list[str]:
    """返回告警列表。重叠是允许的（有些设备确实复用地址），只提示不拦截。"""
    warnings: list[str] = []
    occupied: dict[int, str] = {}
    for item in fields:
        if item.offset + item.span > size:
            warnings.append(f"「{item.name or item.offset}」超出读取范围")
        for index in range(item.offset, item.offset + item.span):
            if index in occupied:
                warnings.append(
                    f"「{item.name or item.offset}」与「{occupied[index]}」在偏移 {index} 重叠")
            occupied[index] = item.name or str(item.offset)
    return warnings
