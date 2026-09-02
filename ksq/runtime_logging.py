"""运行时日志配置。

日志同时写入标准错误（容器日志）和可轮转文件（现场排查）。文件写入失败
不会阻断服务启动，避免日志目录权限问题变成服务不可用。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

from ksq.constants import RUNTIME_LOG_FILE

LOGGER_NAME = "ksq"
_CONFIG_LOCK = threading.Lock()
_CONFIGURED_PATH: Optional[Path] = None
_PREVIOUS_EXCEPTHOOK = sys.excepthook
_PREVIOUS_THREADING_EXCEPTHOOK = threading.excepthook


def configure(log_file: Path = RUNTIME_LOG_FILE) -> Path:
    """配置服务日志并返回实际使用的日志文件路径。"""

    global _CONFIGURED_PATH
    path = Path(log_file).expanduser()
    with _CONFIG_LOCK:
        if _CONFIGURED_PATH == path:
            return path
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S%z",
        )
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError:
            logger.warning("无法写入运行日志文件：%s", path, exc_info=True)
        else:
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        def _uncaught_exception(
            exception_type: type[BaseException],
            value: BaseException,
            traceback: object,
        ) -> None:
            logger.error(
                "未捕获异常",
                exc_info=(exception_type, value, traceback),
            )
            _PREVIOUS_EXCEPTHOOK(exception_type, value, traceback)

        def _thread_exception(args: threading.ExceptHookArgs) -> None:
            logger.error(
                "线程未捕获异常 thread=%s",
                args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            _PREVIOUS_THREADING_EXCEPTHOOK(args)

        sys.excepthook = _uncaught_exception
        threading.excepthook = _thread_exception
        _CONFIGURED_PATH = path
    return path


def get_logger(name: str = "") -> logging.Logger:
    """返回服务日志 logger；启动入口会负责初始化 handler。"""

    suffix = str(name or "").strip(".")
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
