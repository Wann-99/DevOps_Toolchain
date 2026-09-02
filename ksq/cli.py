"""Command-line entry points for the knowledge shelf query product."""

from __future__ import annotations

import argparse
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional

from ksq.constants import (
    DEFAULT_CONFIG_PNP_DIR,
    DEFAULT_KNOWLEDGE,
    DEFAULT_KNOWLEDGE_ROOT,
    DEFAULT_PICK_STRATEGY,
    DEFAULT_SHELVES,
    DEFAULT_TOOL_MAPPING,
    DEFAULT_UNAVAILABLE,
    DEFAULT_VFM_APP_DIR,
    HOST,
    PORT,
)
from ksq.dataset import build_dataset
from ksq.package_io import save_package
from ksq.state_reset import reset_state_if_version_changed
from ksq.runtime_logging import configure as configure_runtime_logging
from ksq.runtime_logging import get_logger
from ksq.web import dashboard_api, state
from ksq.web.handlers import QueryHandler
from ksq.web.loader import existing_optional_path, resolve_knowledge_path


LOGGER = get_logger("startup")


def serve(arguments: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="本机 Knowledge 库位查询服务")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    # ``--knowledge`` is the actual directory to read.  ``--knowledge-root``
    # is its relative-path/display base and is deliberately separate.
    parser.add_argument("--knowledge", default=None)
    parser.add_argument(
        "--knowledge-root",
        default=None,
        help="Knowledge 模板根目录；未指定 --knowledge 时默认读取其 knowledge 子目录",
    )
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
    parser.add_argument(
        "--vfm-app",
        default=None,
        help="VfmApp 部署目录；由其 config.yaml 的 template_root 定位 knowledge 目录",
    )
    parsed = parser.parse_args(arguments)
    runtime_log_file = configure_runtime_logging()
    LOGGER.info("启动服务，运行日志文件：%s", runtime_log_file)

    # A root supplied by deployment makes the default target unambiguous:
    # <root>/knowledge.  An old invocation with only --knowledge keeps using
    # that directory as its own base, so existing direct mounts remain valid.
    knowledge_root: Optional[Path]
    knowledge_path: Path
    if parsed.knowledge_root is not None:
        knowledge_root = Path(parsed.knowledge_root).expanduser().resolve()
        raw_knowledge = str(parsed.knowledge or "knowledge")
        knowledge_path = resolve_knowledge_path(raw_knowledge, knowledge_root)
    elif parsed.knowledge is not None:
        knowledge_path = Path(parsed.knowledge).expanduser().resolve()
        knowledge_root = knowledge_path
    elif parsed.vfm_app is None:
        knowledge_root = DEFAULT_KNOWLEDGE_ROOT
        knowledge_path = DEFAULT_KNOWLEDGE
    else:
        # Explicit --vfm-app retains the legacy config.yaml-driven mode.
        knowledge_root = None
        knowledge_path = DEFAULT_KNOWLEDGE

    state.configured_knowledge = knowledge_path
    state.configured_knowledge_root = knowledge_root
    state.configured_config_pnp = Path(parsed.config_pnp).expanduser().resolve()
    state.configured_vfm_app = Path(
        parsed.vfm_app or str(DEFAULT_VFM_APP_DIR)
    ).expanduser().resolve()
    state._cli_knowledge_root = knowledge_root
    state._cli_knowledge_path = knowledge_path if knowledge_root is not None else None
    # Track which path args the user explicitly passed.
    explicit: set[str] = set()
    if parsed.knowledge is not None:
        explicit.add("knowledge")
    if parsed.shelves is not None:
        explicit.add("shelves")
    if parsed.unavailable is not None:
        explicit.add("unavailable")
    if parsed.tool_mapping is not None:
        explicit.add("tool_mapping")
    if parsed.pick_strategy is not None:
        explicit.add("pick_strategy")
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
    state._cli_config_paths = {
        key: getattr(state, f"configured_{key}") for key in explicit
    }
    state._explicit_config_keys = frozenset(state._cli_config_paths)
    # config.py 与 VfmApp config.yaml 只覆盖未显式传入的字段。
    state.reload_config_pnp_paths()
    LOGGER.info("默认 Knowledge 路径：%s", state.configured_knowledge)
    LOGGER.info("默认 Shelves 路径：%s", state.configured_shelves)
    LOGGER.info("默认不可处理列表：%s", state.configured_unavailable or "未提供")
    LOGGER.info("默认工具映射：%s", state.configured_tool_mapping or "未提供")
    LOGGER.info("默认闭环吸取列表：%s", state.configured_pick_strategy or "未提供")
    # Reset state files when the application version changes (e.g. after
    # a .bin update).  This runs before the HTTP server starts so there is
    # no concurrent access to the state files.  Must not block startup.
    reset_state_if_version_changed()

    LOGGER.info("数据尚未加载，请在页面中按需加载。")
    server = ThreadingHTTPServer((parsed.host, parsed.port), QueryHandler)
    LOGGER.info("服务已监听：http://%s:%s", parsed.host, parsed.port)
    try:
        dashboard_api.start_dashboard_monitor()
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("收到退出信号，服务停止")
    finally:
        dashboard_api.stop_dashboard_monitor()
        server.server_close()


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
