"""Command-line entry points for the knowledge shelf query product."""

from __future__ import annotations

import argparse
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

from ksq.constants import (
    DEFAULT_CONFIG_PNP_DIR,
    DEFAULT_KNOWLEDGE,
    DEFAULT_PICK_STRATEGY,
    DEFAULT_SHELVES,
    DEFAULT_TOOL_MAPPING,
    DEFAULT_UNAVAILABLE,
    HOST,
    PORT,
)
from ksq.dataset import build_dataset
from ksq.package_io import save_package
from ksq.state_reset import reset_state_if_version_changed
from ksq.web import state
from ksq.web.handlers import QueryHandler
from ksq.web.loader import (
    apply_configured_paths_reload,
    configured_paths_ready,
    existing_optional_path,
)


def serve(arguments: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="本机 Knowledge 库位查询服务")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--knowledge", default=str(DEFAULT_KNOWLEDGE))
    # Use default=None for the four path arguments so we can distinguish
    # "user passed --shelves" from "relied on the default".  The fallback
    # to DEFAULT_* happens below; config.py may then override only those
    # fields the user did NOT explicitly set.
    parser.add_argument("--shelves", default=None)
    parser.add_argument("--unavailable", default=None)
    parser.add_argument("--tool-mapping", default=None)
    parser.add_argument("--pick-strategy", default=None)
    parser.add_argument(
        "--config-pnp", default=str(DEFAULT_CONFIG_PNP_DIR)
    )
    parsed = parser.parse_args(arguments)
    state.configured_knowledge = Path(parsed.knowledge).expanduser().resolve()
    state.configured_config_pnp = Path(parsed.config_pnp).expanduser().resolve()
    # Track which path args the user explicitly passed.
    explicit: set[str] = set()
    if parsed.shelves is not None:
        explicit.add("shelves")
    if parsed.unavailable is not None:
        explicit.add("unavailable")
    if parsed.tool_mapping is not None:
        explicit.add("tool_mapping")
    if parsed.pick_strategy is not None:
        explicit.add("pick_strategy")
    state._explicit_config_keys = frozenset(explicit)
    # Apply defaults for arguments the user did not explicitly provide.
    state.configured_shelves = Path(
        parsed.shelves or str(DEFAULT_SHELVES)
    ).expanduser().resolve()
    state.configured_unavailable = existing_optional_path(
        parsed.unavailable or str(DEFAULT_UNAVAILABLE)
    )
    state.configured_tool_mapping = existing_optional_path(
        parsed.tool_mapping or str(DEFAULT_TOOL_MAPPING)
    )
    state.configured_pick_strategy = existing_optional_path(
        parsed.pick_strategy or str(DEFAULT_PICK_STRATEGY)
    )
    # config.py overrides only fields the user did NOT explicitly pass.
    state.reload_config_pnp_paths()
    print(f"默认 Knowledge 路径：{state.configured_knowledge}")
    print(f"默认 Shelves 路径：{state.configured_shelves}")
    print(f"默认不可处理列表：{state.configured_unavailable or '未提供'}")
    print(f"默认工具映射：{state.configured_tool_mapping or '未提供'}")
    print(f"默认闭环吸取列表：{state.configured_pick_strategy or '未提供'}")
    # Reset state files when the application version changes (e.g. after
    # a .bin update).  This runs before the HTTP server starts so there is
    # no concurrent access to the state files.  Must not block startup.
    reset_state_if_version_changed()

    if configured_paths_ready():
        with state.DATASET_LOCK:
            loaded = apply_configured_paths_reload()
        print(
            f"已自动从本机路径加载：{loaded['count']} 条"
            f"（{loaded['elapsed_seconds']}s）"
        )
    else:
        print("本机路径未就绪，请在页面中加载数据。")
    server = ThreadingHTTPServer((parsed.host, parsed.port), QueryHandler)
    print(f"本机打开：http://127.0.0.1:{parsed.port}")
    server.serve_forever()


def build_package_main(arguments: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="构建 knowledge 库位查询数据包")
    parser.add_argument("--knowledge", required=True, help="knowledge JSON 目录路径")
    parser.add_argument("--shelves", required=True, help="sku-shelves.csv 文件路径")
    parser.add_argument(
        "--output",
        default="data.kpkg",
        help="输出数据包路径，默认 data.kpkg",
    )
    parsed = parser.parse_args(arguments)

    knowledge_directory = Path(parsed.knowledge).expanduser().resolve()
    shelves_file = Path(parsed.shelves).expanduser().resolve()
    output_file = Path(parsed.output).expanduser().resolve()

    started = time.perf_counter()
    dataset = build_dataset(knowledge_directory, shelves_file)
    save_package(dataset, output_file)
    elapsed = time.perf_counter() - started

    print(f"已生成：{output_file}")
    print(dataset.report.summary())
    if dataset.report.duplicate_knowledge_files:
        print(
            "重复 knowledge 文件：",
            ", ".join(dataset.report.duplicate_knowledge_files),
        )
    if dataset.report.filename_id_mismatches:
        print(
            "文件名与 id 不一致：",
            ", ".join(dataset.report.filename_id_mismatches),
        )
    if dataset.report.conflicting_knowledge_ids:
        print(
            "同 id 内容冲突：",
            ", ".join(dataset.report.conflicting_knowledge_ids),
        )
    print(f"文件大小：{output_file.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"耗时：{elapsed:.2f} 秒")
    return 0


def main(arguments: Optional[List[str]] = None) -> None:
    serve(arguments)


if __name__ == "__main__":
    main()
