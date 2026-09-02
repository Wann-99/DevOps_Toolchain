"""Load datasets from configured paths or uploaded zip bundles."""

from __future__ import annotations

import cgi
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from ksq.bundle import extract_bundle_from_zip
from ksq.constants import RUNTIME_UPLOAD_DIRECTORY
from ksq.dataset import build_dataset
from ksq.models import Dataset
from ksq.side_data import load_closed_loop_ids, load_tool_mapping, load_unavailable_ids
from ksq.web import state


_RUNTIME_UPLOAD_LOCK = threading.Lock()


def resolve_input_path(
    raw_path: str,
    label: str,
    base_directory: Optional[Path],
) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    if base_directory is None:
        raise ValueError(
            f"{label}缺少当前目录配置，请填写容器内绝对路径。"
        )
    base = base_directory.resolve()
    resolved = (base / path).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as error:
        raise ValueError(f"{label}路径不能超出当前目录。") from error
    return resolved


def resolve_knowledge_path(
    raw_path: str,
    base_directory: Optional[Path],
) -> Path:
    """Resolve a Knowledge path relative to the mounted templates root.

    A VfmApp ``template_root`` names the scene directory, while KSQ reads its
    ``knowledge`` child.  Accept both forms so operators can paste either the
    scene path (with or without the historical ``templates/`` prefix) or the
    final ``.../knowledge`` directory.
    """
    input_path = Path(raw_path).expanduser()
    resolved = resolve_input_path(raw_path, "Knowledge 目录", base_directory)

    def ensure_within_root(candidate: Path) -> Path:
        if base_directory is None:
            return candidate
        try:
            candidate.relative_to(base_directory.resolve())
        except ValueError as error:
            raise ValueError("Knowledge 目录路径不能超出当前目录。") from error
        return candidate

    # ``resolve_input_path`` deliberately accepts absolute paths for the
    # generic side-data fields.  Knowledge in mounted-root mode is stricter:
    # absolute input must also stay below the configured root.
    resolved = ensure_within_root(resolved)

    # Values copied from VfmApp are relative to ``model`` and therefore carry
    # one extra ``templates`` component after the templates root is mounted.
    if (
        not input_path.is_absolute()
        and len(input_path.parts) > 1
        and input_path.parts[0] == "templates"
        and not resolved.is_dir()
    ):
        resolved = resolve_input_path(
            str(Path(*input_path.parts[1:])),
            "Knowledge 目录",
            base_directory,
        )
        resolved = ensure_within_root(resolved)

    # Scene directories conventionally contain the actual JSON files in a
    # ``knowledge`` child.  Prefer that child whenever it exists; an explicit
    # path ending in ``knowledge`` is left unchanged.
    if resolved.is_dir() and resolved.name.lower() != "knowledge":
        child = (resolved / "knowledge").resolve()
        if child.is_dir():
            resolved = ensure_within_root(child)
    return resolved


def parse_optional_path(
    raw_value: object,
    label: str,
    base_directory: Optional[Path],
) -> Optional[Path]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ValueError(f"{label}路径必须是字符串。")
    stripped = raw_value.strip()
    if not stripped:
        return None
    path = resolve_input_path(stripped, label, base_directory)
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
    """Return the runtime upload directory without destroying its contents.

    Callers that replace an uploaded bundle must stage and validate it first;
    this compatibility helper therefore only ensures the directory exists.
    """
    RUNTIME_UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    return RUNTIME_UPLOAD_DIRECTORY


def create_runtime_upload_staging_directory() -> Path:
    """Create a sibling directory for an upload that is not yet committed."""
    parent = RUNTIME_UPLOAD_DIRECTORY.parent
    parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix=f".{RUNTIME_UPLOAD_DIRECTORY.name}.staging-",
            dir=str(parent),
        )
    )


def discard_runtime_upload_staging(staging: Path) -> None:
    """Remove one staging directory, ignoring a directory already committed."""
    if not staging.exists():
        return
    if staging.is_dir() and not staging.is_symlink():
        shutil.rmtree(staging, ignore_errors=True)
    else:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def commit_runtime_upload_directory(staging: Path) -> Path:
    """Publish a validated staging directory as the current upload.

    Directory replacement needs a short rename sequence on POSIX (a non-empty
    directory cannot be passed directly to ``os.replace``).  The old runtime
    is moved aside first and restored if publishing fails, so validation errors
    never remove the currently loaded bundle.
    """
    if staging.is_symlink():
        raise ValueError("上传临时目录无效。")
    parent = RUNTIME_UPLOAD_DIRECTORY.parent.resolve()
    candidate = staging.resolve()
    try:
        candidate.relative_to(parent)
    except ValueError as error:
        raise ValueError("上传临时目录必须位于应用目录内。") from error
    if not candidate.is_dir():
        raise ValueError("上传临时目录无效。")

    previous = parent / f".{RUNTIME_UPLOAD_DIRECTORY.name}.previous-{uuid.uuid4().hex}"
    runtime = RUNTIME_UPLOAD_DIRECTORY
    with _RUNTIME_UPLOAD_LOCK:
        had_previous = runtime.exists() or runtime.is_symlink()
        if had_previous:
            runtime.rename(previous)
        try:
            candidate.rename(runtime)
        except Exception:
            if had_previous and previous.exists():
                previous.rename(runtime)
            raise
        if had_previous:
            discard_runtime_upload_staging(previous)
    return runtime


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


def apply_configured_paths_reload(
    *, require_vfm_knowledge: bool = False
) -> Dict[str, object]:
    """Reload in-memory dataset from configured host paths (full features)."""
    from ksq.web import edit_workspace

    # Re-parse config_pnp/config.py so device-side file-name changes take
    # effect without a restart.  Explicit CLI arguments are preserved.
    state.reload_config_pnp_paths(
        require_vfm_knowledge=require_vfm_knowledge
    )
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

    staging = create_runtime_upload_staging_directory()
    try:
        zip_path = staging / zip_name
        save_uploaded_file(zip_upload, zip_path)
        bundle = extract_bundle_from_zip(zip_path, staging / "extracted")

        # Build the complete dataset and parse optional files while the old
        # runtime directory is still untouched.
        dataset = build_dataset(bundle.knowledge_directory, bundle.shelves_file)
        tool_mapping, closed_loop_ids, unavailable_ids = load_optional_side_data(
            bundle.unavailable_file,
            bundle.tool_mapping_file,
            bundle.pick_strategy_file,
        )
        runtime = commit_runtime_upload_directory(staging)

        def committed(path: Optional[Path]) -> Optional[Path]:
            if path is None:
                return None
            return runtime / path.relative_to(staging)

        return (
            dataset,
            tool_mapping,
            closed_loop_ids,
            unavailable_ids,
            committed(bundle.knowledge_directory),  # type: ignore[arg-type]
            committed(bundle.shelves_file),  # type: ignore[arg-type]
            committed(bundle.unavailable_file),
            committed(bundle.tool_mapping_file),
            committed(bundle.pick_strategy_file),
        )
    except Exception:
        discard_runtime_upload_staging(staging)
        raise
