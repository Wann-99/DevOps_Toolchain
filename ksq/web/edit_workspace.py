"""Shelf-only editing and read-only exports from the loaded data copy."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ksq import safe_io
from ksq.models import Dataset, ShelfEntry
from ksq.shelves import format_shelf_location, parse_shelf_locations, shelf_row_id
from ksq.web import state

SHELF_FIELD_MAP = {
    "货架属性": "shelf_attribute",
    "挡板高度": "baffle_height",
}
LOCATION_SCOPED_FIELDS = frozenset({"库位", "货架属性", "挡板高度"})
SHELF_LOCATION_COLUMNS = ("shelf_number", "level", "bin_unit")

SIDE_FILE_KEYS = {
    "unavailabel_obj.json": "unavailable",
    "obj_tool_mapping.json": "tool_mapping",
    "pick_strategy_obj.json": "pick_strategy",
}


def _validate_item_id(item_id: str) -> str:
    """Keep an item id a single filename component before it reaches disk."""
    value = str(item_id or "").strip()
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("id 包含无效路径字符。")
    return value


def _read_csv_rows(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError("库位表缺少表头。")
        fieldnames = [name.lstrip("\ufeff") for name in reader.fieldnames]
        reader.fieldnames = fieldnames
        rows = [{key: (row.get(key) or "") for key in fieldnames} for row in reader]
    return fieldnames, rows


def _read_json_file(path: Optional[Path]) -> Tuple[Optional[object], int]:
    if path is None or not path.is_file():
        return None, 4
    text = path.read_text(encoding="utf-8")
    return json.loads(text), _detect_json_indent(text)


def _shelf_entries_from_rows(
    fieldnames: List[str], rows: List[Dict[str, str]]
) -> dict[str, tuple[ShelfEntry, ...]]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    buffer.seek(0)
    return parse_shelf_locations(buffer).entries


def _build_dataset_from_workspace() -> Dataset:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未初始化编辑工作区。")
    base = state.loaded_dataset
    if base is None:
        raise ValueError("尚未加载数据。")
    shelf_entries = _shelf_entries_from_rows(
        workspace["shelf_fieldnames"], workspace["shelf_rows"]
    )
    return Dataset(
        knowledge_records=base.knowledge_records,
        shelf_entries=shelf_entries,
        report=base.report,
    )


def init_workspace_from_loaded() -> None:
    dataset = state.loaded_dataset
    if dataset is None:
        state.edit_workspace = None
        return
    knowledge_by_id: Dict[str, Dict[str, object]] = {}
    for record in dataset.knowledge_records:
        item_id = str(record.get("id") or "").strip()
        if not item_id:
            continue
        # Match query rendering: when duplicate records share an id, the
        # first deterministic record is the one users see and export.
        knowledge_by_id.setdefault(item_id, deepcopy(dict(record)))

    shelves_path = state.loaded_path("shelves")
    if shelves_path is None:
        raise ValueError("未加载库位表，无法初始化编辑工作区。")
    fieldnames, rows = _read_csv_rows(shelves_path)
    side_files: Dict[str, object] = {}
    side_json_indents: Dict[str, int] = {}
    unavailable, unavailable_indent = _read_json_file(state.loaded_path("unavailable"))
    if unavailable is not None:
        side_files["unavailabel_obj.json"] = unavailable
        side_json_indents["unavailabel_obj.json"] = unavailable_indent
    tool_mapping_raw, tool_indent = _read_json_file(state.loaded_path("tool_mapping"))
    if tool_mapping_raw is not None:
        side_files["obj_tool_mapping.json"] = tool_mapping_raw
        side_json_indents["obj_tool_mapping.json"] = tool_indent
    pick_strategy, pick_indent = _read_json_file(state.loaded_path("pick_strategy"))
    if pick_strategy is not None:
        side_files["pick_strategy_obj.json"] = pick_strategy
        side_json_indents["pick_strategy_obj.json"] = pick_indent

    knowledge_indent = 4
    knowledge_dir = state.loaded_path("knowledge")
    if knowledge_dir is not None and knowledge_dir.is_dir():
        for path in sorted(knowledge_dir.glob("*.json")):
            try:
                knowledge_indent = _detect_json_indent(path.read_text(encoding="utf-8"))
                break
            except OSError:
                continue

    state.edit_workspace = {
        "knowledge_by_id": knowledge_by_id,
        "shelf_fieldnames": fieldnames,
        "shelf_rows": rows,
        "side_files": side_files,
        "side_json_indents": side_json_indents,
        "knowledge_json_indent": knowledge_indent,
        "dirty_shelf_ops": [],
        "shelves_dirty": False,
    }


def _write_csv_rows(
    path: Path, fieldnames: List[str], rows: List[Dict[str, str]]
) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    try:
        safe_io.safe_write_text(
            path, buffer.getvalue(), encoding="utf-8-sig", backup=False
        )
    except OSError as error:
        raise OSError(f"写入库位表失败：{path}，原因：{error}") from error


def _mark_shelf_op(
    workspace: Dict[str, object],
    item_id: str,
    location: str,
    columns: Dict[str, str],
) -> None:
    ops: List[Dict[str, object]] = workspace["dirty_shelf_ops"]  # type: ignore[assignment]
    ops.append(
        {
            "sku_code": str(item_id).strip(),
            "location": str(location or "").strip(),
            "columns": dict(columns),
        }
    )
    workspace["shelves_dirty"] = True


def _normalize_location_token(value: str) -> Tuple[str, str, str]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("库位不能为空。")
    text = text.replace(" ", "")
    if text and text[0].isalpha():
        if "-" in text[:3]:
            text = text.split("-", 1)[1]
        else:
            index = 0
            while index < len(text) and text[index].isalpha():
                index += 1
            text = text[index:]
    parts = [part.strip() for part in text.split("-") if part.strip()]
    if len(parts) == 3:
        shelf_number, level, bin_unit = parts
    else:
        digits = "".join(char for char in text if char.isdigit())
        if len(digits) != 6:
            raise ValueError(f"库位格式无效，需要 架-层-位：{value!r}")
        shelf_number, level, bin_unit = digits[:2], digits[2:4], digits[4:6]
    if not (shelf_number and level and bin_unit):
        raise ValueError(f"库位格式无效，需要 架-层-位：{value!r}")
    if shelf_number.isdigit():
        shelf_number = shelf_number.zfill(2)
    if level.isdigit():
        level = level.zfill(2)
    if bin_unit.isdigit():
        bin_unit = bin_unit.zfill(2)
    return shelf_number, level, bin_unit


def _parse_location(value: str) -> Tuple[str, str, str]:
    return _normalize_location_token(value)


def _detect_json_indent(text: str) -> int:
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        leading = len(line) - len(line.lstrip(" "))
        if leading > 0:
            return leading
    return 4


def _json_dump_bytes(payload: object, indent: int) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=indent) + "\n").encode(
        "utf-8"
    )


def _rows_for_sku(workspace: Dict[str, object], item_id: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = workspace["shelf_rows"]  # type: ignore[assignment]
    return [row for row in rows if shelf_row_id(row) == item_id]


def _row_location(row: Dict[str, str]) -> str:
    return format_shelf_location(
        (row.get("shelf_number") or "").strip(),
        (row.get("level") or "").strip(),
        (row.get("bin_unit") or "").strip(),
    )


def _normalize_location_key(value: str) -> str:
    text = str(value or "").strip().replace(" ", "")
    if not text:
        return ""
    try:
        shelf_number, level, bin_unit = _normalize_location_token(text)
    except ValueError:
        return text.replace("-", "")
    return f"{shelf_number}-{level}-{bin_unit}"


def _find_row_by_location(
    rows: List[Dict[str, str]], location: str
) -> Dict[str, str]:
    target = _normalize_location_key(location)
    if not target:
        raise ValueError("库位不能为空。")
    for row in rows:
        current = _normalize_location_key(_row_location(row))
        if current == target:
            return row
        if current.replace("-", "") == target.replace("-", ""):
            return row
    raise ValueError(f"未找到库位行：{location}")


def save_field(
    item_id: str,
    field: str,
    value: str,
    location: Optional[str] = None,
) -> Dict[str, object]:
    item_id = _validate_item_id(item_id)
    field = str(field or "").strip()
    location_text = str(location or "").strip()
    if not item_id:
        raise ValueError("id 不能为空。")
    if not field:
        raise ValueError("field 不能为空。")
    if field not in LOCATION_SCOPED_FIELDS:
        raise ValueError(f"不允许修改 {field}。")

    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据，无法保存修改。")

    text = str(value)

    if field in SHELF_FIELD_MAP:
        csv_key = SHELF_FIELD_MAP[field]
        rows = _rows_for_sku(workspace, item_id)
        if not rows:
            raise ValueError(f"商品 {item_id} 无库位行，无法修改 {field}。")
        if field in LOCATION_SCOPED_FIELDS and (location_text or len(rows) > 1):
            if not location_text:
                raise ValueError(f"多库位商品修改 {field} 时必须指定库位。")
            target_row = _find_row_by_location(rows, location_text)
            target_row[csv_key] = text.strip()
            _mark_shelf_op(
                workspace,
                item_id,
                location_text,
                {csv_key: text.strip()},
            )
        else:
            for row in rows:
                row[csv_key] = text.strip()
                _mark_shelf_op(
                    workspace,
                    item_id,
                    _row_location(row),
                    {csv_key: text.strip()},
                )
    elif field == "库位":
        rows = _rows_for_sku(workspace, item_id)
        if not rows:
            raise ValueError(f"商品 {item_id} 无库位行，无法修改库位。")
        if location_text or len(rows) > 1:
            if not location_text:
                raise ValueError("多库位商品修改库位时必须先选择要编辑的库位。")
            target_row = _find_row_by_location(rows, location_text)
            shelf_number, level, bin_unit = _parse_location(text)
            target_row["shelf_number"] = shelf_number
            target_row["level"] = level
            target_row["bin_unit"] = bin_unit
            _mark_shelf_op(
                workspace,
                item_id,
                location_text,
                {
                    "shelf_number": shelf_number,
                    "level": level,
                    "bin_unit": bin_unit,
                },
            )
        else:
            tokens = [
                part.strip()
                for part in text.replace("、", ",").split(",")
                if part.strip() and part.strip() != "-"
            ]
            if not tokens:
                raise ValueError("库位不能为空。")
            if len(tokens) != len(rows):
                raise ValueError(
                    f"库位数量须与现有行一致（当前 {len(rows)} 个，收到 {len(tokens)} 个）。"
                )
            for row, token in zip(rows, tokens):
                old_location = _row_location(row)
                shelf_number, level, bin_unit = _parse_location(token)
                row["shelf_number"] = shelf_number
                row["level"] = level
                row["bin_unit"] = bin_unit
                _mark_shelf_op(
                    workspace,
                    item_id,
                    old_location,
                    {
                        "shelf_number": shelf_number,
                        "level": level,
                        "bin_unit": bin_unit,
                    },
                )
    dataset = _build_dataset_from_workspace()
    state.loaded_dataset = dataset
    revision = state.bump_data_revision()
    return {
        "ok": True,
        "id": item_id,
        "field": field,
        "saved_in_memory": True,
        "wrote_original": False,
        "data_revision": revision,
    }


def _find_original_shelf_row(
    rows: List[Dict[str, str]], item_id: str, location: str
) -> Dict[str, str]:
    sku_rows = [row for row in rows if shelf_row_id(row) == item_id]
    if not sku_rows:
        raise ValueError(f"库位表中未找到 SKU：{item_id}")
    if location:
        return _find_row_by_location(sku_rows, location)
    if len(sku_rows) == 1:
        return sku_rows[0]
    raise ValueError(f"商品 {item_id} 有多个库位，写回时必须指定库位。")


def _persist_shelves(workspace: Dict[str, object]) -> Optional[Dict[str, object]]:
    ops: List[Dict[str, object]] = workspace["dirty_shelf_ops"]  # type: ignore[assignment]
    if not ops:
        return None
    path = state.loaded_path("shelves")
    if path is None:
        raise ValueError("未配置库位表路径，无法写回。")
    if not path.is_file():
        raise FileNotFoundError(f"库位表不存在：{path}")
    fieldnames, rows = _read_csv_rows(path)
    updated_skus: Set[str] = set()
    for op in ops:
        sku_code = str(op.get("sku_code") or "").strip()
        location = str(op.get("location") or "").strip()
        columns = op.get("columns")
        if not sku_code or not isinstance(columns, dict):
            continue
        target_row = _find_original_shelf_row(rows, sku_code, location)
        for column, value in columns.items():
            column_name = str(column)
            if (
                column_name not in fieldnames
                and column_name not in SHELF_LOCATION_COLUMNS
            ):
                continue
            target_row[column_name] = str(value)
        updated_skus.add(sku_code)
    _write_csv_rows(path, fieldnames, rows)
    return {
        "path": str(path),
        "backup": None,
        "updated_keys": len(updated_skus),
    }


def persist_dirty_files() -> Dict[str, object]:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据，无法保存工作副本。")
    result = _persist_shelves(workspace)
    if result is None:
        raise ValueError("没有可保存的修改。")
    workspace["dirty_shelf_ops"] = []
    workspace["shelves_dirty"] = False
    revision = state.bump_data_revision()
    return {
        "ok": True,
        "wrote_original": False,
        "files": [{"kind": "shelves", **result}],
        "restart_services": [],
        "data_revision": revision,
    }


def list_export_files() -> Dict[str, object]:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    knowledge_ids = workspace["knowledge_by_id"].keys()  # type: ignore[index]
    files = [{"name": "sku-shelves.csv", "kind": "shelves", "label": "sku-shelves.csv"}]
    side_files: Dict[str, object] = workspace["side_files"]  # type: ignore[assignment]
    for name in SIDE_FILE_KEYS:
        if name in side_files:
            files.append({"name": name, "kind": "side", "label": name})
    return {
        "files": files,
        "knowledge_count": len(knowledge_ids),
        "dirty_knowledge": 0,
        "shelves_dirty": bool(workspace["shelves_dirty"]),
        "side_dirty": False,
    }


def build_shelves_csv_bytes() -> bytes:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    fieldnames: List[str] = workspace["shelf_fieldnames"]  # type: ignore[assignment]
    rows: List[Dict[str, str]] = workspace["shelf_rows"]  # type: ignore[assignment]
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return buffer.getvalue().encode("utf-8-sig")


def build_knowledge_json_bytes(item_id: str) -> bytes:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    knowledge_by_id: Dict[str, Dict[str, object]] = workspace["knowledge_by_id"]  # type: ignore[assignment]
    record = knowledge_by_id.get(item_id)
    if record is None:
        raise FileNotFoundError(f"未找到 knowledge：{item_id}")
    indent = int(workspace.get("knowledge_json_indent") or 4)
    return _json_dump_bytes(record, indent)


def build_side_file_bytes(name: str) -> bytes:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    side_files: Dict[str, object] = workspace["side_files"]  # type: ignore[assignment]
    if name not in side_files:
        raise FileNotFoundError(f"未找到文件：{name}")
    indents: Dict[str, int] = workspace.get("side_json_indents") or {}  # type: ignore[assignment]
    indent = int(indents.get(name) or 4)
    return _json_dump_bytes(side_files[name], indent)


def build_export_file(name: str) -> Tuple[str, bytes, str]:
    name = str(name or "").strip()
    if name == "sku-shelves.csv":
        return name, build_shelves_csv_bytes(), "text/csv; charset=utf-8"
    if name in SIDE_FILE_KEYS:
        return name, build_side_file_bytes(name), "application/json; charset=utf-8"
    if name.endswith(".json"):
        item_id = Path(name).stem
        return name, build_knowledge_json_bytes(item_id), "application/json; charset=utf-8"
    raise ValueError(f"不支持的文件名：{name}")


def build_knowledge_zip_bytes() -> bytes:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    knowledge_by_id: Dict[str, Dict[str, object]] = workspace["knowledge_by_id"]  # type: ignore[assignment]
    if not knowledge_by_id:
        raise ValueError("当前没有可导出的 knowledge。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item_id in sorted(knowledge_by_id):
            archive.writestr(
                f"knowledge/{item_id}.json",
                build_knowledge_json_bytes(item_id),
            )
    return buffer.getvalue()


def build_missing_knowledge_template(item_id: str) -> Dict[str, object]:
    return {
        "id": item_id,
        "是否有商品码": "",
        "是否有溯源码": "",
        "条码位置": "",
        "溯源码位置": "",
        "长度": 0,
        "宽度": 0,
        "高度": 0,
        "重量": 0.0,
        "包装类型": "",
    }


def build_missing_rows_csv_bytes(rows: List[Tuple[str, str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["id", "name", "locations"])
    for sku, name, locations in rows:
        writer.writerow([sku, name, locations])
    return buffer.getvalue().encode("utf-8-sig")


def build_missing_knowledge_zip_bytes(rows: List[Tuple[str, str, str]]) -> bytes:
    if not rows:
        raise ValueError("当前没有缺少 knowledge 的药品。")
    indent = 4
    workspace = state.edit_workspace
    if workspace is not None:
        indent = int(workspace.get("knowledge_json_indent") or 4)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sku, _name, _locations in rows:
            item_id = str(sku or "").strip()
            if not item_id:
                continue
            archive.writestr(
                f"knowledge/{item_id}.json",
                _json_dump_bytes(build_missing_knowledge_template(item_id), indent),
            )
    return buffer.getvalue()


def build_export_zip_bytes() -> bytes:
    workspace = state.edit_workspace
    if workspace is None:
        raise ValueError("尚未加载数据。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sku-shelves.csv", build_shelves_csv_bytes())
        knowledge_by_id: Dict[str, Dict[str, object]] = workspace["knowledge_by_id"]  # type: ignore[assignment]
        for item_id in sorted(knowledge_by_id):
            archive.writestr(
                f"knowledge/{item_id}.json",
                build_knowledge_json_bytes(item_id),
            )
        side_files: Dict[str, object] = workspace["side_files"]  # type: ignore[assignment]
        for name in SIDE_FILE_KEYS:
            if name in side_files:
                archive.writestr(name, build_side_file_bytes(name))
    return buffer.getvalue()
