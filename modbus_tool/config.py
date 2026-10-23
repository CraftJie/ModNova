"""配置持久化。

放用户目录而不是项目目录 —— 程序可能被拷到别的机器、别的目录，配置应该跟着人走。

    Windows    %APPDATA%\\ModbusTool\\config.json
    Linux/mac  ~/.config/modbus-tool/config.json

两条纪律：
1. 写入用「临时文件 + 原子替换」，半截写入不会毁掉原配置；
2. 读到损坏的 JSON 不抛异常 —— 备份成 .bad 后回到默认值。配置文件坏了就打不开
   工具，是最恼人的失败方式。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "version": 1,
    "link": {
        "mode": "tcp",
        "host": "127.0.0.1",
        "port": 502,
        "port_name": "",
        "baud": 9600,
        "parity": "N",
        "stopbits": 1,
        "unit": 1,
        "timeout": 2.0,
        "retries": 1,
    },
    "view": {
        "fc": 3,
        "address": 0,
        "count": 10,
        "dtype": "uint16",
        "byteorder": "ABCD",
        "scale": 1.0,
        "bias": 0.0,
        "unit": "",
        "address_mode": "protocol",
        # 字段级视图：非空时按字段解释，忽略上面那套整段设置
        "fields": [],
    },
    "presets": [],
}


def default_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "ModbusTool" / "config.json"


def _merge(base: dict, override: dict) -> dict:
    """把已存的配置叠到默认值上，缺的字段自动补齐（老版本配置不会因为加了新字段就崩）。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else default_path()
        # 界面线程和轮询任务线程都可能触发 save（改预设、断开时回写链路参数）
        self._save_lock = threading.Lock()
        self.data = self._load()

    # -- 读写 --------------------------------------------------------------

    def _load(self) -> dict:
        if not self.path.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("配置根节点不是对象")
            return _merge(DEFAULT_CONFIG, raw)
        except (OSError, ValueError):
            # 坏掉的配置留在旁边供排查，程序照常启动
            try:
                broken = self.path.with_suffix(self.path.suffix + ".bad")
                self.path.replace(broken)
            except OSError:
                pass
            return copy.deepcopy(DEFAULT_CONFIG)

    def save(self) -> None:
        with self._save_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 临时文件名带 pid + 线程号：固定 .tmp 的话，两个线程并发保存
            # 会互相覆盖半截文件，os.replace 搬过去的就是坏配置
            tmp = self.path.with_suffix(
                f"{self.path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)

    # -- 字段 --------------------------------------------------------------

    @property
    def link(self) -> dict:
        return self.data["link"]

    @property
    def view(self) -> dict:
        return self.data["view"]

    def update_link(self, values: dict) -> None:
        self.data["link"].update({k: v for k, v in values.items() if k in DEFAULT_CONFIG["link"]})

    def update_view(self, values: dict) -> None:
        self.data["view"].update({k: v for k, v in values.items() if k in DEFAULT_CONFIG["view"]})

    # -- 预设 --------------------------------------------------------------

    def list_presets(self) -> list[dict]:
        return list(self.data.get("presets") or [])

    def get_preset(self, name: str) -> dict | None:
        for preset in self.list_presets():
            if preset.get("name") == name:
                return preset
        return None

    def save_preset(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("预设名称不能为空")
        preset = {
            "name": name,
            "link": copy.deepcopy(self.link),
            "view": copy.deepcopy(self.view),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        presets = [p for p in self.list_presets() if p.get("name") != name]
        presets.append(preset)
        self.data["presets"] = presets
        self.save()
        return preset

    def apply_preset(self, name: str) -> dict:
        preset = self.get_preset(name)
        if preset is None:
            raise ValueError(f"没有名为 {name} 的预设")
        self.data["link"].update(preset.get("link") or {})
        self.data["view"].update(preset.get("view") or {})
        self.save()
        return preset

    def delete_preset(self, name: str) -> bool:
        before = len(self.list_presets())
        self.data["presets"] = [p for p in self.list_presets() if p.get("name") != name]
        if len(self.data["presets"]) == before:
            return False
        self.save()
        return True
