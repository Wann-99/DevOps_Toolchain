"""Domain data models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


@dataclass(frozen=True)
class BundlePaths:
    knowledge_directory: Path
    shelves_file: Path
    unavailable_file: Path | None
    tool_mapping_file: Path | None
    pick_strategy_file: Path | None


@dataclass(frozen=True)
class ShelfEntry:
    location: str
    name: str
    shelf_attribute: str
    baffle_height: str
    out_item_id: str
    sku_code: str = ""


class ShelfParseResult(NamedTuple):
    entries: dict[str, tuple[ShelfEntry, ...]]
    skipped_empty_sku_count: int
    row_count: int
    mapped_row_count: int
    merge_conflicts: tuple[str, ...]
    missing_location_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadReport:
    knowledge_file_count: int
    knowledge_record_count: int
    duplicate_knowledge_files: tuple[str, ...]
    filename_id_mismatches: tuple[str, ...]
    shelf_row_count: int
    shelf_empty_sku_count: int
    shelf_mapped_row_count: int
    shelf_unique_sku_count: int
    multi_location_sku_count: int
    conflicting_knowledge_ids: tuple[str, ...]
    shelves_without_knowledge: tuple[str, ...]
    ignored_knowledge_files: tuple[str, ...]
    shelf_merge_conflicts: tuple[str, ...] = ()
    shelf_location_warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        parts = [
            (
                f"在架 SKU {self.shelf_unique_sku_count}"
                f"（库位行 {self.shelf_row_count}，空 sku {self.shelf_empty_sku_count}，"
                f"已映射 {self.shelf_mapped_row_count}）"
            ),
            f"其中无 knowledge {len(self.shelves_without_knowledge)}",
            f"多库位 sku {self.multi_location_sku_count}",
            f"knowledge 字典 {self.knowledge_file_count} 文件 / {self.knowledge_record_count} 条",
        ]
        if self.duplicate_knowledge_files:
            parts.append(f"重复 knowledge 文件 {len(self.duplicate_knowledge_files)}")
        if self.filename_id_mismatches:
            parts.append(f"文件名与 id 不一致 {len(self.filename_id_mismatches)}")
        if self.conflicting_knowledge_ids:
            parts.append(f"同 id 内容冲突 {len(self.conflicting_knowledge_ids)}")
        if self.ignored_knowledge_files:
            parts.append(f"已忽略非 knowledge 文件 {len(self.ignored_knowledge_files)}")
        if self.shelf_merge_conflicts:
            parts.append(f"库位行字段冲突 {len(self.shelf_merge_conflicts)}")
        if self.shelf_location_warnings:
            parts.append(
                f"缺少库位字段 {len(self.shelf_location_warnings)} 行（已按空库位加载）"
            )
        return "；".join(parts) + "。"


@dataclass(frozen=True)
class Dataset:
    knowledge_records: tuple[dict[str, object], ...]
    shelf_entries: dict[str, tuple[ShelfEntry, ...]]
    report: LoadReport
