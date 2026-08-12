"""Filter matching against knowledge records."""

from __future__ import annotations

import json
from collections.abc import Iterable

from ksq.models import Dataset


def parse_filter(filter_expression: str) -> tuple[str, object]:
    if "=" not in filter_expression:
        raise ValueError(
            f"筛选条件格式错误：{filter_expression!r}。"
            "应使用“字段=值”，例如“几何形状=片状”。"
        )

    field_name, raw_value = filter_expression.split("=", maxsplit=1)
    if not field_name or not raw_value:
        raise ValueError(
            f"筛选条件格式错误：{filter_expression!r}。字段和值都不能为空。"
        )

    return field_name, parse_filter_value(raw_value)


def parse_filter_value(raw_value: str) -> object:
    stripped = raw_value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "false", "null"}:
        return json.loads(lowered)

    if stripped.isdigit() and len(stripped) >= 8:
        return stripped

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def values_match(actual_value: object, expected_value: object) -> bool:
    if actual_value == expected_value:
        return True

    if isinstance(actual_value, bool) or isinstance(expected_value, bool):
        return False

    if isinstance(actual_value, (int, float)) and isinstance(expected_value, str):
        try:
            return actual_value == json.loads(expected_value)
        except json.JSONDecodeError:
            return str(actual_value) == expected_value

    if isinstance(expected_value, (int, float)) and isinstance(actual_value, str):
        if actual_value == str(expected_value):
            return True
        if isinstance(expected_value, float) and expected_value.is_integer():
            return actual_value == str(int(expected_value))
        if isinstance(expected_value, int):
            return actual_value == str(expected_value)
        return False

    return False


def matches_filters(
    knowledge: dict[str, object], filters: Iterable[tuple[str, object]]
) -> bool:
    for field_name, expected_value in filters:
        if field_name not in knowledge:
            return False

        actual_value = knowledge[field_name]
        if isinstance(actual_value, list):
            if not any(values_match(item, expected_value) for item in actual_value):
                return False
        elif not values_match(actual_value, expected_value):
            return False
    return True


def record_matches(
    knowledge: dict[str, object],
    filters: list[tuple[str, object]],
    match_mode: str,
) -> bool:
    if match_mode not in {"and", "or"}:
        raise ValueError(f"不支持的匹配模式：{match_mode!r}，仅支持 and/or。")
    if not filters:
        return True
    if match_mode == "and":
        return matches_filters(knowledge, filters)
    return any(matches_filters(knowledge, [single_filter]) for single_filter in filters)


def find_matching_ids(
    knowledge_records: tuple[dict[str, object], ...],
    filters: list[tuple[str, object]],
    match_mode: str,
) -> list[str]:
    matching_ids: list[str] = []
    seen: set[str] = set()
    for knowledge in knowledge_records:
        if not record_matches(knowledge, filters, match_mode):
            continue
        item_id = knowledge["id"]
        if not isinstance(item_id, str):
            raise ValueError("knowledge 记录中的 id 必须是字符串。")
        if item_id in seen:
            continue
        seen.add(item_id)
        matching_ids.append(item_id)
    return matching_ids


def query_dataset(
    dataset: Dataset,
    filters: list[tuple[str, object]],
    match_mode: str,
) -> list[tuple[str, str, str]]:
    # Shelf data is the subject: knowledge filters narrow the on-shelf SKUs,
    # dictionary-only SKUs are not results. With no filters every on-shelf SKU
    # matches, including those the dictionary does not cover.
    if filters:
        matching_ids: list[str] = [
            item_id
            for item_id in find_matching_ids(
                dataset.knowledge_records, filters, match_mode
            )
            if item_id in dataset.shelf_entries
        ]
    else:
        matching_ids = sorted(dataset.shelf_entries)

    rows: list[tuple[str, str, str]] = []
    for item_id in matching_ids:
        for entry in dataset.shelf_entries[item_id]:
            rows.append((item_id, entry.name, entry.location))
    return rows
