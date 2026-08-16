"""HTML page rendering and API payload helpers."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from ksq.constants import APP_VERSION, BASE_COLUMNS, PACKAGE_DIRECTORY
from ksq.display import display_value
from ksq.models import Dataset, ShelfEntry
from ksq.side_data import (
    resolve_closed_loop_label,
    resolve_tool_name,
    resolve_unavailable_label,
)
from ksq.web import state

TEMPLATES_DIRECTORY = PACKAGE_DIRECTORY / "web" / "templates"
STATIC_DIRECTORY = PACKAGE_DIRECTORY / "web" / "static"


def optional_path_display(path: Optional[Path]) -> str:
    return "" if path is None else str(path)


def _read_template(name: str) -> str:
    return (TEMPLATES_DIRECTORY / name).read_text(encoding="utf-8")


def shell_page_html() -> str:
    return (
        _read_template("shell.html")
        .replace("__APP_VERSION__", html.escape(APP_VERSION))
        .replace("__KNOWLEDGE__", html.escape(str(state.configured_knowledge)))
        .replace("__SHELVES__", html.escape(str(state.configured_shelves)))
        .replace(
            "__UNAVAILABLE__",
            html.escape(optional_path_display(state.configured_unavailable)),
        )
        .replace(
            "__TOOL_MAPPING__",
            html.escape(optional_path_display(state.configured_tool_mapping)),
        )
        .replace(
            "__PICK_STRATEGY__",
            html.escape(optional_path_display(state.configured_pick_strategy)),
        )
    )


def login_page_html() -> str:
    return _read_template("login.html").replace(
        "__APP_VERSION__", html.escape(APP_VERSION)
    )


def home_page_html() -> str:
    return shell_page_html()


def query_page_html() -> str:
    return shell_page_html()


def order_page_html() -> str:
    return shell_page_html()


def build_missing_rows(dataset: Dataset) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    for item_id in dataset.report.shelves_without_knowledge:
        entries = dataset.shelf_entries.get(item_id, ())
        locations = display_value([entry.location for entry in entries])
        names = display_value(
            [entry.name for entry in entries if entry.name and entry.name != "未命名"]
        )
        rows.append((item_id, names, locations))
    return rows


def format_status_html(
    dataset: Dataset,
    elapsed_seconds: float,
    source: str,
    knowledge_source: str,
    shelves_source: str,
    has_unavailable: bool,
    has_tool_mapping: bool,
    has_pick_strategy: bool,
) -> str:
    report = dataset.report
    shelf_ids = set(dataset.shelf_entries)
    # An empty dictionary is loadable but almost always a wrong path or a stale
    # container mount, so it gets its own banner rather than a note in the list.
    empty_dictionary_html = (
        ""
        if report.knowledge_record_count
        else (
            "<p class='status-alert'>knowledge 字典为空："
            + html.escape(knowledge_source)
            + " 中没有 JSON 文件。在架 SKU 仍可查询与下单，但所有 knowledge 字段均为 -。"
            + "请确认路径是否正确（容器部署时还需确认挂载目录未失效）。</p>"
        )
    )
    notes: List[str] = []
    if report.shelves_without_knowledge and report.knowledge_record_count:
        notes.append(
            "在架 SKU 无 knowledge："
            + str(len(report.shelves_without_knowledge))
            + "（已纳入查询，knowledge 字段为 -）"
        )
    on_shelf_conflicts = [
        item_id for item_id in report.conflicting_knowledge_ids if item_id in shelf_ids
    ]
    if on_shelf_conflicts:
        notes.append(
            "在架 SKU 同 id 内容冲突："
            + ", ".join(html.escape(item) for item in on_shelf_conflicts[:5])
            + f"（共 {len(on_shelf_conflicts)} 个，已取首条）"
        )
    if report.shelf_merge_conflicts:
        preview = "".join(
            f"<li>{html.escape(item)}</li>"
            for item in report.shelf_merge_conflicts[:5]
        )
        more = (
            ""
            if len(report.shelf_merge_conflicts) <= 5
            else f"<li>… 共 {len(report.shelf_merge_conflicts)} 处</li>"
        )
        notes.append(
            "库位表同库位行字段冲突（已按先出现的行取值，建议核对库位表）："
            + f"<ul class='status-list'>{preview}{more}</ul>"
        )
    dictionary_notes: List[str] = []
    if report.duplicate_knowledge_files:
        dictionary_notes.append(
            "重复 knowledge 文件："
            + ", ".join(html.escape(item) for item in report.duplicate_knowledge_files[:5])
        )
    if report.filename_id_mismatches:
        dictionary_notes.append(
            "文件名与 id 不一致："
            + ", ".join(html.escape(item) for item in report.filename_id_mismatches[:5])
        )
    if report.conflicting_knowledge_ids:
        dictionary_notes.append(
            "同 id 内容冲突："
            + ", ".join(html.escape(item) for item in report.conflicting_knowledge_ids[:5])
            + f"（共 {len(report.conflicting_knowledge_ids)} 个）"
        )
    if report.ignored_knowledge_files:
        preview = ", ".join(
            html.escape(item) for item in report.ignored_knowledge_files[:5]
        )
        more = (
            ""
            if len(report.ignored_knowledge_files) <= 5
            else f" 等 {len(report.ignored_knowledge_files)} 个"
        )
        dictionary_notes.append(
            "已忽略非 knowledge 文件（如编辑器 .swp / 备份 .bak，不影响加载）："
            + preview
            + more
        )
    notes_html = (
        ""
        if not notes
        else "<ul class='status-list'>"
        + "".join(f"<li>{item}</li>" for item in notes)
        + "</ul>"
    )
    dictionary_notes_html = (
        ""
        if not dictionary_notes
        else "<details class='status-details'><summary>knowledge 字典问题（不影响在架 SKU 查询）</summary>"
        + "<ul class='status-list'>"
        + "".join(f"<li>{item}</li>" for item in dictionary_notes)
        + "</ul></details>"
    )
    knowledge_ids = {
        str(record["id"])
        for record in dataset.knowledge_records
        if isinstance(record.get("id"), str)
    }
    matched_count = len(shelf_ids & knowledge_ids)
    unmatched_count = len(report.shelves_without_knowledge)
    return f"""<div class="status-card"><h3>加载成功</h3>
<dl class="status-grid"><dt>在架 SKU</dt><dd>{report.shelf_unique_sku_count}</dd><dt>库位行</dt><dd>{report.shelf_mapped_row_count}</dd>
<dt>已匹配 knowledge</dt><dd>{matched_count}</dd><dt>无 knowledge</dt><dd>{unmatched_count}</dd>
<dt>多库位 SKU</dt><dd>{report.multi_location_sku_count}</dd><dt>knowledge 字典</dt><dd>{report.knowledge_record_count}</dd>
<dt>不可处理</dt><dd>{"有" if has_unavailable else "无"}</dd><dt>工具映射</dt><dd>{"有" if has_tool_mapping else "无"}</dd>
<dt>闭环列表</dt><dd>{"有" if has_pick_strategy else "无"}</dd><dt>耗时</dt><dd>{elapsed_seconds:.2f}s</dd></dl>{empty_dictionary_html}{notes_html}{dictionary_notes_html}</div>"""


def _join_unique_display(values: List[object]) -> str:
    parts: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = display_value(value)
        if text == "-" or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return "、".join(parts) if parts else "-"


def _shelf_display_fields(entries: tuple[ShelfEntry, ...]) -> Dict[str, str]:
    names = _join_unique_display(
        [entry.name for entry in entries if entry.name and entry.name != "未命名"]
    )
    locations = display_value([entry.location for entry in entries])
    shelf_attributes = _join_unique_display(
        [entry.shelf_attribute for entry in entries]
    )
    baffle_heights = _join_unique_display([entry.baffle_height for entry in entries])
    out_item_ids = _join_unique_display([entry.out_item_id for entry in entries])
    return {
        "name": names,
        "locations": locations,
        "shelf_attribute": shelf_attributes,
        "baffle_height": baffle_heights,
        "out_item_id": out_item_ids,
    }


def _order_lines(barcode: str, entries: tuple[ShelfEntry, ...]) -> List[Dict[str, str]]:
    lines: List[Dict[str, str]] = []
    for entry in entries:
        if not entry.out_item_id or not entry.location:
            continue
        lines.append(
            {
                "item_id": entry.out_item_id,
                "barcode": barcode,
                "location_code": entry.location,
                "name": entry.name if entry.name != "未命名" else "",
                "shelf_attribute": entry.shelf_attribute or "",
                "baffle_height": entry.baffle_height or "",
            }
        )
    return lines


def _build_query_record(
    item_id: str,
    entries: tuple[ShelfEntry, ...],
    knowledge_values: Dict[str, str],
    tool_mapping: Optional[Dict[str, str]],
    closed_loop_ids: Optional[FrozenSet[str]],
    unavailable_ids: Optional[FrozenSet[str]],
) -> Dict[str, object]:
    shelf_fields = _shelf_display_fields(entries)
    return {
        "id": item_id,
        "out_item_id": shelf_fields["out_item_id"],
        "name": shelf_fields["name"],
        "locations": shelf_fields["locations"],
        "shelf_attribute": shelf_fields["shelf_attribute"],
        "baffle_height": shelf_fields["baffle_height"],
        "tool": display_value(resolve_tool_name(item_id, tool_mapping)),
        "closed_loop": display_value(
            resolve_closed_loop_label(item_id, closed_loop_ids)
        ),
        "unavailable": display_value(
            resolve_unavailable_label(item_id, unavailable_ids)
        ),
        "order_lines": _order_lines(item_id, entries),
        "knowledge": knowledge_values,
    }


def records_payload(
    dataset: Dataset,
    tool_mapping: Optional[Dict[str, str]],
    closed_loop_ids: Optional[FrozenSet[str]],
    unavailable_ids: Optional[FrozenSet[str]],
) -> Dict[str, object]:
    # Shelf data is the subject: only on-shelf SKUs are listed. knowledge is a
    # lookup dictionary used to enrich them and to flag anomalies.
    knowledge_by_id: Dict[str, Dict[str, object]] = {}
    for knowledge in dataset.knowledge_records:
        item_id = knowledge.get("id")
        if not isinstance(item_id, str):
            raise ValueError("knowledge 记录中的 id 必须是字符串。")
        knowledge_by_id.setdefault(item_id, knowledge)

    fields: List[str] = []
    seen_fields: set[str] = set()
    records: List[Dict[str, object]] = []
    for item_id in sorted(dataset.shelf_entries):
        knowledge = knowledge_by_id.get(item_id)
        knowledge_values = (
            {}
            if knowledge is None
            else {
                key: display_value(value)
                for key, value in knowledge.items()
                if key != "id"
            }
        )
        for field_name in knowledge_values:
            if field_name not in seen_fields:
                seen_fields.add(field_name)
                fields.append(field_name)
        records.append(
            _build_query_record(
                item_id,
                dataset.shelf_entries[item_id],
                knowledge_values,
                tool_mapping,
                closed_loop_ids,
                unavailable_ids,
            )
        )

    return {
        "fields": fields,
        "records": records,
        "base_columns": list(BASE_COLUMNS),
        "knowledge_dictionary_count": len(knowledge_by_id),
    }


def resolve_static_file(relative_path: str) -> Optional[Path]:
    if not relative_path or ".." in relative_path or relative_path.startswith("/"):
        return None
    candidate = (STATIC_DIRECTORY / relative_path).resolve()
    try:
        candidate.relative_to(STATIC_DIRECTORY.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate
