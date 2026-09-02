"""Build Dataset from knowledge directory + shelves file or zip."""

from __future__ import annotations

import json
import zipfile
from io import TextIOWrapper
from pathlib import Path

from ksq.knowledge import load_knowledge_from_mapping, load_knowledge_records
from ksq.models import Dataset, LoadReport, ShelfEntry, ShelfParseResult
from ksq.naming import is_knowledge_member, is_shelves_file_name
from ksq.shelves import load_shelf_locations, parse_shelf_locations


def build_load_report(
    knowledge_records: list[dict[str, object]],
    knowledge_file_count: int,
    duplicate_knowledge_files: list[str],
    filename_id_mismatches: list[str],
    conflicting_knowledge_ids: list[str],
    shelf_entries: dict[str, tuple[ShelfEntry, ...]],
    skipped_empty_sku_count: int,
    shelf_row_count: int,
    mapped_row_count: int,
    ignored_knowledge_files: list[str],
    shelf_merge_conflicts: tuple[str, ...] = (),
    shelf_location_warnings: tuple[str, ...] = (),
) -> LoadReport:
    knowledge_ids = {
        str(record["id"])
        for record in knowledge_records
        if isinstance(record.get("id"), str)
    }
    shelves_without_knowledge = tuple(
        sorted(item_id for item_id in shelf_entries if item_id not in knowledge_ids)
    )
    return LoadReport(
        knowledge_file_count=knowledge_file_count,
        knowledge_record_count=len(knowledge_records),
        duplicate_knowledge_files=tuple(duplicate_knowledge_files),
        filename_id_mismatches=tuple(filename_id_mismatches),
        shelf_row_count=shelf_row_count,
        shelf_empty_sku_count=skipped_empty_sku_count,
        shelf_mapped_row_count=mapped_row_count,
        shelf_unique_sku_count=len(shelf_entries),
        multi_location_sku_count=sum(
            1 for entries in shelf_entries.values() if len(entries) > 1
        ),
        conflicting_knowledge_ids=tuple(sorted(set(conflicting_knowledge_ids))),
        shelves_without_knowledge=shelves_without_knowledge,
        ignored_knowledge_files=tuple(ignored_knowledge_files),
        shelf_merge_conflicts=tuple(shelf_merge_conflicts),
        shelf_location_warnings=tuple(shelf_location_warnings),
    )


def build_dataset(knowledge_directory: Path, shelves_file: Path) -> Dataset:
    (
        knowledge_records,
        knowledge_file_count,
        duplicate_knowledge_files,
        filename_id_mismatches,
        conflicting_knowledge_ids,
        ignored_knowledge_files,
    ) = load_knowledge_records(knowledge_directory)
    shelves = load_shelf_locations(shelves_file)
    report = build_load_report(
        knowledge_records,
        knowledge_file_count,
        duplicate_knowledge_files,
        filename_id_mismatches,
        conflicting_knowledge_ids,
        shelves.entries,
        shelves.skipped_empty_sku_count,
        shelves.row_count,
        shelves.mapped_row_count,
        ignored_knowledge_files,
        shelves.merge_conflicts,
        shelves.missing_location_warnings,
    )
    return Dataset(
        knowledge_records=tuple(knowledge_records),
        shelf_entries=shelves.entries,
        report=report,
    )


def load_dataset_from_zip(zip_path: Path) -> Dataset:
    if not zip_path.is_file():
        raise FileNotFoundError(f"压缩包不存在：{zip_path}")

    payloads: list[tuple[str, dict[str, object]]] = []
    shelves: ShelfParseResult | None = None

    with zipfile.ZipFile(zip_path) as archive:
        for member_name in archive.namelist():
            if member_name.endswith("/"):
                continue
            file_name = Path(member_name).name
            if is_shelves_file_name(file_name):
                with archive.open(member_name) as raw_file:
                    with TextIOWrapper(
                        raw_file, encoding="utf-8-sig", newline=""
                    ) as text_file:
                        shelves = parse_shelf_locations(text_file)
            elif is_knowledge_member(member_name, file_name):
                with archive.open(member_name) as raw_file:
                    with TextIOWrapper(raw_file, encoding="utf-8") as text_file:
                        knowledge = json.load(text_file)
                if not isinstance(knowledge, dict):
                    raise ValueError(f"JSON 根节点必须是对象：{member_name}")
                payloads.append((file_name, knowledge))

    if not payloads:
        raise ValueError("压缩包中未找到 knowledge JSON 文件。")
    if shelves is None:
        raise ValueError(
            "压缩包中未找到库位表（支持 sku-shelves*.csv 或 "
            "etm_sku_locations_cache*.csv）。"
        )

    (
        knowledge_records,
        duplicate_knowledge_files,
        filename_id_mismatches,
        conflicting_knowledge_ids,
    ) = load_knowledge_from_mapping(payloads)

    report = build_load_report(
        knowledge_records,
        len(payloads),
        duplicate_knowledge_files,
        filename_id_mismatches,
        conflicting_knowledge_ids,
        shelves.entries,
        shelves.skipped_empty_sku_count,
        shelves.row_count,
        shelves.mapped_row_count,
        [],
        shelves.merge_conflicts,
        shelves.missing_location_warnings,
    )
    return Dataset(
        knowledge_records=tuple(knowledge_records),
        shelf_entries=shelves.entries,
        report=report,
    )
