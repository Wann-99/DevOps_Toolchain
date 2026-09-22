from __future__ import annotations

from http import HTTPStatus
from typing import Dict
from urllib.parse import parse_qs, unquote, urlparse
import time

from ksq.dashboard import settings as dashboard_settings
from ksq.data import progress as load_progress
from ksq.data import service as data_service
from ksq.data import state as state
from ksq.data import workspace as edit_workspace
from ksq.data.imports import import_uploaded_files
from ksq.data.loader import CLOUD_SHELVES_URL, apply_configured_paths_reload, load_uploaded_zip, parse_shelves_source
from ksq.data.paths import configured_path_field_values
from ksq.order import active as active_orders
from ksq.web.pages import build_missing_rows, format_status_html, home_page_html, order_page_html, query_page_html, records_payload
from ksq.web.request_utils import read_form, _drain_request_body, _mark_request_body_consumed, _request_content_length, read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/load-progress":
        load_id = (parse_qs(parsed.query).get("id") or [""])[0]
        handler._send_json(
            HTTPStatus.OK,
            load_progress.snapshot(load_id, str(session.get("username") or "")),
        )
        return True
    if path == "/":
        handler._send_html(HTTPStatus.OK, home_page_html())
        return True
    if path == "/query":
        handler._send_html(HTTPStatus.OK, query_page_html())
        return True
    if path == "/order":
        handler._send_html(HTTPStatus.OK, order_page_html())
        return True
    if path == "/api/status":
        with state.DATASET_LOCK:
            dataset = state.loaded_dataset
            load_method = state.data_load_method
            reloadable = load_method == "paths"
            capabilities = state.load_capabilities(load_method)
        handler._send_json(
            HTTPStatus.OK,
            {
                "loaded": dataset is not None,
                "count": 0 if dataset is None else len(dataset.shelf_entries),
                "knowledge_dictionary_count": 0
                if dataset is None
                else len(dataset.knowledge_records),
                "reloadable": reloadable,
                "load_method": load_method,
                "capabilities": capabilities,
                "capability_message": state.BUNDLE_CAPABILITY_MESSAGE
                if load_method == "bundle"
                else "",
                "data_revision": state.data_revision,
                "dashboard_mode": dashboard_settings.resolve_dashboard_mode(""),
                "active_order_keys": active_orders.active_order_blocking_keys(),
            },
        )
        return True
    if path == "/api/records":
        with state.DATASET_LOCK:
            dataset = state.loaded_dataset
            tool_mapping = state.loaded_tool_mapping
            closed_loop_ids = state.loaded_closed_loop_ids
            unavailable_ids = state.loaded_unavailable_ids
            if (
                dataset is not None
                and state.edit_workspace is None
                and state.data_load_method == "paths"
            ):
                edit_workspace.init_workspace_from_loaded()
        if dataset is None:
            handler._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "尚未加载数据，请先返回首页加载。"},
            )
            return True
        handler._send_json(
            HTTPStatus.OK,
            records_payload(
                dataset, tool_mapping, closed_loop_ids, unavailable_ids
            ),
        )
        return True
    if path == "/api/export/files":
        try:
            with state.DATASET_LOCK:
                payload = edit_workspace.list_export_files()
            handler._send_json(HTTPStatus.OK, payload)
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/export/zip":
        try:
            with state.DATASET_LOCK:
                body = edit_workspace.build_export_zip_bytes()
            handler._send_bytes(
                HTTPStatus.OK,
                body,
                "application/zip",
                "knowledge_bundle_edited.zip",
            )
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/export/knowledge-zip":
        try:
            with state.DATASET_LOCK:
                body = edit_workspace.build_knowledge_zip_bytes()
            handler._send_bytes(
                HTTPStatus.OK,
                body,
                "application/zip",
                "knowledge_folder.zip",
            )
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/export/file":
        query = parse_qs(parsed.query)
        name = unquote((query.get("name") or [""])[0]).strip()
        try:
            with state.DATASET_LOCK:
                filename, body, content_type = edit_workspace.build_export_file(
                    name
                )
            handler._send_bytes(HTTPStatus.OK, body, content_type, filename)
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path in {"/api/export/missing.csv", "/api/export/missing-knowledge-zip"}:
        query = parse_qs(parsed.query)
        exclude = (query.get("exclude_unavailable") or ["0"])[0] in {
            "1",
            "true",
            "True",
        }
        try:
            with state.DATASET_LOCK:
                dataset = state.loaded_dataset
                if dataset is None:
                    raise ValueError("尚未加载数据。")
                rows = build_missing_rows(dataset)
                if exclude and state.loaded_unavailable_ids:
                    rows = [
                        row
                        for row in rows
                        if str(row[0]) not in state.loaded_unavailable_ids
                    ]
                if path == "/api/export/missing.csv":
                    if not rows:
                        raise ValueError("当前没有缺少 knowledge 的药品。")
                    body = edit_workspace.build_missing_rows_csv_bytes(rows)
                    handler._send_bytes(
                        HTTPStatus.OK,
                        body,
                        "text/csv; charset=utf-8",
                        "missing_knowledge.csv",
                    )
                else:
                    body = edit_workspace.build_missing_knowledge_zip_bytes(rows)
                    handler._send_bytes(
                        HTTPStatus.OK,
                        body,
                        "application/zip",
                        "missing_knowledge_templates.zip",
                    )
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/import":
        form = read_form(handler)
        _mark_request_body_consumed(handler, _request_content_length(handler))
        with state.DATASET_LOCK:
            result = import_uploaded_files(form)
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/load-paths":
        payload = read_json_body(handler)
        (dataset, elapsed, shelves_source, knowledge_path, shelves_path, unavailable_path, tool_mapping_path, pick_strategy_path, unavailable_ids) = data_service.load_paths(payload)
        handler._send_json(
            HTTPStatus.OK,
            {
                "html": format_status_html(
                    dataset,
                    elapsed,
                    "已加载云端库位表与本地配置" if shelves_source == "cloud" else "已从本机路径加载",
                    str(knowledge_path),
                    CLOUD_SHELVES_URL if shelves_source == "cloud" else str(shelves_path),
                    unavailable_path is not None or bool(unavailable_ids),
                    tool_mapping_path is not None,
                    pick_strategy_path is not None,
                ),
                "missing_rows": build_missing_rows(dataset),
                "unavailable_ids": unavailable_ids,
                "has_unavailable": unavailable_path is not None or bool(unavailable_ids),
                "load_method": "paths",
                "shelves_source": shelves_source,
                "capabilities": state.load_capabilities("paths"),
            },
        )
        return True
    if path == "/load-auto":
        payload = read_json_body(handler) if _request_content_length(handler) else {}
        shelves_source = parse_shelves_source(payload.get("shelves_source", "local"))
        paths: Dict[str, str] = {}
        try:
            # Pre-check: config_pnp/config.py must exist before
            # attempting a one-click load.  When the file is missing
            # we must NOT fall back to DEFAULT_* paths (which point at
            # non-existent container locations) — return a clear
            # error instead so the user can fix the mount/config.
            config_pnp_dir = state.configured_config_pnp
            config_py_file = (
                (config_pnp_dir / "config.py")
                if config_pnp_dir is not None
                else None
            )
            if config_py_file is None or not config_py_file.is_file():
                with state.DATASET_LOCK:
                    ref_paths = configured_path_field_values()
                search_hint = (
                    str(config_pnp_dir)
                    if config_pnp_dir is not None
                    else "未配置 config_pnp 目录"
                )
                handler._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": (
                            "未找到 config_pnp/config.py"
                            f"（查找路径：{search_hint}），"
                            "请确认 config_pnp 目录已正确挂载或配置"
                        ),
                        "paths": ref_paths,
                    },
                )
                return True
            (result, paths, dataset, has_unavailable, has_tool_mapping, has_pick_strategy) = data_service.load_auto(shelves_source)
            handler._send_json(
                HTTPStatus.OK,
                {
                    "html": format_status_html(
                        dataset,
                        result["elapsed_seconds"],
                        "已加载云端库位表与本地配置" if shelves_source == "cloud" else "已从本机路径加载",
                        str(state.configured_knowledge),
                        CLOUD_SHELVES_URL if shelves_source == "cloud" else str(state.configured_shelves),
                        has_unavailable,
                        has_tool_mapping,
                        has_pick_strategy,
                    ),
                    "missing_rows": build_missing_rows(dataset),
                    "unavailable_ids": result["unavailable_ids"],
                    "has_unavailable": has_unavailable,
                    "load_method": "paths",
                    "shelves_source": shelves_source,
                    "capabilities": state.load_capabilities("paths"),
                    "paths": paths,
                    "source_paths": result["source_paths"],
                },
            )
            return True
        except Exception as error:
            handler._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": str(error), "paths": paths},
            )
            return True
    if path == "/load-upload":
        form = read_form(handler)
        _mark_request_body_consumed(handler, _request_content_length(handler))
        started = time.perf_counter()
        with state.DATASET_LOCK:
            (
                dataset,
                tool_mapping,
                closed_loop_ids,
                unavailable_ids,
                knowledge_path,
                shelves_path,
                unavailable_path,
                tool_mapping_path,
                pick_strategy_path,
            ) = load_uploaded_zip(form)
        handler._send_json(
            HTTPStatus.OK,
            {
                "html": format_status_html(
                    dataset,
                    time.perf_counter() - started,
                    "已从压缩包加载（仅查看）",
                    f"压缩包 knowledge JSON × {dataset.report.knowledge_file_count}",
                    "压缩包 sku-shelves.csv",
                    unavailable_path is not None,
                    tool_mapping_path is not None,
                    pick_strategy_path is not None,
                )
                + (
                    "<p class='meta compact'>"
                    + state.BUNDLE_CAPABILITY_MESSAGE
                    + "</p>"
                ),
                "missing_rows": build_missing_rows(dataset),
                "unavailable_ids": unavailable_ids,
                "has_unavailable": unavailable_path is not None,
                "load_method": "bundle",
                "capabilities": state.load_capabilities("bundle"),
                "capability_message": state.BUNDLE_CAPABILITY_MESSAGE,
            },
        )
        return True
    if path == "/api/edit/save":
        state.require_full_data_source("编辑保存")
        payload = read_json_body(handler)
        item_id = str(payload.get("id") or "").strip()
        field = str(payload.get("field") or "").strip()
        value = payload.get("value")
        if value is None:
            value = ""
        location = payload.get("location")
        location_text = (
            "" if location is None else str(location).strip()
        )
        with state.DATASET_LOCK:
            result = edit_workspace.save_field(
                item_id, field, str(value), location_text or None
            )
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/edit/persist":
        # Persist has no request fields, but clients still send a JSON
        # body.  Drain it so the next keep-alive request starts cleanly.
        _drain_request_body(handler)
        state.require_full_data_source("编辑落盘")
        with state.DATASET_LOCK:
            result = edit_workspace.persist_dirty_files()
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/reload":
        _drain_request_body(handler)
        with state.DATASET_LOCK:
            if not state.data_source_ready:
                raise ValueError("尚未加载数据，请先返回首页加载。")
            if state.data_load_method == "bundle":
                raise ValueError(
                    "重新加载不支持。" + state.BUNDLE_CAPABILITY_MESSAGE
                )
            if state.data_load_method != "paths":
                raise ValueError("尚未从本机路径加载，无法重新加载。")
            result = apply_configured_paths_reload()
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'GET': ('/api/load-progress', '/', '/query', '/order', '/api/status', '/api/records', '/api/export/files', '/api/export/zip', '/api/export/knowledge-zip', '/api/export/file', '/api/export/missing.csv', '/api/export/missing-knowledge-zip'), 'POST': ('/api/import', '/load-paths', '/load-auto', '/load-upload', '/api/edit/save', '/api/edit/persist', '/api/reload')}
