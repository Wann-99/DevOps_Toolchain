"""服务自身运行日志的最小自检。"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from ksq import runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
    def test_writes_structured_message_to_rotating_file(self) -> None:
        logger = logging.getLogger(runtime_logging.LOGGER_NAME)
        old_handlers = list(logger.handlers)
        old_path = runtime_logging._CONFIGURED_PATH
        old_hook = sys.excepthook
        old_thread_hook = threading.excepthook
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "service.log"
                runtime_logging.configure(path)
                runtime_logging.get_logger("test").warning("self-check message")
                for handler in logger.handlers:
                    handler.flush()
                content = path.read_text(encoding="utf-8")
                self.assertIn("WARNING", content)
                self.assertIn("self-check message", content)
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            for handler in old_handlers:
                logger.addHandler(handler)
            runtime_logging._CONFIGURED_PATH = old_path
            sys.excepthook = old_hook
            threading.excepthook = old_thread_hook


if __name__ == "__main__":
    unittest.main()
