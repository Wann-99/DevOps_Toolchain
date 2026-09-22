"""Import dataset files into managed copies and preserve source directories."""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Dict, List, Optional, Tuple
import io
import json
import shutil
import zipfile

from ksq import safe_io
from ksq.constants import ORDER_CONFIG_FILE, ORDER_CONFIG_PROD_FILE, PICK_STRATEGY_FILE_NAME, SHELVES_FILE_NAME, TOOL_MAPPING_FILE_NAME
from ksq.data import progress as load_progress
from ksq.data import state as state
from ksq.data import storage as data_storage
from ksq.data.loader import bundle_path_map, configured_bundle, get_uploaded_files, install_staged_dataset
from ksq.knowledge import is_knowledge_record, list_knowledge_files, load_knowledge_from_mapping
from ksq.models import BundlePaths
from ksq.naming import classify_import_kind, is_auxiliary_knowledge_file
from ksq.order.config import validate_order_config_types
from ksq.shelves import load_shelf_locations, parse_shelf_locations
from ksq.side_data import load_closed_loop_ids, load_tool_mapping, load_unavailable_ids


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


def _write_bytes(destination: Path, payload: bytes) -> Optional[str]:
    """Backup then write, sharing the retention scheme with edit write-back."""
    backup_path = safe_io.safe_write_bytes(
        destination,
        payload,
        keep_days=_IMPORT_BACKUP_KEEP_DAYS,
    )
    return None if backup_path is None else str(backup_path)


def _collect_entries_from_zip_payload(
    payload: bytes, ignored_files: List[str]
) -> List[Tuple[str, str, bytes]]:
    """Read a zip upload without touching the live runtime upload directory."""
    entries: List[Tuple[str, str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member_name in archive.namelist():
            if member_name.endswith("/"):
                continue
            file_name = Path(member_name).name
            if not file_name or file_name.startswith("."):
                continue
            if is_auxiliary_knowledge_file(file_name):
                ignored_files.append(file_name)
                continue
            kind = classify_import_kind(file_name, member_name)
            if kind == "unknown":
                continue
            with archive.open(member_name) as raw_file:
                entries.append((kind, file_name, raw_file.read()))
    return entries


def _collect_entries_from_upload(
    uploaded, ignored_files: List[str],
) -> List[Tuple[str, str, bytes]]:
    file_name = Path(uploaded.filename or "").name
    if not file_name:
        raise ValueError("上传文件名为空。")
    if is_auxiliary_knowledge_file(file_name):
        ignored_files.append(file_name)
        return []
    if uploaded.file is None:
        raise ValueError(f"上传文件无效：{file_name}")
    payload = uploaded.file.read()
    suffix = Path(file_name).suffix.lower()
    if suffix == ".zip":
        return _collect_entries_from_zip_payload(payload, ignored_files)
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


def _parse_uploaded_shelves(payload: bytes, file_name: str):
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"库位表编码错误：{file_name}") from error
    return parse_shelf_locations(io.StringIO(text))


def _validate_entry(
    kind: str, file_name: str, payload: bytes, staging_root: Path, index: int,
    expected_ids: Collection[str] = (),
) -> bool:
    """Validate one payload before any configured file is changed."""
    if kind == "knowledge":
        value = _decode_json(payload, file_name)
        if not is_knowledge_record(value, file_name, expected_ids):
            return False
        load_knowledge_from_mapping([(file_name, value)])
        return True
    if kind == "shelves":
        _parse_uploaded_shelves(payload, file_name)
        return True
    if kind == "order_config" or kind == "order_config_prod":
        value = _decode_json(payload, file_name)
        if not isinstance(value, dict):
            raise ValueError(f"下单配置根节点必须是对象：{file_name}")
        validate_order_config_types(value)
        return True

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
    return True


def _safe_knowledge_destination(knowledge_dir: Path, file_name: str) -> Path:
    root = knowledge_dir.expanduser().resolve()
    destination = (root / file_name).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise ValueError(f"knowledge 文件名无效：{file_name}") from error
    return destination


def _copy_import_base(
    targets: Dict[str, Path], expected_ids: Collection[str],
    replaced_knowledge: Collection[str],
) -> List[str]:
    sources = state.loaded_paths or bundle_path_map(configured_bundle())
    copies = []
    ignored_files: List[str] = []
    for kind in DATASET_IMPORT_KINDS:
        source = sources.get(kind)
        if source is None:
            continue
        source = Path(source)
        if kind == "knowledge" and source.is_dir():
            targets[kind].mkdir(parents=True, exist_ok=True)
            files, ignored = list_knowledge_files(source)
            ignored_files.extend(ignored)
            for path in files:
                if path.name in replaced_knowledge:
                    continue
                value = _decode_json(path.read_bytes(), path.name)
                if is_knowledge_record(value, path.name, expected_ids):
                    load_knowledge_from_mapping([(path.name, value)])
                    copies.append((path, targets[kind] / path.name))
                else:
                    ignored_files.append(path.name)
        elif kind != "knowledge" and source.is_file():
            copies.append((source, targets[kind]))
    for done, (source, target) in enumerate(copies, 1):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        load_progress.update("copy", f"复制当前加载文件 {done}/{len(copies)}", done, len(copies))
    return ignored_files


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


def import_uploaded_files(form) -> Dict[str, object]:
    uploads = get_uploaded_files(form, "files")
    if not uploads:
        uploads = get_uploaded_files(form, "bundle_zip")
    if not uploads:
        raise ValueError("请选择要导入的压缩包或文件。")

    state_snapshot = {
        key: getattr(state, key) for key in (
            "configured_knowledge", "configured_knowledge_root", "configured_shelves",
            "configured_unavailable", "configured_tool_mapping", "configured_pick_strategy",
            "_explicit_config_keys", "loaded_dataset", "loaded_tool_mapping",
            "loaded_closed_loop_ids", "loaded_unavailable_ids", "loaded_paths",
            "data_source_ready", "data_load_method", "edit_workspace", "data_revision", "shelves_source",
        )
    }
    snapshots: Dict[Path, Optional[bytes]] = {}
    try:
        entries: List[Tuple[str, str, bytes]] = []
        source_names: List[str] = []
        ignored_files: List[str] = []
        load_progress.update("upload", "接收导入文件")
        for uploaded in uploads:
            name = Path(uploaded.filename or "").name
            if not name:
                continue
            source_names.append(name)
            entries.extend(_collect_entries_from_upload(uploaded, ignored_files))

        if not entries and not ignored_files:
            raise ValueError("未从上传内容中识别到可导入的配置或数据文件。")

        expected_ids = set()
        shelf_uploads = [(name, payload) for kind, name, payload in entries if kind == "shelves"]
        if shelf_uploads:
            name, payload = shelf_uploads[-1]
            expected_ids.update(_parse_uploaded_shelves(payload, name).entries)
        elif any(kind in DATASET_IMPORT_KINDS for kind, _name, _payload in entries):
            shelves = state.loaded_paths.get("shelves") or state.configured_shelves
            if shelves is not None and Path(shelves).is_file():
                expected_ids.update(load_shelf_locations(Path(shelves)).entries)
            elif state.loaded_dataset is not None:
                expected_ids.update(state.loaded_dataset.shelf_entries)
        if state.loaded_dataset is not None:
            expected_ids.update(str(record["id"]) for record in state.loaded_dataset.knowledge_records)
        reload_info = None
        with data_storage.staged_dataset() as staging:
            config = staging / "config_pnp"
            targets = {
                "knowledge": staging / "knowledge",
                "shelves": config / SHELVES_FILE_NAME,
                "unavailable": config / "unavailable_obj.json",
                "tool_mapping": config / TOOL_MAPPING_FILE_NAME,
                "pick_strategy": config / PICK_STRATEGY_FILE_NAME,
                "order_config": ORDER_CONFIG_FILE,
                "order_config_prod": ORDER_CONFIG_PROD_FILE,
            }
            plans = []
            for index, (kind, file_name, payload) in enumerate(entries):
                if not _validate_entry(kind, file_name, payload, staging, index, expected_ids):
                    ignored_files.append(file_name)
                    continue
                destination = (
                    _safe_knowledge_destination(targets[kind], file_name)
                    if kind == "knowledge" else targets[kind]
                )
                plans.append((kind, file_name, payload, destination))
                load_progress.update("validate", f"校验导入文件 {index + 1}/{len(entries)}", index + 1, len(entries))
            touched = {kind for kind, _name, _payload, _destination in plans} & DATASET_IMPORT_KINDS
            if touched:
                load_progress.update("copy", "复制当前加载文件")
                ignored_files.extend(_copy_import_base(
                    targets, expected_ids,
                    {name for kind, name, _payload, _destination in plans if kind == "knowledge"},
                ))
            snapshots = _snapshot_targets([
                destination for kind, _name, _payload, destination in plans
                if kind not in DATASET_IMPORT_KINDS
            ])
            written = []
            backup_count = 0
            for done, (kind, file_name, payload, destination) in enumerate(plans, 1):
                backup_path = None
                if kind in DATASET_IMPORT_KINDS:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    target = data_storage.CURRENT_DIRECTORY / destination.relative_to(staging)
                else:
                    backup_path = _write_bytes(destination, payload)
                    target = destination
                item = {
                    "kind": kind, "label": KIND_LABELS[kind],
                    "source": file_name, "target": str(target),
                }
                if backup_path:
                    item["backup"] = backup_path
                    backup_count += 1
                written.append(item)
                load_progress.update("import", f"写入导入文件 {done}/{len(plans)}", done, len(plans))

            if touched:
                available = {
                    kind: path for kind, path in targets.items()
                    if kind in DATASET_IMPORT_KINDS
                    and (path.is_dir() if kind == "knowledge" else path.is_file())
                }
                complete = "knowledge" in available and "shelves" in available
                if not complete and state.data_source_ready:
                    raise ValueError("当前加载文件不完整，已保留正在使用的数据；请补齐 Knowledge 和库位表后重试。")
                backup_count += int(data_storage.CURRENT_DIRECTORY.exists())
                if complete:
                    bundle = BundlePaths(
                        available["knowledge"], available["shelves"],
                        available.get("unavailable"), available.get("tool_mapping"),
                        available.get("pick_strategy"),
                        ignored_knowledge_files=tuple(ignored_files),
                    )
                    dataset, tools, closed, unavailable, elapsed = install_staged_dataset(
                        staging, bundle,
                        shelves_source="local" if "shelves" in touched else state.shelves_source,
                    )
                    reload_info = {
                        "count": len(dataset.shelf_entries),
                        "knowledge_dictionary_count": len(dataset.knowledge_records),
                        "elapsed_seconds": round(elapsed, 2),
                        "unavailable_ids": unavailable,
                        "tool_mapping_count": 0 if tools is None else len(tools),
                        "closed_loop_count": 0 if closed is None else len(closed),
                        "load_method": "paths", "capabilities": state.load_capabilities("paths"),
                        "shelves_source": state.shelves_source,
                    }
                else:
                    load_progress.update("publish", "备份上次数据并保存导入副本")
                    with data_storage.publish_dataset(staging) as current:
                        state.loaded_paths = {
                            kind: current / path.relative_to(staging)
                            for kind, path in available.items()
                        }
                for kind in touched:
                    setattr(state, f"configured_{kind}", state.loaded_paths[kind])
                if "knowledge" in touched:
                    state.configured_knowledge_root = data_storage.CURRENT_DIRECTORY
                state._explicit_config_keys = state._explicit_config_keys | touched

        ignored_files = list(dict.fromkeys(ignored_files))
        knowledge_count = sum(1 for kind, _name, _payload, _destination in plans if kind == "knowledge")
        message = f"已导入 {len(written)} 项。"
        if ignored_files:
            message += f" 已忽略 {len(ignored_files)} 个辅助或无药品标识的文件。"
        if backup_count:
            message += f" 已生成 {backup_count} 份备份。"

        result: Dict[str, object] = {
            "ok": True,
            "source_files": source_names,
            "written": written,
            "ignored_files": ignored_files,
            "knowledge_files": knowledge_count,
            "shelves_updated": "shelves" in touched,
            "backup_count": backup_count,
            "reloaded": False,
            "message": message,
        }

        if reload_info is not None:
            result["reloaded"] = True
            result["reload"] = reload_info
            result["load_method"] = "paths"
            result["capabilities"] = reload_info["capabilities"]
            result["message"] = (
                message + f" 已自动重新加载数据（{reload_info['count']} 条）。"
            )
        elif touched:
            result["message"] = (
                message + " 尚未同时具备 Knowledge 目录与库位表，请到「本机路径」加载。"
            )
        else:
            result["message"] = message + " 未改动药品数据文件，无需重新加载。"

        return result
    except Exception:
        if snapshots:
            _restore_targets(snapshots)
        for key, value in state_snapshot.items():
            setattr(state, key, value)
        raise
