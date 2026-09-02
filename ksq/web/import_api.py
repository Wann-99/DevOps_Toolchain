"""Import files into configured target paths (write-only, with backup)."""

from __future__ import annotations

import cgi
import io
import json
import shutil
import tempfile
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
from ksq.knowledge import load_knowledge_from_mapping
from ksq.naming import classify_import_kind
from ksq.order.config import validate_order_config_types
from ksq.side_data import (
    load_closed_loop_ids,
    load_tool_mapping,
    load_unavailable_ids,
)
from ksq.shelves import parse_shelf_locations
from ksq.web import state
from ksq.web.loader import (
    apply_configured_paths_reload,
    configured_paths_ready,
    get_uploaded_files,
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


def _collect_entries_from_zip_payload(payload: bytes) -> List[Tuple[str, str, bytes]]:
    """Read a zip upload without touching the live runtime upload directory."""
    entries: List[Tuple[str, str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
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
        return _collect_entries_from_zip_payload(payload)
    kind = classify_import_kind(file_name, file_name)
    if kind == "unknown":
        raise ValueError(
            f"无法识别文件类型：{file_name}。"
            "支持 sku-shelves*.csv、etm_sku_locations_cache*.csv、knowledge JSON、"
            "工具/闭环/不可处理配置、order_config*.json，或包含它们的 zip。"
        )
    return [(kind, file_name, payload)]


def _decode_json(payload: bytes, source: str) -> object:
    try:
        return json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError(f"文件编码错误：{source}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 文件格式错误：{source}") from error


def _validate_entry(
    kind: str, file_name: str, payload: bytes, staging_root: Path, index: int
) -> None:
    """Validate one payload before any configured file is changed."""
    if kind == "knowledge":
        value = _decode_json(payload, file_name)
        if not isinstance(value, dict):
            raise ValueError(f"JSON 根节点必须是对象：{file_name}")
        load_knowledge_from_mapping([(file_name, value)])
        return
    if kind == "shelves":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"库位表编码错误：{file_name}") from error
        parse_shelf_locations(io.StringIO(text))
        return
    if kind == "order_config" or kind == "order_config_prod":
        value = _decode_json(payload, file_name)
        if not isinstance(value, dict):
            raise ValueError(f"下单配置根节点必须是对象：{file_name}")
        validate_order_config_types(value)
        return

    # Side-data loaders contain the canonical shape checks.  Give them a
    # private staged file so malformed JSON is rejected before commit.
    staged = staging_root / f"{index}-{file_name}"
    staged.write_bytes(payload)
    try:
        if kind == "unavailable":
            load_unavailable_ids(staged)
        elif kind == "tool_mapping":
            load_tool_mapping(staged)
        elif kind == "pick_strategy":
            load_closed_loop_ids(staged)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _safe_knowledge_destination(knowledge_dir: Path, file_name: str) -> Path:
    root = knowledge_dir.expanduser().resolve()
    destination = (root / file_name).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise ValueError(f"knowledge 文件名无效：{file_name}") from error
    return destination


def _plan_destination(kind: str, file_name: str, targets: Dict[str, Path]) -> Path:
    if kind == "knowledge":
        knowledge_dir = targets["knowledge"]
        if knowledge_dir.exists() and not knowledge_dir.is_dir():
            raise ValueError(
                f"Knowledge 目标不是目录：{knowledge_dir}。请先在本机路径中设置目录。"
            )
        return _safe_knowledge_destination(knowledge_dir, file_name)
    destination = targets[kind]
    if destination.is_dir():
        if kind == "shelves":
            return destination / SHELVES_FILE_NAME
        if kind == "unavailable":
            return destination / "unavailabel_obj.json"
        if kind == "tool_mapping":
            return destination / TOOL_MAPPING_FILE_NAME
        if kind == "pick_strategy":
            return destination / PICK_STRATEGY_FILE_NAME
    if destination.exists() and not destination.is_file():
        raise ValueError(f"导入目标不是文件：{destination}")
    return destination


def _snapshot_targets(destinations: List[Path]) -> Dict[Path, Optional[bytes]]:
    snapshots: Dict[Path, Optional[bytes]] = {}
    for destination in destinations:
        if destination in snapshots:
            continue
        if destination.is_file():
            snapshots[destination] = destination.read_bytes()
        elif destination.exists():
            raise ValueError(f"导入目标不是普通文件：{destination}")
        else:
            snapshots[destination] = None
    return snapshots


def _restore_targets(snapshots: Dict[Path, Optional[bytes]]) -> None:
    """Best-effort rollback if a later target write or reload fails."""
    for destination, payload in snapshots.items():
        if payload is None:
            if destination.is_file():
                try:
                    destination.unlink()
                except OSError:
                    pass
            continue
        try:
            safe_io.write_bytes_durably(destination, payload)
            safe_io.verify_written(destination, payload)
        except OSError:
            # Preserve the original exception from the transaction; the
            # backup created by safe_io remains available for manual recovery.
            pass


def import_uploaded_files(form: cgi.FieldStorage) -> Dict[str, object]:
    uploads = get_uploaded_files(form, "files")
    if not uploads:
        uploads = get_uploaded_files(form, "bundle_zip")
    if not uploads:
        raise ValueError("请选择要导入的压缩包或文件。")

    # Keep the transaction workspace beside the app runtime, but separate from
    # the live upload directory.  A bad upload therefore cannot erase a bundle
    # that is currently being viewed.
    staging_parent = RUNTIME_UPLOAD_DIRECTORY.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".ksq-import-", dir=str(staging_parent))
    )
    state_snapshot = {
        "configured_knowledge": state.configured_knowledge,
        "configured_knowledge_root": state.configured_knowledge_root,
        "configured_shelves": state.configured_shelves,
        "configured_unavailable": state.configured_unavailable,
        "configured_tool_mapping": state.configured_tool_mapping,
        "configured_pick_strategy": state.configured_pick_strategy,
        "explicit": state._explicit_config_keys,
        "loaded_dataset": state.loaded_dataset,
        "loaded_tool_mapping": state.loaded_tool_mapping,
        "loaded_closed_loop_ids": state.loaded_closed_loop_ids,
        "loaded_unavailable_ids": state.loaded_unavailable_ids,
        "data_source_ready": state.data_source_ready,
        "data_load_method": state.data_load_method,
        "edit_workspace": state.edit_workspace,
        "data_revision": state.data_revision,
    }
    snapshots: Dict[Path, Optional[bytes]] = {}
    try:
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

        # Validate every entry and resolve every destination before the first
        # configured file is touched.
        targets = _target_paths()
        plans: List[Tuple[str, str, bytes, Path]] = []
        for index, (kind, file_name, payload) in enumerate(entries):
            _validate_entry(kind, file_name, payload, staging_root, index)
            destination = _plan_destination(kind, file_name, targets)
            plans.append((kind, file_name, payload, destination))
        snapshots = _snapshot_targets([plan[3] for plan in plans])

        written: List[Dict[str, str]] = []
        knowledge_count = sum(1 for plan in plans if plan[0] == "knowledge")
        shelves_written = any(plan[0] == "shelves" for plan in plans)
        backup_count = 0
        for kind, file_name, payload, destination in plans:
            backup_path = _write_bytes(destination, payload)
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

        # Update path state only after all writes have succeeded.
        for kind, _file_name, _payload, destination in plans:
            if kind == "shelves":
                state.configured_shelves = destination
            elif kind == "unavailable":
                state.configured_unavailable = destination
            elif kind == "tool_mapping":
                state.configured_tool_mapping = destination
            elif kind == "pick_strategy":
                state.configured_pick_strategy = destination
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
        # override them with config.py on the next reload.
        explicit_kinds = {
            kind for kind, _name, _payload, _destination in plans
            if kind in DATASET_IMPORT_KINDS
        }
        if explicit_kinds:
            state._explicit_config_keys = state._explicit_config_keys | explicit_kinds

        touched_dataset = bool(explicit_kinds)
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
    except Exception:
        if snapshots:
            _restore_targets(snapshots)
        state.configured_knowledge = state_snapshot["configured_knowledge"]  # type: ignore[assignment]
        state.configured_knowledge_root = state_snapshot["configured_knowledge_root"]  # type: ignore[assignment]
        state.configured_shelves = state_snapshot["configured_shelves"]  # type: ignore[assignment]
        state.configured_unavailable = state_snapshot["configured_unavailable"]  # type: ignore[assignment]
        state.configured_tool_mapping = state_snapshot["configured_tool_mapping"]  # type: ignore[assignment]
        state.configured_pick_strategy = state_snapshot["configured_pick_strategy"]  # type: ignore[assignment]
        state._explicit_config_keys = state_snapshot["explicit"]  # type: ignore[assignment]
        state.loaded_dataset = state_snapshot["loaded_dataset"]  # type: ignore[assignment]
        state.loaded_tool_mapping = state_snapshot["loaded_tool_mapping"]  # type: ignore[assignment]
        state.loaded_closed_loop_ids = state_snapshot["loaded_closed_loop_ids"]  # type: ignore[assignment]
        state.loaded_unavailable_ids = state_snapshot["loaded_unavailable_ids"]  # type: ignore[assignment]
        state.data_source_ready = bool(state_snapshot["data_source_ready"])
        state.data_load_method = str(state_snapshot["data_load_method"])
        state.edit_workspace = state_snapshot["edit_workspace"]  # type: ignore[assignment]
        state.data_revision = int(state_snapshot["data_revision"])
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
