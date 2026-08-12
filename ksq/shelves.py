"""Parse sku-shelves CSV into location entries."""

from __future__ import annotations

import csv
from io import TextIOWrapper
from pathlib import Path

from ksq.models import ShelfEntry, ShelfParseResult

# Fields compared when two CSV rows land on the same (sku, location).
# A differing non-empty value means the merge silently drops one of them.
MERGE_CONFLICT_FIELDS = (
    ("out_item_id", "商品编码"),
    ("shelf_attribute", "货架属性"),
    ("baffle_height", "挡板高度"),
)


def format_shelf_location(shelf_number: str, level: str, bin_unit: str) -> str:
    if not shelf_number or not level or not bin_unit:
        raise ValueError(
            f"库位字段不完整：shelf_number={shelf_number!r}, "
            f"level={level!r}, bin_unit={bin_unit!r}"
        )
    return f"{shelf_number}-{level}-{bin_unit}"


def describe_merge_conflicts(
    item_id: str,
    existing: ShelfEntry,
    incoming: ShelfEntry,
    row_number: int,
) -> list[str]:
    """Report fields where merging would silently discard a differing value.

    The merge itself keeps first-come-wins; this only surfaces what was dropped
    so the source CSV can be fixed.
    """
    conflicts: list[str] = []
    for attribute, label in MERGE_CONFLICT_FIELDS:
        kept = getattr(existing, attribute)
        dropped = getattr(incoming, attribute)
        if kept and dropped and kept != dropped:
            conflicts.append(
                f"{item_id} @ {existing.location} 第 {row_number} 行 "
                f"{label}：保留 {kept}，忽略 {dropped}"
            )
    return conflicts


def merge_shelf_entry(existing: ShelfEntry, incoming: ShelfEntry) -> ShelfEntry:
    name = existing.name
    if name == "未命名" and incoming.name != "未命名":
        name = incoming.name
    shelf_attribute = existing.shelf_attribute or incoming.shelf_attribute
    baffle_height = existing.baffle_height or incoming.baffle_height
    out_item_id = existing.out_item_id or incoming.out_item_id
    return ShelfEntry(
        location=existing.location,
        name=name,
        shelf_attribute=shelf_attribute,
        baffle_height=baffle_height,
        out_item_id=out_item_id,
    )


def parse_shelf_locations(file_object: TextIOWrapper) -> ShelfParseResult:
    reader = csv.DictReader(file_object)
    required_columns = {"sku_code", "name", "shelf_number", "level", "bin_unit"}
    if reader.fieldnames is None or not required_columns.issubset(set(reader.fieldnames)):
        raise ValueError(f"库位表缺少必要列：{', '.join(sorted(required_columns))}")

    shelf_entries: dict[str, list[ShelfEntry]] = {}
    skipped_empty_sku_count = 0
    shelf_row_count = 0
    mapped_row_count = 0
    merge_conflicts: list[str] = []

    for row_number, row in enumerate(reader, start=2):
        shelf_row_count += 1
        item_id = (row.get("sku_code") or "").strip()
        if not item_id:
            skipped_empty_sku_count += 1
            continue

        try:
            location = format_shelf_location(
                (row.get("shelf_number") or "").strip(),
                (row.get("level") or "").strip(),
                (row.get("bin_unit") or "").strip(),
            )
        except ValueError as error:
            raise ValueError(f"库位表第 {row_number} 行无效：{error}") from error

        name = (row.get("name") or "").strip() or "未命名"
        shelf_attribute = (row.get("shelf_attribute") or "").strip()
        baffle_height = (row.get("baffle_height") or "").strip()
        out_item_id = (row.get("out_item_id") or "").strip()
        incoming = ShelfEntry(
            location=location,
            name=name,
            shelf_attribute=shelf_attribute,
            baffle_height=baffle_height,
            out_item_id=out_item_id,
        )
        entries = shelf_entries.setdefault(item_id, [])
        existing_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry.location == location
            ),
            -1,
        )
        if existing_index < 0:
            entries.append(incoming)
        else:
            merge_conflicts.extend(
                describe_merge_conflicts(
                    item_id, entries[existing_index], incoming, row_number
                )
            )
            entries[existing_index] = merge_shelf_entry(entries[existing_index], incoming)
        mapped_row_count += 1

    frozen = {item_id: tuple(entries) for item_id, entries in shelf_entries.items()}
    return ShelfParseResult(
        entries=frozen,
        skipped_empty_sku_count=skipped_empty_sku_count,
        row_count=shelf_row_count,
        mapped_row_count=mapped_row_count,
        merge_conflicts=tuple(merge_conflicts),
    )


def load_shelf_locations(shelves_file: Path) -> ShelfParseResult:
    if not shelves_file.is_file():
        raise FileNotFoundError(f"库位表不存在：{shelves_file}")

    with shelves_file.open(encoding="utf-8-sig", newline="") as file:
        return parse_shelf_locations(file)
