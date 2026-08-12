"""Serialize and load .kpkg dataset packages."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from ksq.constants import PACKAGE_VERSION
from ksq.dataset import build_load_report
from ksq.knowledge import load_knowledge_from_mapping
from ksq.models import Dataset, ShelfEntry


def save_package(dataset: Dataset, package_file: Path) -> None:
    payload = {
        "version": PACKAGE_VERSION,
        "knowledge": list(dataset.knowledge_records),
        "shelves": {
            item_id: [
                {
                    "location": entry.location,
                    "name": entry.name,
                    "shelf_attribute": entry.shelf_attribute,
                    "baffle_height": entry.baffle_height,
                    "out_item_id": entry.out_item_id,
                }
                for entry in entries
            ]
            for item_id, entries in dataset.shelf_entries.items()
        },
        "report": {
            "knowledge_file_count": dataset.report.knowledge_file_count,
            "knowledge_record_count": dataset.report.knowledge_record_count,
            "duplicate_knowledge_files": list(dataset.report.duplicate_knowledge_files),
            "filename_id_mismatches": list(dataset.report.filename_id_mismatches),
            "shelf_row_count": dataset.report.shelf_row_count,
            "shelf_empty_sku_count": dataset.report.shelf_empty_sku_count,
            "shelf_mapped_row_count": dataset.report.shelf_mapped_row_count,
            "shelf_unique_sku_count": dataset.report.shelf_unique_sku_count,
            "multi_location_sku_count": dataset.report.multi_location_sku_count,
            "conflicting_knowledge_ids": list(dataset.report.conflicting_knowledge_ids),
            "shelves_without_knowledge": list(dataset.report.shelves_without_knowledge),
        },
    }
    package_file.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(package_file, "wt", encoding="utf-8", compresslevel=6) as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))


def load_package(package_file: Path) -> Dataset:
    if not package_file.is_file():
        raise FileNotFoundError(f"数据包不存在：{package_file}")

    with gzip.open(package_file, "rt", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"数据包格式错误：{package_file}")
    version = payload.get("version")
    if version not in {1, 2, PACKAGE_VERSION}:
        raise ValueError(f"不支持的数据包版本：{version}")

    knowledge = payload.get("knowledge")
    shelves = payload.get("shelves")
    if not isinstance(knowledge, list):
        raise ValueError("数据包缺少 knowledge 列表。")
    if not isinstance(shelves, dict):
        raise ValueError("数据包缺少 shelves 字典。")

    package_payloads: list[tuple[str, dict[str, object]]] = []
    for index, item in enumerate(knowledge):
        if not isinstance(item, dict):
            raise ValueError(f"数据包 knowledge[{index}] 必须是对象。")
        package_payloads.append((f"package[{index}]", item))
    records, duplicates, mismatches, conflicts = load_knowledge_from_mapping(
        package_payloads
    )

    shelf_entries: dict[str, tuple[ShelfEntry, ...]] = {}
    mapped_row_count = 0
    for raw_id, raw_locations in shelves.items():
        item_id = str(raw_id).strip()
        entries: list[ShelfEntry] = []
        if version == 1:
            location = str(raw_locations).strip()
            if location:
                entries.append(
                    ShelfEntry(
                        location=location,
                        name="未命名",
                        shelf_attribute="",
                        baffle_height="",
                        out_item_id="",
                    )
                )
        elif isinstance(raw_locations, list):
            for item in raw_locations:
                if isinstance(item, dict):
                    location = str(item.get("location") or "").strip()
                    name = str(item.get("name") or "").strip() or "未命名"
                    shelf_attribute = str(item.get("shelf_attribute") or "").strip()
                    baffle_height = str(item.get("baffle_height") or "").strip()
                    out_item_id = str(item.get("out_item_id") or "").strip()
                    if location:
                        entries.append(
                            ShelfEntry(
                                location=location,
                                name=name,
                                shelf_attribute=shelf_attribute,
                                baffle_height=baffle_height,
                                out_item_id=out_item_id,
                            )
                        )
                else:
                    location = str(item).strip()
                    if location:
                        entries.append(
                            ShelfEntry(
                                location=location,
                                name="未命名",
                                shelf_attribute="",
                                baffle_height="",
                                out_item_id="",
                            )
                        )
        else:
            raise ValueError(f"数据包 shelves[{item_id}] 必须是列表。")
        if not entries:
            raise ValueError(f"数据包 shelves[{item_id}] 为空。")

        unique_entries: list[ShelfEntry] = []
        seen_locations: set[str] = set()
        for entry in entries:
            if entry.location in seen_locations:
                continue
            seen_locations.add(entry.location)
            unique_entries.append(entry)
        shelf_entries[item_id] = tuple(unique_entries)
        mapped_row_count += len(unique_entries)

    report_payload = payload.get("report")
    if isinstance(report_payload, dict):
        empty_sku_count = int(report_payload.get("shelf_empty_sku_count", 0))
        shelf_rows = int(report_payload.get("shelf_row_count", mapped_row_count))
    else:
        empty_sku_count = int(payload.get("skipped_empty_sku_count", 0))
        shelf_rows = mapped_row_count
    report = build_load_report(
        records,
        len(knowledge),
        list(duplicates),
        list(mismatches),
        list(conflicts),
        shelf_entries,
        empty_sku_count,
        shelf_rows,
        mapped_row_count,
        [],
    )

    return Dataset(
        knowledge_records=tuple(records),
        shelf_entries=shelf_entries,
        report=report,
    )
