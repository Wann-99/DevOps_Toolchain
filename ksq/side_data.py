"""Optional side data: tool mapping, closed-loop, unavailable list."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ksq.config_pnp import load_config_pnp_unavailable
from ksq.constants import DEFAULT_TOOL_NAME
from ksq.models import ShelfEntry


def unavailable_ids_from_config(
    config_pnp_dir: Path | None,
    shelf_entries: dict[str, tuple[ShelfEntry, ...]],
) -> set[str]:
    """Expand shelf/level/bin restrictions into the existing SKU blacklist."""
    def normalize(value: str) -> str:
        value = value.strip()
        return str(int(value)) if value.isdecimal() else value

    scopes: set[tuple[str, ...]] = set()
    compact_scopes: set[str] = set()
    for key, values in load_config_pnp_unavailable(config_pnp_dir).items():
        for value in values:
            if "-" in value:
                parts = value.split("-")
                if len(parts) not in (2, 3) or not all(part.strip() for part in parts):
                    raise ValueError(f"config.scene.{key} 应填写货架-层或货架-层-库位。")
            elif key == "unavailable_shelf_list":
                parts = [value]
            elif value.isdecimal() and len(value) in (4, 6, 8):
                # A level is the full location without its last two digits.
                compact_scopes.add(normalize(value))
                continue
            else:
                raise ValueError(f"config.scene.{key} 应填写 4/6/8 位库位码、货架-层或货架-层-库位。")
            scopes.add(tuple(normalize(part) for part in parts))

    unavailable: set[str] = set()
    for item_id, entries in shelf_entries.items():
        for entry in entries:
            parts = tuple(normalize(part) for part in entry.location.split("-"))
            if len(parts) != 3:
                continue
            compact_level = parts[0] + parts[1].zfill(2)
            compact_bin = compact_level + parts[2].zfill(2)
            if (
                any(scope in scopes for scope in (parts[:1], parts[:2], parts))
                or normalize(compact_level) in compact_scopes
                or normalize(compact_bin) in compact_scopes
            ):
                unavailable.add(item_id)
                break
    return unavailable


def load_tool_mapping(mapping_file: Path) -> dict[str, str]:
    if not mapping_file.is_file():
        raise FileNotFoundError(f"工具映射文件不存在：{mapping_file}")
    with mapping_file.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"工具映射根节点必须是对象：{mapping_file}")
    mapping: dict[str, str] = {}
    for raw_id, raw_tool in payload.items():
        item_id = str(raw_id).strip()
        tool_name = str(raw_tool).strip()
        if not item_id or not tool_name:
            continue
        mapping[item_id] = tool_name
    return mapping


def load_closed_loop_ids(strategy_file: Path) -> frozenset[str]:
    if not strategy_file.is_file():
        raise FileNotFoundError(f"吸取策略文件不存在：{strategy_file}")
    with strategy_file.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"吸取策略根节点必须是对象：{strategy_file}")
    raw_items = payload.get("closed_loop")
    if not isinstance(raw_items, list):
        raise ValueError(f"吸取策略缺少 closed_loop 数组：{strategy_file}")
    closed_loop_ids: set[str] = set()
    for item in raw_items:
        item_id = str(item).strip()
        if item_id:
            closed_loop_ids.add(item_id)
    return frozenset(closed_loop_ids)


def load_unavailable_ids(unavailable_file: Path) -> list[str]:
    if not unavailable_file.is_file():
        raise FileNotFoundError(f"不可处理列表不存在：{unavailable_file}")
    with unavailable_file.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"不可处理列表根节点必须是对象：{unavailable_file}")
    raw_items = payload.get("unavailable_obj")
    if not isinstance(raw_items, list):
        raise ValueError(f"不可处理列表缺少 unavailable_obj 数组：{unavailable_file}")
    unavailable_ids: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        item_id = str(item).strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        unavailable_ids.append(item_id)
    return unavailable_ids


def _identifiers(item_id: str, aliases: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys([item_id, *(value for value in aliases if value)]))


def resolve_tool_name(
    item_id: str,
    tool_mapping: dict[str, str] | None,
    aliases: Iterable[str] = (),
) -> str:
    if tool_mapping is None:
        return "-"
    return next(
        (tool_mapping[value] for value in _identifiers(item_id, aliases) if value in tool_mapping),
        DEFAULT_TOOL_NAME,
    )


def resolve_closed_loop_label(
    item_id: str,
    closed_loop_ids: frozenset[str] | None,
    aliases: Iterable[str] = (),
) -> str:
    if closed_loop_ids is None:
        return "-"
    return (
        "是"
        if any(value in closed_loop_ids for value in _identifiers(item_id, aliases))
        else "否"
    )


def resolve_unavailable_label(
    item_id: str,
    unavailable_ids: frozenset[str] | None,
    aliases: Iterable[str] = (),
) -> str:
    if unavailable_ids is None:
        return "-"
    return (
        "是"
        if any(value in unavailable_ids for value in _identifiers(item_id, aliases))
        else "否"
    )


def is_closed_loop(item_id: str, closed_loop_ids: frozenset[str]) -> bool:
    return item_id in closed_loop_ids
