"""Configured data paths and their display values."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from ksq.constants import DEFAULT_KNOWLEDGE, DEFAULT_KNOWLEDGE_ROOT
from ksq.data import state


def path_field_bases() -> Tuple[Optional[Path], Optional[Path]]:
    # In the mounted-root layout the root, rather than the current target
    # directory, is the base for all relative scene paths.
    knowledge_base = state.configured_knowledge_root
    if knowledge_base is None:
        knowledge_base = state._cli_config_paths.get("knowledge")
    if knowledge_base is None and state.configured_vfm_app is not None:
        knowledge_base = state.configured_vfm_app / "model/templates"
    if knowledge_base is None and state.configured_knowledge == DEFAULT_KNOWLEDGE:
        knowledge_base = DEFAULT_KNOWLEDGE_ROOT
    return knowledge_base, state.configured_config_pnp


def path_field_display(path: Optional[Path], base: Optional[Path]) -> str:
    if path is None:
        return ""
    if base is not None:
        try:
            return str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            pass
    return str(path)


def configured_path_field_values() -> Dict[str, str]:
    knowledge_base, config_base = path_field_bases()
    return {
        "knowledge": path_field_display(
            state.configured_knowledge, knowledge_base
        ),
        "shelves": path_field_display(state.configured_shelves, config_base),
        "unavailable": path_field_display(
            state.configured_unavailable, config_base
        ),
        "tool_mapping": path_field_display(
            state.configured_tool_mapping, config_base
        ),
        "pick_strategy": path_field_display(
            state.configured_pick_strategy, config_base
        ),
    }
