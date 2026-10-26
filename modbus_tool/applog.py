"""应用运行日志与崩溃记录。

报文日志在界面上看只是「当场」，调试工具的价值恰恰在「事后」——
现场挂机两小时抓偶发故障，程序被关掉或崩掉，内存里的日志全没了。
这个模块把运行事件与崩溃堆栈落成滚动文件：

    <APPDATA>/ModNova/logs/modnova.log        运行事件 + 报文（2MB × 3 份滚动）

设计约束：落盘失败绝不能挡住程序运行 —— 没有日志文件顶多丢证据，
起不来界面就是事故了。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

_LOGGER_NAME = "modnova"
MAX_BYTES = 2_000_000     # 单文件 2MB
BACKUP_COUNT = 3          # 连同本体最多留 4 份


def log_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "ModNova" / "logs"


def log_file() -> Path:
    return log_dir() / "modnova.log"


def setup_logging() -> logging.Logger:
    """初始化滚动文件日志。重复调用直接返回已有的 logger。"""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    except OSError:
        # 目录建不出来（只读盘/权限）：内存日志照常，文件侧放弃
        pass
    return logger


def set_file_logging(enabled: bool) -> logging.Logger | None:
    """开/关「运行日志落盘」。默认关闭 —— 落盘是可选项，界面日志永远照常。

    开：等价 setup_logging()；关：摘掉全部 handler 并返回 None。
    关了再开也没问题（setup_logging 按 handler 判空会重新挂）。
    """
    if enabled:
        return setup_logging()
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)
    return None


def install_excepthook(logger: logging.Logger | None = None, root=None,
                       on_crash=None, get_logger=None) -> None:
    """把未捕获异常写进日志，可选地通知界面。

    Tk 的回调异常不走 sys.excepthook，而走 root.report_callback_exception ——
    两处都要挂，否则按钮回调里的崩溃永远落不了盘。
    logger 允许为 None（文件日志默认关闭）；传 get_logger 时每次崩溃现取
    当前 logger —— 界面里「存文件」中途打开，之后的崩溃也接得住。
    on_crash 只在主线程的 Tk 回调语境里被调用，直接更新状态栏是安全的。
    """

    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        active = get_logger() if get_logger is not None else logger
        try:
            if active is not None:
                active.error("未捕获异常\n%s", text)
        except Exception:
            pass
        try:                       # 原始 stderr 仍然要有一份，控制台跑的时候看得见
            if sys.__stderr__ is not None:     # pythonw / 打包成窗口程序时它可能是 None
                sys.__stderr__.write(text)
        except Exception:
            pass
        if on_crash is not None:
            try:
                on_crash(f"程序内部错误：{exc_value}", text)
            except Exception:
                pass               # 通知界面失败也不能让 hook 自己再炸一次

    sys.excepthook = hook
    if root is not None:
        root.report_callback_exception = hook
