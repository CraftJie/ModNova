"""运行日志与崩溃记录（applog）的测试。

落盘是「事后追查」的唯一证据链，这里锁住三件事：文件真的写出来、
滚动配置不抛错、崩溃钩子把堆栈交给 logger 和通知回调。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import unittest
from pathlib import Path

from modbus_tool import applog


class TestSetupLogging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp.name
        self.logger = logging.getLogger(applog._LOGGER_NAME)
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        self.tmp.cleanup()

    def test_creates_file_and_writes(self):
        logger = applog.setup_logging()
        logger.info("hello-log")
        for handler in logger.handlers:
            handler.flush()
        path = applog.log_file()
        self.assertTrue(path.exists())
        self.assertIn("hello-log", path.read_text(encoding="utf-8", errors="replace"))

    def test_repeated_setup_reuses_logger(self):
        first = applog.setup_logging()
        again = applog.setup_logging()
        self.assertIs(first, again)
        # 只挂一个文件 handler，重复调用不会叠加
        file_handlers = [h for h in first.handlers
                         if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(file_handlers), 1)

    def test_unwritable_dir_does_not_raise(self):
        # APPDATA 指到一个文件上：目录建不出来，也绝不能抛
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        os.environ["APPDATA"] = str(blocker)
        logger = applog.setup_logging()      # 不应抛
        logger.info("still-alive")           # 也不应因 handler 缺失而崩


class TestExcepthook(unittest.TestCase):
    class _FakeLogger:
        def __init__(self):
            self.records = []

        def error(self, msg, *args):
            self.records.append(msg % args if args else msg)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_hook = sys.excepthook

    def tearDown(self):
        sys.excepthook = self.saved_hook
        self.tmp.cleanup()

    def test_hook_logs_and_notifies(self):
        logger = self._FakeLogger()
        crashes = []
        applog.install_excepthook(logger, on_crash=lambda m, d: crashes.append((m, d)))
        try:
            raise RuntimeError("boom-123")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(len(logger.records), 1)
        self.assertIn("boom-123", logger.records[0])
        self.assertEqual(len(crashes), 1)
        self.assertIn("boom-123", crashes[0][0])
        self.assertIn("RuntimeError", crashes[0][1])

    def test_root_callback_hook_installed(self):
        logger = self._FakeLogger()
        root = type("FakeRoot", (), {})()
        applog.install_excepthook(logger, root=root)
        self.assertTrue(callable(getattr(root, "report_callback_exception")))

    def test_on_crash_failure_swallowed(self):
        """通知界面失败绝不能让钩子自己再炸一次。"""
        logger = self._FakeLogger()

        def broken(_m, _d):
            raise OSError("tk is gone")

        applog.install_excepthook(logger, on_crash=broken)
        try:
            raise ValueError("still-logged")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(len(logger.records), 1)


if __name__ == "__main__":
    unittest.main()
