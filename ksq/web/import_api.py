"""Import files into configured target paths (write-only, with backup)."""

from __future__ import annotations

import cgi
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ksq import safe_io
from ksq.constants import (
    DEFAULT_KNOWLEDGE,
    DEFAULT_PICK_STRATEGY,
    DEFAULT_SHELVES,
    DEFAULT_TOOL_MAPPING,
    DEFAULT_UNAVAILABLE,
    ORDER_CONFIG_FILE,
    ORDER_CONFIG_PROD_FILE,
    PICK_STRATEGY_FILE_NAME,
    RUNTIME_UPLOAD_DIRECTORY,
    SHELVES_FILE_NAME,
    TOOL_MAPPING_FILE_NAME,
)
from ksq.naming import classify_import_kind
from ksq.web import state
from ksq.web.loader import (
    apply_configured_paths_reload,
    configured_paths_ready,
    get_uploaded_files,
    prepare_runtime_upload_directory,
)

DATASET_IMPORT_KINDS = frozenset(
    {
        "knowledge",
        "shelves",
        "unavailable",
        "tool_mapping",
        "pick_strategy",
    }
)


_IMPORT_BACKUP_KEEP_DAYS = 2

KIND_LABELS = {
    "knowledge": "knowledge",
    "shelves": "库位表",
    "unavailable": "不可处理列表",
    "tool_mapping": "工具映射",
    "pick_strategy": "闭环吸取列表",
    "order_config": "测试下单配置",
    "order_config_prod": "生产下单配置",
}


def _target_paths() -> Dict[str, Path]:
    knowledge = state.configured_knowledge or DEFAULT_KNOWLEDGE
    shelves = state.configured_shelves or DEFAULT_SHELVES
    unavailable = state.configured_unavailable or DEFAULT_UNAVAILABLE
    tool_mapping = state.configured_tool_mapping or DEFAULT_TOOL_MAPPING
    pick_strategy = state.configured_pick_strategy or DEFAULT_PICK_STRATEGY
    return {
        "knowledge": Path(knowledge),
        "shelves": Path(shelves),
        "unavailable": Path(unavailable),
        "tool_mapping": Path(tool_mapping),
        "pick_strategy": Path(pick_strategy),
        "order_config": ORDER_CONFIG_FILE,
        "order_config_prod": ORDER_CONFIG_PROD_FILE,
    }


def _write_bytes(destination: Path, payload: bytes) -> Optional[str]:
    """Backup then write, sharing the retention scheme with edit write-back."""
    backup_path = safe_io.safe_write_bytes(
        destination,
        payload,
        keep_days=_IMPORT_BACKUP_KEEP_DAYS,
    )
    return None if backup_path is None else str(backup_path)


def _collect_entries_from_zip(zip_path: Path) -> List[Tuple[str, str, bytes]]:
    entries: List[Tuple[str, str, bytes]] = []
    with zipfile.ZipFile(zip_path) as archive:
        for member_name in archive.namelist():
            if member_name.endswith("/"):
                continue
            file_name = Path(member_name).name
            if not file_name or file_name.startswith("."):
                continue
            kind = classify_import_kind(file_name, member_name)
            if kind == "unknown":
                continue
            with archive.open(member_name) as raw_file:
                entries.append((kind, file_name, raw_file.read()))
    return entries


def _collect_entries_from_upload(
    uploaded: cgi.FieldStorage,
) -> List[Tuple[str, str, bytes]]:
    file_name = Path(uploaded.filename or "").name
    if not file_name:
        raise ValueError("上传文件名为空。")
    if uploaded.file is None:
        raise ValueError(f"上传文件无效：{file_name}")
    payload = uploaded.file.read()
    suffix = Path(file_name).suffix.lower()
    if suffix == ".zip":
        upload_directory = RUNTIME_UPLOAD_DIRECTORY / "import_tmp"
        upload_directory.mkdir(parents=True, exist_ok=True)
        zip_path = upload_directory / file_name
        zip_path.write_bytes(payload)
        return _collect_entries_from_zip(zip_path)
    kind = classify_import_kind(file_name, file_name)
    if kind == "unknown":
        raise ValueError(
            f"无法识别文件类型：{file_name}。"
            "支持 sku-shelves*.csv、knowledge JSON、"
            "工具/闭环/不可处理配置、order_config*.json，或包含它们的 zip。"
        )
    return [(kind, file_name, payload)]


def import_uploaded_files(form: cgi.FieldStorage) -> Dict[str, object]:
    uploads = get_uploaded_files(form, "files")
    if not uploads:
        uploads = get_uploaded_files(form, "bundle_zip")
    if not uploads:
        raise ValueError("请选择要导入的压缩包或文件。")

    prepare_runtime_upload_directory()
    entries: List[Tuple[str, str, bytes]] = []
    source_names: List[str] = []
    for uploaded in uploads:
        name = Path(uploaded.filename or "").name
        if not name:
            continue
        source_names.append(name)
        entries.extend(_collect_entries_from_upload(uploaded))

    if not entries:
        raise ValueError("未从上传内容中识别到可导入的配置或数据文件。")

    targets = _target_paths()
    written: List[Dict[str, str]] = []
    knowledge_count = 0
    shelves_written = False
    backup_count = 0

    for kind, file_name, payload in entries:
        if kind == "knowledge":
            knowledge_dir = targets["knowledge"]
            if knowledge_dir.is_file():
                raise ValueError(
                    f"Knowledge 目标不是目录：{knowledge_dir}。请先在本机路径中设置目录。"
                )
            knowledge_dir.mkdir(parents=True, exist_ok=True)
            destination = knowledge_dir / file_name
            backup_path = _write_bytes(destination, payload)
            knowledge_count += 1
            item = {
                "kind": kind,
                "label": KIND_LABELS[kind],
                "source": file_name,
                "target": str(destination),
            }
            if backup_path:
                item["backup"] = backup_path
                backup_count += 1
            written.append(item)
            continue

        if kind == "shelves":
            destination = targets["shelves"]
            if destination.is_dir():
                destination = destination / SHELVES_FILE_NAME
            backup_path = _write_bytes(destination, payload)
            state.configured_shelves = destination
            shelves_written = True
            item = {
                "kind": kind,
                "label": KIND_LABELS[kind],
                "source": file_name,
                "target": str(destination),
            }
            if backup_path:
                item["backup"] = backup_path
                backup_count += 1
            written.append(item)
            continue

        destination = targets[kind]
        backup_path: Optional[str] = None
        if kind == "unavailable":
            if destination.is_dir():
                destination = destination / "unavailabel_obj.json"
            backup_path = _write_bytes(destination, payload)
            state.configured_unavailable = destination
        elif kind == "tool_mapping":
            if destination.is_dir():
                destination = destination / TOOL_MAPPING_FILE_NAME
            backup_path = _write_bytes(destination, payload)
            state.configured_tool_mapping = destination
        elif kind == "pick_strategy":
            if destination.is_dir():
                destination = destination / PICK_STRATEGY_FILE_NAME
            backup_path = _write_bytes(destination, payload)
            state.configured_pick_strategy = destination
        elif kind in {"order_config", "order_config_prod"}:
            backup_path = _write_bytes(destination, payload)
        else:
            continue
        item = {
            "kind": kind,
            "label": KIND_LABELS[kind],
            "source": file_name,
            "target": str(destination),
        }
        if backup_path:
            item["backup"] = backup_path
            backup_count += 1
        written.append(item)

    if knowledge_count and not (
        state.configured_knowledge and Path(state.configured_knowledge).is_dir()
    ):
        state.configured_knowledge = targets["knowledge"]

    message = f"已导入 {len(written)} 项到配置路径。"
    if backup_count:
        message += f" 其中 {backup_count} 个同名文件已备份。"

    result: Dict[str, object] = {
        "ok": True,
        "source_files": source_names,
        "written": written,
        "knowledge_files": knowledge_count,
        "shelves_updated": shelves_written,
        "backup_count": backup_count,
        "reloaded": False,
        "message": message,
    }

    # Mark imported paths as explicit so reload_config_pnp_paths() won't
    # override them with config.py values on the next reload.
    explicit_kinds = {
        item["kind"] for item in written if item["kind"] in DATASET_IMPORT_KINDS
    }
    if explicit_kinds:
        state._explicit_config_keys = state._explicit_config_keys | explicit_kinds

    touched_dataset = any(item["kind"] in DATASET_IMPORT_KINDS for item in written)
    if touched_dataset and configured_paths_ready():
        reload_info = apply_configured_paths_reload()
        result["reloaded"] = True
        result["reload"] = reload_info
        result["load_method"] = "paths"
        result["capabilities"] = reload_info["capabilities"]
        result["message"] = (
            message + f" 已自动重新加载数据（{reload_info['count']} 条）。"
        )
    elif touched_dataset:
        result["message"] = (
            message + " 尚未同时具备 Knowledge 目录与库位表，请到「本机路径」加载。"
        )
    else:
        result["message"] = message + " 未改动药品数据文件，无需重新加载。"

    return result
