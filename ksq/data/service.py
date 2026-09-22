"""Dataset loading transactions, path validation and failure rollback."""

from __future__ import annotations

from typing import Dict

from ksq.data import state as state
from ksq.data.loader import CLOUD_SHELVES_URL, bundle_path_map, configured_bundle, parse_shelves_source, resolve_knowledge_path, resolve_input_path, parse_optional_path, load_from_configured_paths, apply_configured_paths_reload
from ksq.data.paths import path_field_bases, configured_path_field_values


_LOAD_PATH_STATE_FIELDS = (
    "configured_knowledge",
    "configured_knowledge_root",
    "configured_shelves",
    "shelves_source",
    "configured_unavailable",
    "configured_tool_mapping",
    "configured_pick_strategy",
    "_explicit_config_keys",
    "loaded_dataset",
    "loaded_tool_mapping",
    "loaded_closed_loop_ids",
    "loaded_unavailable_ids",
    "loaded_paths",
    "data_source_ready",
    "data_load_method",
    "edit_workspace",
    "data_revision",
)


def _snapshot_load_path_state() -> Dict[str, object]:
    return {
        name: getattr(state, name)
        for name in _LOAD_PATH_STATE_FIELDS
    }


def _restore_load_path_state(snapshot: Dict[str, object]) -> None:
    for name, value in snapshot.items():
        setattr(state, name, value)


def load_paths(payload):
    shelves_source = parse_shelves_source(payload.get("shelves_source", "local"))
    knowledge_raw = payload.get("knowledge")
    shelves_raw = payload.get("shelves")
    if not isinstance(knowledge_raw, str) or not knowledge_raw.strip():
        raise ValueError("knowledge 路径不能为空。")
    if shelves_source == "local" and (not isinstance(shelves_raw, str) or not shelves_raw.strip()):
        raise ValueError("shelves 路径不能为空。")
    knowledge_base, config_base = path_field_bases()
    knowledge_path = resolve_knowledge_path(
        knowledge_raw, knowledge_base
    )
    shelves_path = (
        resolve_input_path(shelves_raw, "库位表", config_base)
        if shelves_source == "local" else None
    )
    if not knowledge_path.is_dir():
        raise FileNotFoundError(f"Knowledge 目录不存在：{knowledge_path}")
    if shelves_path is not None and not shelves_path.is_file():
        raise FileNotFoundError(f"库位表不存在：{shelves_path}")
    unavailable_path = parse_optional_path(
        payload.get("unavailable"), "不可处理列表", config_base
    )
    tool_mapping_path = parse_optional_path(
        payload.get("tool_mapping"), "工具映射", config_base
    )
    pick_strategy_path = parse_optional_path(
        payload.get("pick_strategy"), "闭环吸取列表", config_base
    )
    with state.DATASET_LOCK:
        snapshot = _snapshot_load_path_state()
        try:
            state.configured_knowledge = knowledge_path
            state.shelves_source = shelves_source
            if shelves_path is not None:
                state.configured_shelves = shelves_path
            state.configured_unavailable = unavailable_path
            state.configured_tool_mapping = tool_mapping_path
            state.configured_pick_strategy = pick_strategy_path
            # Mark user-submitted non-empty paths as explicit so
            # reload_config_pnp_paths() preserves them over config.py.
            explicit_keys = {"knowledge"}
            if shelves_path is not None:
                explicit_keys.add("shelves")
            if unavailable_path is not None:
                explicit_keys.add("unavailable")
            if tool_mapping_path is not None:
                explicit_keys.add("tool_mapping")
            if pick_strategy_path is not None:
                explicit_keys.add("pick_strategy")
            state._explicit_config_keys = (
                state._explicit_config_keys | explicit_keys
            )
            dataset, tool_mapping, closed_loop_ids, unavailable_ids, elapsed = (
                load_from_configured_paths()
            )
        except Exception:
            _restore_load_path_state(snapshot)
            raise
    return (dataset, elapsed, shelves_source, knowledge_path, shelves_path, unavailable_path, tool_mapping_path, pick_strategy_path, unavailable_ids)


def load_auto(shelves_source):
    with state.DATASET_LOCK:
        load_snapshot = _snapshot_load_path_state()
        try:
            # One-click load restores startup source paths.
            state.shelves_source = shelves_source
            state.configured_knowledge_root = state._cli_knowledge_root
            if state._cli_knowledge_path is not None:
                state.configured_knowledge = state._cli_knowledge_path
            for key, value in state._cli_config_paths.items():
                setattr(state, f"configured_{key}", value)
            state._explicit_config_keys = frozenset(
                state._cli_config_paths
            )
            result = apply_configured_paths_reload()
        except Exception:
            _restore_load_path_state(load_snapshot)
            raise
        paths = configured_path_field_values()
        result["source_paths"] = {
            key: str(path.resolve()) if path is not None else ""
            for key, path in bundle_path_map(configured_bundle()).items()
        }
        if shelves_source == "cloud":
            result["source_paths"]["shelves"] = CLOUD_SHELVES_URL
        dataset = state.loaded_dataset
        has_unavailable = (
            state.configured_unavailable is not None or bool(state.loaded_unavailable_ids)
        )
        has_tool_mapping = (
            state.configured_tool_mapping is not None
        )
        has_pick_strategy = (
            state.configured_pick_strategy is not None
        )
    return (result, paths, dataset, has_unavailable, has_tool_mapping, has_pick_strategy)
