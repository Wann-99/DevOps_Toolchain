"""Load datasets from configured paths or uploaded zip bundles."""

from __future__ import annotations

import cgi
import shutil
import time
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from ksq.bundle import extract_bundle_from_zip
from ksq.constants import RUNTIME_UPLOAD_DIRECTORY
from ksq.dataset import build_dataset
from ksq.models import Dataset
from ksq.side_data import load_closed_loop_ids, load_tool_mapping, load_unavailable_ids
from ksq.web import state


def parse_optional_path(raw_value: object, label: str) -> Optional[Path]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"{label}路径必须是字符串。")
    stripped = raw_value.strip()
    if not stripped:
        return None
    path = Path(stripped).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    return path


def existing_optional_path(raw_path: str) -> Optional[Path]:
    path = Path(raw_path).expanduser().resolve()
    return path if path.is_file() else None


def load_optional_side_data(
    unavailable_path: Optional[Path],
    tool_mapping_path: Optional[Path],
    pick_strategy_path: Optional[Path],
) -> Tuple[Optional[Dict[str, str]], Optional[FrozenSet[str]], List[str]]:
    tool_mapping = (
        None if tool_mapping_path is None else load_tool_mapping(tool_mapping_path)
    )
    closed_loop_ids = (
        None if pick_strategy_path is None else load_closed_loop_ids(pick_strategy_path)
    )
    unavailable_ids = (
        [] if unavailable_path is None else load_unavailable_ids(unavailable_path)
    )
    return tool_mapping, closed_loop_ids, unavailable_ids


def get_uploaded_files(form: cgi.FieldStorage, field_name: str) -> List[cgi.FieldStorage]:
    if field_name not in form:
        return []
    fields = form[field_name]
    return fields if isinstance(fields, list) else [fields]


def save_uploaded_file(uploaded: cgi.FieldStorage, destination: Path) -> None:
    if uploaded.filename is None or uploaded.file is None:
        raise ValueError(f"上传文件无效：{destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as file:
        shutil.copyfileobj(uploaded.file, file)


def prepare_runtime_upload_directory() -> Path:
    if RUNTIME_UPLOAD_DIRECTORY.exists():
        shutil.rmtree(RUNTIME_UPLOAD_DIRECTORY)
    RUNTIME_UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return RUNTIME_UPLOAD_DIRECTORY


def load_from_configured_paths() -> Tuple[
    Dataset, Optional[Dict[str, str]], Optional[FrozenSet[str]], List[str], float
]:
    started = time.perf_counter()
    dataset = build_dataset(state.configured_knowledge, state.configured_shelves)
    tool_mapping, closed_loop_ids, unavailable_ids = load_optional_side_data(
        state.configured_unavailable,
        state.configured_tool_mapping,
        state.configured_pick_strategy,
    )
    return (
        dataset,
        tool_mapping,
        closed_loop_ids,
        unavailable_ids,
        time.perf_counter() - started,
    )


def configured_paths_ready() -> bool:
    knowledge = state.configured_knowledge
    shelves = state.configured_shelves
    return bool(
        knowledge
        and Path(knowledge).is_dir()
        and shelves
        and Path(shelves).is_file()
    )


def apply_configured_paths_reload() -> Dict[str, object]:
    """Reload in-memory dataset from configured host paths (full features)."""
    from ksq.web import edit_workspace

    # Re-parse config_pnp/config.py so device-side file-name changes take
    # effect without a restart.  Explicit CLI arguments are preserved.
    state.reload_config_pnp_paths()
    dataset, tool_mapping, closed_loop_ids, unavailable_ids, elapsed = (
        load_from_configured_paths()
    )
    state.loaded_dataset = dataset
    state.loaded_tool_mapping = tool_mapping
    state.loaded_closed_loop_ids = closed_loop_ids
    state.loaded_unavailable_ids = (
        None
        if state.configured_unavailable is None
        else frozenset(unavailable_ids)
    )
    state.data_source_ready = True
    state.data_load_method = "paths"
    edit_workspace.init_workspace_from_loaded()
    state.bump_data_revision()
    return {
        "count": len(dataset.shelf_entries),
        "knowledge_dictionary_count": len(dataset.knowledge_records),
        "elapsed_seconds": round(elapsed, 2),
        "unavailable_ids": unavailable_ids,
        "tool_mapping_count": 0 if tool_mapping is None else len(tool_mapping),
        "closed_loop_count": 0 if closed_loop_ids is None else len(closed_loop_ids),
        "load_method": "paths",
        "capabilities": state.load_capabilities("paths"),
    }


def load_uploaded_zip(
    form: cgi.FieldStorage,
) -> Tuple[
    Dataset,
    Optional[Dict[str, str]],
    Optional[FrozenSet[str]],
    List[str],
    Path,
    Path,
    Optional[Path],
    Optional[Path],
    Optional[Path],
]:
    zip_uploads = get_uploaded_files(form, "bundle_zip")
    if not zip_uploads:
        raise ValueError("未上传压缩包。")
    zip_upload = zip_uploads[0]
    zip_name = Path(zip_upload.filename or "").name
    if Path(zip_name).suffix.lower() != ".zip":
        raise ValueError("请上传 .zip 压缩包。")

    upload_directory = prepare_runtime_upload_directory()
    zip_path = upload_directory / zip_name
    save_uploaded_file(zip_upload, zip_path)
    bundle = extract_bundle_from_zip(zip_path, upload_directory / "extracted")
    dataset = build_dataset(bundle.knowledge_directory, bundle.shelves_file)
    tool_mapping, closed_loop_ids, unavailable_ids = load_optional_side_data(
        bundle.unavailable_file,
        bundle.tool_mapping_file,
        bundle.pick_strategy_file,
    )
    return (
        dataset,
        tool_mapping,
        closed_loop_ids,
        unavailable_ids,
        bundle.knowledge_directory,
        bundle.shelves_file,
        bundle.unavailable_file,
        bundle.tool_mapping_file,
        bundle.pick_strategy_file,
    )
