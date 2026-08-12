"""Compatibility re-exports for the domain layer (prefer importing from ksq.*)."""

from __future__ import annotations

from ksq.bundle import extract_bundle_from_zip
from ksq.constants import (
    DEFAULT_TOOL_NAME,
    OPTIONAL_CONFIG_FILE_NAMES,
    PACKAGE_SUFFIX,
    PACKAGE_VERSION,
    PICK_STRATEGY_FILE_NAME,
    PICK_STRATEGY_FILE_PREFIX,
    SHELVES_FILE_NAME,
    SHELVES_FILE_PREFIX,
    TOOL_MAPPING_FILE_NAME,
    TOOL_MAPPING_FILE_PREFIX,
    UNAVAILABLE_FILE_NAMES,
    UNAVAILABLE_FILE_PREFIXES,
)
from ksq.dataset import build_dataset, build_load_report, load_dataset_from_zip
from ksq.knowledge import (
    list_knowledge_files,
    load_knowledge_from_mapping,
    load_knowledge_records,
    normalize_item_id,
)
from ksq.models import BundlePaths, Dataset, LoadReport, ShelfEntry
from ksq.naming import (
    file_stem_lower,
    is_knowledge_member,
    is_optional_config_file_name,
    is_pick_strategy_file_name,
    is_shelves_file_name,
    is_tool_mapping_file_name,
    is_unavailable_file_name,
    matches_file_prefix,
)
from ksq.package_io import load_package, save_package
from ksq.query import (
    find_matching_ids,
    matches_filters,
    parse_filter,
    parse_filter_value,
    query_dataset,
    record_matches,
    values_match,
)
from ksq.shelves import format_shelf_location, load_shelf_locations, parse_shelf_locations
from ksq.side_data import (
    is_closed_loop,
    load_closed_loop_ids,
    load_tool_mapping,
    resolve_closed_loop_label,
    resolve_tool_name,
)

__all__ = [
    "BundlePaths",
    "DEFAULT_TOOL_NAME",
    "Dataset",
    "LoadReport",
    "OPTIONAL_CONFIG_FILE_NAMES",
    "PACKAGE_SUFFIX",
    "PACKAGE_VERSION",
    "PICK_STRATEGY_FILE_NAME",
    "PICK_STRATEGY_FILE_PREFIX",
    "SHELVES_FILE_NAME",
    "SHELVES_FILE_PREFIX",
    "ShelfEntry",
    "TOOL_MAPPING_FILE_NAME",
    "TOOL_MAPPING_FILE_PREFIX",
    "UNAVAILABLE_FILE_NAMES",
    "UNAVAILABLE_FILE_PREFIXES",
    "build_dataset",
    "build_load_report",
    "extract_bundle_from_zip",
    "file_stem_lower",
    "find_matching_ids",
    "format_shelf_location",
    "is_closed_loop",
    "is_knowledge_member",
    "is_optional_config_file_name",
    "is_pick_strategy_file_name",
    "is_shelves_file_name",
    "is_tool_mapping_file_name",
    "is_unavailable_file_name",
    "list_knowledge_files",
    "load_closed_loop_ids",
    "load_dataset_from_zip",
    "load_knowledge_from_mapping",
    "load_knowledge_records",
    "load_package",
    "load_shelf_locations",
    "load_tool_mapping",
    "matches_file_prefix",
    "matches_filters",
    "normalize_item_id",
    "parse_filter",
    "parse_filter_value",
    "parse_shelf_locations",
    "query_dataset",
    "record_matches",
    "resolve_closed_loop_label",
    "resolve_tool_name",
    "save_package",
    "values_match",
]
