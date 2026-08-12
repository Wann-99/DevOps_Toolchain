"""Process-wide loaded dataset and path configuration."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Dict, FrozenSet, Optional

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
configured_shelves: Path = DEFAULT_SHELVES
configured_unavailable: Optional[Path] = DEFAULT_UNAVAILABLE
configured_tool_mapping: Optional[Path] = DEFAULT_TOOL_MAPPING
configured_pick_strategy: Optional[Path] = DEFAULT_PICK_STRATEGY
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
