"""Optional side data: tool mapping, closed-loop, unavailable list."""

from __future__ import annotations

import json
from pathlib import Path

from ksq.constants import DEFAULT_TOOL_NAME


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


def resolve_tool_name(item_id: str, tool_mapping: dict[str, str] | None) -> str:
    if tool_mapping is None:
        return "-"
    return tool_mapping.get(item_id, DEFAULT_TOOL_NAME)


def resolve_closed_loop_label(
    item_id: str, closed_loop_ids: frozenset[str] | None
) -> str:
    if closed_loop_ids is None:
        return "-"
    return "是" if item_id in closed_loop_ids else "否"


def resolve_unavailable_label(
    item_id: str, unavailable_ids: frozenset[str] | None
) -> str:
    if unavailable_ids is None:
        return "-"
    return "是" if item_id in unavailable_ids else "否"


def is_closed_loop(item_id: str, closed_loop_ids: frozenset[str]) -> bool:
    return item_id in closed_loop_ids
