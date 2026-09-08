"""Load datasets from configured paths or uploaded zip bundles."""

from __future__ import annotations

import cgi
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from ksq.bundle import extract_bundle_from_zip
from ksq.constants import DEFAULT_ETM_BASE_URL, RUNTIME_UPLOAD_DIRECTORY
from ksq.dataset import build_dataset
from ksq.models import BundlePaths, Dataset
from ksq.side_data import load_closed_loop_ids, load_tool_mapping, load_unavailable_ids
from ksq.web import data_storage, load_progress, state


CLOUD_SHELVES_URL = DEFAULT_ETM_BASE_URL + "/api/v1/sku/locations"
CLOUD_SHELVES_MAX_BYTES = 64 * 1024 * 1024


def parse_shelves_source(value: object) -> str:
    if not isinstance(value, str) or value not in ("local", "cloud"):
        raise ValueError("库位表来源必须是 local 或 cloud。")
    return value


def download_cloud_shelves(destination: Path) -> None:
    load_progress.update("download", "下载云端 SKU 库位表")
    request = urllib.request.Request(
        CLOUD_SHELVES_URL, headers={"Accept": "text/csv, application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isdigit() else 0
            if total > CLOUD_SHELVES_MAX_BYTES:
                raise ValueError("云端 SKU 库位表超过 64 MiB，已取消加载。")
            completed = 0
            with destination.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    completed += len(chunk)
                    if completed > CLOUD_SHELVES_MAX_BYTES:
                        raise ValueError("云端 SKU 库位表超过 64 MiB，已取消加载。")
                    output.write(chunk)
                    load_progress.update("download", "下载云端 SKU 库位表", completed, total)
            if not completed:
                raise ValueError("云端 SKU 接口返回空文件，已保留当前数据。")
            if total and completed != total:
                raise ValueError("云端 SKU 库位表下载不完整，请重试。")
    except urllib.error.HTTPError as error:
        raise ValueError(f"云端 SKU 库位表下载失败（HTTP {error.code}）。") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValueError(f"无法下载云端 SKU 库位表，请检查服务：{CLOUD_SHELVES_URL}") from error


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


def bundle_path_map(bundle: BundlePaths) -> Dict[str, Optional[Path]]:
    return {
        "knowledge": bundle.knowledge_directory,
        "shelves": bundle.shelves_file,
        "unavailable": bundle.unavailable_file,
        "tool_mapping": bundle.tool_mapping_file,
        "pick_strategy": bundle.pick_strategy_file,
    }


def configured_bundle() -> BundlePaths:
    return BundlePaths(
        state.configured_knowledge, state.configured_shelves,
        state.configured_unavailable, state.configured_tool_mapping,
        state.configured_pick_strategy,
    )


def install_staged_dataset(
    staging: Path, bundle: BundlePaths, method: str = "paths", *, shelves_source: str = "local",
) -> tuple:
    """Validate a complete copy, then publish disk and memory as one transaction.

    The caller holds DATASET_LOCK so readers cannot observe the rename window.
    Configured paths remain source paths; loaded_paths always describes data/current.
    """
    from ksq.web import edit_workspace

    started = time.perf_counter()
    load_progress.update("parse", "解析 Knowledge 和库位表")
    dataset = build_dataset(bundle.knowledge_directory, bundle.shelves_file)
    load_progress.update("validate", "校验工具、不可处理和闭环吸取配置")
    tool_mapping, closed_loop_ids, unavailable_ids = load_optional_side_data(
        bundle.unavailable_file, bundle.tool_mapping_file, bundle.pick_strategy_file,
    )
    previous = {name: getattr(state, name) for name in (
        "loaded_dataset", "loaded_tool_mapping", "loaded_closed_loop_ids",
        "loaded_unavailable_ids", "loaded_paths", "data_source_ready",
        "data_load_method", "edit_workspace", "data_revision", "shelves_source",
    )}
    load_progress.update("publish", "备份上次数据并切换当前副本")
    try:
        with data_storage.publish_dataset(staging) as current:
            state.loaded_paths = {
                key: current / path.relative_to(staging) if path is not None else None
                for key, path in bundle_path_map(bundle).items()
            }
            state.loaded_dataset = dataset
            state.loaded_tool_mapping = tool_mapping
            state.loaded_closed_loop_ids = closed_loop_ids
            state.loaded_unavailable_ids = (
                frozenset(unavailable_ids) if bundle.unavailable_file is not None else None
            )
            state.data_source_ready = True
            state.data_load_method = method
            state.shelves_source = shelves_source
            load_progress.update("index", "建立查询索引")
            if method == "paths":
                edit_workspace.init_workspace_from_loaded()
            else:
                state.edit_workspace = None
            state.bump_data_revision()
    except Exception:
        for name, value in previous.items():
            setattr(state, name, value)
        raise
    return dataset, tool_mapping, closed_loop_ids, unavailable_ids, time.perf_counter() - started


def load_bundle_paths(
    bundle: BundlePaths, method: str = "paths", *, shelves_source: str = "local",
) -> tuple:
    started = time.perf_counter()
    parse_shelves_source(shelves_source)
    with data_storage.staged_dataset() as staging:
        if shelves_source == "cloud":
            downloaded = staging / "cloud-shelves.csv"
            download_cloud_shelves(downloaded)
            bundle = replace(bundle, shelves_file=downloaded)
        load_progress.update("copy", "复制加载文件到 data")
        copied = data_storage.copy_dataset_files(
            staging, bundle.knowledge_directory, bundle.shelves_file,
            bundle.unavailable_file, bundle.tool_mapping_file, bundle.pick_strategy_file,
            progress=lambda done, total: load_progress.update(
                "copy", f"复制加载文件 {done}/{total}", done, total,
            ),
        )
        if shelves_source == "cloud":
            downloaded.unlink()
        dataset, tools, closed, unavailable, _elapsed = install_staged_dataset(
            staging, copied, method, shelves_source=shelves_source,
        )
    return dataset, tools, closed, unavailable, time.perf_counter() - started


def load_from_configured_paths() -> Tuple[
    Dataset, Optional[Dict[str, str]], Optional[FrozenSet[str]], List[str], float
]:
    return load_bundle_paths(configured_bundle(), shelves_source=state.shelves_source)


def configured_paths_ready() -> bool:
    knowledge = state.configured_knowledge
    shelves = state.configured_shelves
    return bool(
        knowledge
        and Path(knowledge).is_dir()
        and (state.shelves_source == "cloud" or (shelves and Path(shelves).is_file()))
    )


def apply_configured_paths_reload(
    *, require_vfm_knowledge: bool = False
) -> Dict[str, object]:
    """Refresh source configuration and load a validated private data copy."""
    # Re-parse config_pnp/config.py so device-side file-name changes take
    # effect without a restart.  Explicit CLI arguments are preserved.
    previous = {f"configured_{key}": value for key, value in bundle_path_map(configured_bundle()).items()}
    try:
        load_progress.update("resolve", "定位加载来源")
        state.reload_config_pnp_paths(require_vfm_knowledge=require_vfm_knowledge)
        dataset, tool_mapping, closed_loop_ids, unavailable_ids, elapsed = load_from_configured_paths()
    except Exception:
        for name, value in previous.items():
            setattr(state, name, value)
        raise
    return {
        "count": len(dataset.shelf_entries),
        "knowledge_dictionary_count": len(dataset.knowledge_records),
        "elapsed_seconds": round(elapsed, 2),
        "unavailable_ids": unavailable_ids,
        "tool_mapping_count": 0 if tool_mapping is None else len(tool_mapping),
        "closed_loop_count": 0 if closed_loop_ids is None else len(closed_loop_ids),
        "load_method": "paths",
        "shelves_source": state.shelves_source,
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

    parent = RUNTIME_UPLOAD_DIRECTORY.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ksq-upload-", dir=parent) as directory:
        staging = Path(directory)
        zip_path = staging / zip_name
        load_progress.update("upload", "接收压缩包")
        save_uploaded_file(zip_upload, zip_path)
        load_progress.update("extract", "解压加载文件")
        bundle = extract_bundle_from_zip(zip_path, staging / "extracted")
        dataset, tool_mapping, closed_loop_ids, unavailable_ids, _elapsed = load_bundle_paths(bundle, "bundle")
        return (
            dataset,
            tool_mapping,
            closed_loop_ids,
            unavailable_ids,
            state.loaded_paths["knowledge"],
            state.loaded_paths["shelves"],
            state.loaded_paths["unavailable"],
            state.loaded_paths["tool_mapping"],
            state.loaded_paths["pick_strategy"],
        )
