#!/usr/bin/env python3
"""把 knowledge 目录和库位表打包成单个高速数据包 (.kpkg)。"""

from __future__ import annotations

import sys

from ksq.cli import build_package_main


if __name__ == "__main__":
    try:
        raise SystemExit(build_package_main(sys.argv[1:]))
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1)
