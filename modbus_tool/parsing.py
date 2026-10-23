"""用户输入的解析 —— 界面与命令行共用。

同一个工具两条入口，如果各写一份「怎么把用户敲的字变成数值」，迟早会漂移：
界面认全角减号和中文分号，命令行只认半角；界面能写 ASCII 字符串，命令行
报「无法解析为数字」。这个模块就是那份唯一实现。

三条规则：

1. **全角归一化**。IME 在全角状态打出的「－5」、从 PDF 抄来的「−5」、
   「０１２」看着和半角一样，编码不同，一律先归一化再解析。
2. **整数接受十进制 / 0x / 0b 和前导零**。手册里的地址常写成 `0005`，
   `int("0005", 0)` 会报错，这里不报。
3. **ASCII 类型整串当字符串**，不按逗号切 —— 型号里带逗号是常事
   （`ABC, Inc.`）。
"""

from __future__ import annotations

from .codec import DataType, is_integer

# 中文输入法和 PDF 手册最爱送进来的"看着一样、其实不是一个字符"：
# － (U+FF0D 全角)、− (U+2212 数学减号)、– — ﹣ 都是减号；全角数字、全角句点同理。
_FULLWIDTH = str.maketrans({
    "－": "-", "−": "-", "–": "-", "—": "-", "﹣": "-",
    "＋": "+", "　": " ", "．": ".",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})

# 写入值里的分隔符：半角/全角逗号、分号、顿号都当分隔符
_SEPARATORS = (";", "；", "，", "、", ",")

TRUTHY = {"1", "on", "true", "yes", "高"}


def normalize_number(text) -> str:
    """全角转半角并去首尾空白。"""
    return str(text).translate(_FULLWIDTH).strip()


def parse_int(text: str) -> int:
    """十进制/十六进制/二进制都能认，且容忍前导零。

    自己先做全角归一化：调用方不一定记得（`_write_cell_async` 记得，
    但 `parse_int` 是被公开导出的工具函数，不该依赖调用方）。
    """
    body = normalize_number(text)
    sign = 1
    if body[:1] in ("+", "-"):
        sign = -1 if body[0] == "-" else 1
        body = body[1:].strip()
    low = body.lower()
    if low.startswith("0x"):
        return sign * int(body, 16)
    if low.startswith("0b"):
        return sign * int(body, 2)
    return sign * int(body, 10)


def split_values(text: str) -> list[str]:
    """把「1, 2、3；4」切成 ['1','2','3','4']。"""
    raw = normalize_number(text)
    for sep in _SEPARATORS:
        raw = raw.replace(sep, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_write_values(text: str, fc: int, dtype: str) -> list:
    """把写入框里的文本解析成「编码前的值列表」。

    - 位区（FC05/15）：任何不在 TRUTHY 里的写法都算 0，和现场习惯一致
    - 整数类型：返回 int（**不要**先转 float —— 64 位整数会被截掉低位）
    - 浮点类型：返回 float
    - ASCII：整串返回，长度由调用方按寄存器数校验

    解析失败抛 `ValueError`，文案面向用户（中文），由调用方决定怎么展示。
    """
    raw = str(text).strip()
    if not raw:
        raise ValueError("没有可写入的值")

    dtype = DataType(dtype)
    if dtype is DataType.ASCII:
        if int(fc) not in (6, 16):
            raise ValueError("ASCII 字符串请用 FC06 / FC16 写")
        return [raw]

    items = split_values(raw)
    if not items:
        raise ValueError("没有可写入的值")
    if int(fc) in (5, 15):
        return [item.lower() in TRUTHY for item in items]
    if is_integer(dtype):
        try:
            return [parse_int(item) for item in items]
        except ValueError:
            raise ValueError(f"无法解析为整数：{raw}") from None
    try:
        return [float(item) for item in items]
    except ValueError:
        raise ValueError(f"无法解析为数字：{raw}") from None
