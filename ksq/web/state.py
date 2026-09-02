"""Process-wide loaded dataset and path configuration."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Dict, FrozenSet, Optional

from ksq import config_pnp
from ksq import vfm_app
from ksq.constants import (
    DEFAULT_KNOWLEDGE,
    DEFAULT_PICK_STRATEGY,
    DEFAULT_SHELVES,
    DEFAULT_TOOL_MAPPING,
    DEFAULT_UNAVAILABLE,
)
from ksq.models import Dataset

DATASET_LOCK = Lock()
loaded_dataset: Optional[Dataset] = None
loaded_tool_mapping: Optional[Dict[str, str]] = None
loaded_closed_loop_ids: Optional[FrozenSet[str]] = None
loaded_unavailable_ids: Optional[FrozenSet[str]] = None
edit_workspace: Optional[Dict[str, object]] = None
order_access_token: Optional[str] = None
order_access_token_key: str = ""
order_access_tokens: Dict[str, str] = {}
configured_knowledge: Path = DEFAULT_KNOWLEDGE
# Optional root used to resolve/display relative Knowledge paths.  The actual
# dataset is always read from ``configured_knowledge``.
configured_knowledge_root: Optional[Path] = None
configured_shelves: Path = DEFAULT_SHELVES
configured_unavailable: Optional[Path] = DEFAULT_UNAVAILABLE
configured_tool_mapping: Optional[Path] = DEFAULT_TOOL_MAPPING
configured_pick_strategy: Optional[Path] = DEFAULT_PICK_STRATEGY
configured_config_pnp: Optional[Path] = None
configured_vfm_app: Optional[Path] = None
# CLI values survive page-driven overrides and are restored by one-click load.
_cli_config_paths: Dict[str, Optional[Path]] = {}
# Root/default target selected at startup.  These are separate from the
# explicitly supplied side-data paths above so one-click load can restore the
# default ``<root>/knowledge`` target without making old VfmApp mode explicit.
_cli_knowledge_root: Optional[Path] = None
_cli_knowledge_path: Optional[Path] = None
# Keys whose path was explicitly set via CLI --knowledge / --shelves /
# --unavailable / --tool-mapping / --pick-strategy, or typed on the load page.
# reload_config_pnp_paths() preserves these so device config never overrides an
# explicit choice.
_explicit_config_keys: FrozenSet[str] = frozenset()
data_source_ready: bool = False
# paths = full features; bundle = package preview (query only)
data_load_method: str = "none"
# Bumps when dataset / edit workspace changes so clients can refresh.
data_revision: int = 0

BUNDLE_CAPABILITY_MESSAGE = (
    "当前为包加载（仅查看）。如需使用该功能，请切换到「本机路径」加载。"
)


def bump_data_revision() -> int:
    global data_revision
    data_revision += 1
    return data_revision


def load_capabilities(method: Optional[str] = None) -> Dict[str, bool]:
    value = str(method if method is not None else data_load_method or "none")
    full = value == "paths"
    loaded = value != "none"
    return {
        "query": loaded,
        "order": full,
        "edit": full,
        "test_order": full,
    }


def require_full_data_source(action: str = "该操作") -> None:
    if data_load_method == "paths":
        return
    if data_load_method == "bundle":
        raise ValueError(f"{action}不支持。{BUNDLE_CAPABILITY_MESSAGE}")
    raise ValueError("尚未加载数据，请先返回首页加载。")


def reload_config_pnp_paths(
    skip_keys: Optional[FrozenSet[str]] = None,
    *,
    require_vfm_knowledge: bool = False,
) -> None:
    """Re-read device-side config and update configured_* paths.

    ``config_pnp/config.py`` is consulted for the four side-data files.  Legacy
    VfmApp mode may also derive the Knowledge directory from
    ``VfmApp_deploy/config.yaml``; when ``configured_knowledge_root`` is set,
    the mounted templates root is authoritative and VfmApp is not consulted.
    Only fields the device actually declares are updated; absent fields keep
    their current value.  Fields listed in *skip_keys* (or, when *skip_keys* is
    ``None``, the module-level ``_explicit_config_keys`` set) are preserved so
    that explicit CLI arguments and page input always take priority.
    When *require_vfm_knowledge* is true, an invalid VfmApp configuration is
    reported instead of silently retaining the fallback path.

    The caller must hold ``DATASET_LOCK`` when concurrency is possible
    (consistent with ``bump_data_revision``).
    """
    global configured_knowledge, configured_shelves, configured_unavailable
    global configured_tool_mapping, configured_pick_strategy

    skip = skip_keys if skip_keys is not None else _explicit_config_keys

    # Legacy VfmApp mode follows percept's template_root.  A mounted root is
    # self-contained and must never be replaced by an unavailable config.yaml.
    if "knowledge" not in skip and configured_knowledge_root is None:
        knowledge_dir = (
            None
            if configured_vfm_app is None
            else vfm_app.knowledge_directory(configured_vfm_app)
        )
        if knowledge_dir is not None:
            configured_knowledge = knowledge_dir
        elif require_vfm_knowledge:
            config_file = (
                "未配置 VfmApp 目录"
                if configured_vfm_app is None
                else str(configured_vfm_app / vfm_app.CONFIG_FILE_NAME)
            )
            raise ValueError(
                f"无法从 VfmApp 配置定位 Knowledge 目录：{config_file}。"
                "请检查 VFM_APP_DIR 挂载、template.template_root "
                "及目标目录。"
            )

    if configured_config_pnp is None:
        return

    parsed = config_pnp.load_config_pnp_paths(configured_config_pnp)

    if "shelves" in parsed and "shelves" not in skip:
        configured_shelves = parsed["shelves"]
    if "unavailable" in parsed and "unavailable" not in skip:
        configured_unavailable = parsed["unavailable"]
    if "tool_mapping" in parsed and "tool_mapping" not in skip:
        configured_tool_mapping = parsed["tool_mapping"]
    if "pick_strategy" in parsed and "pick_strategy" not in skip:
        configured_pick_strategy = parsed["pick_strategy"]
