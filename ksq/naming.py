"""File-name matching rules for zip bundles and config discovery."""

from __future__ import annotations

from pathlib import Path

from ksq.constants import (
    PICK_STRATEGY_FILE_PREFIX,
    SHELVES_FILE_PREFIX,
    TOOL_MAPPING_FILE_PREFIX,
    UNAVAILABLE_FILE_PREFIXES,
)


def file_stem_lower(file_name: str) -> str:
    return Path(file_name).stem.lower()


def matches_file_prefix(file_name: str, prefix: str, suffix: str) -> bool:
    lowered = file_name.lower()
    if not lowered.endswith(suffix.lower()):
        return False
    return file_stem_lower(file_name).startswith(prefix.lower())


def is_shelves_file_name(file_name: str) -> bool:
    return matches_file_prefix(file_name, SHELVES_FILE_PREFIX, ".csv")


def is_tool_mapping_file_name(file_name: str) -> bool:
    return matches_file_prefix(file_name, TOOL_MAPPING_FILE_PREFIX, ".json")


def is_pick_strategy_file_name(file_name: str) -> bool:
    return matches_file_prefix(file_name, PICK_STRATEGY_FILE_PREFIX, ".json")


def is_unavailable_file_name(file_name: str) -> bool:
    return any(
        matches_file_prefix(file_name, prefix, ".json")
        for prefix in UNAVAILABLE_FILE_PREFIXES
    )


def is_optional_config_file_name(file_name: str) -> bool:
    return (
        is_tool_mapping_file_name(file_name)
        or is_pick_strategy_file_name(file_name)
        or is_unavailable_file_name(file_name)
    )


def is_order_config_prod_file_name(file_name: str) -> bool:
    return file_name.lower() in {
        "order_config.prod.json",
        "order_config_prod.json",
    }


def is_order_config_file_name(file_name: str) -> bool:
    lowered = file_name.lower()
    if is_order_config_prod_file_name(file_name):
        return False
    return lowered in {"order_config.json", "order_config.test.json"}


def is_knowledge_member(member_name: str, file_name: str) -> bool:
    lowered = file_name.lower()
    if not lowered.endswith(".json"):
        return False
    if is_optional_config_file_name(file_name):
        return False
    if is_order_config_file_name(file_name) or is_order_config_prod_file_name(
        file_name
    ):
        return False
    normalized = member_name.replace("\\", "/").lower()
    if "/knowledge/" in f"/{normalized}" or normalized.startswith("knowledge/"):
        return True
    return not is_optional_config_file_name(file_name)


def classify_import_kind(file_name: str, member_path: str = "") -> str:
    """Return import bucket for a basename / zip member path."""
    name = Path(file_name).name
    if is_shelves_file_name(name):
        return "shelves"
    if is_unavailable_file_name(name):
        return "unavailable"
    if is_tool_mapping_file_name(name):
        return "tool_mapping"
    if is_pick_strategy_file_name(name):
        return "pick_strategy"
    if is_order_config_prod_file_name(name):
        return "order_config_prod"
    if is_order_config_file_name(name):
        return "order_config"
    if is_knowledge_member(member_path or name, name):
        return "knowledge"
    return "unknown"
