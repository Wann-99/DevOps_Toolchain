"""python -m ksq"""

from __future__ import annotations

from ksq.runtime_logging import configure as configure_runtime_logging
from ksq.runtime_logging import get_logger

configure_runtime_logging()
try:
    from ksq.cli import main
except BaseException:
    get_logger("bootstrap").exception("服务启动失败")
    raise

if __name__ == "__main__":
    main()
