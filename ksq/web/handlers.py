"""HTTP request handlers for the knowledge shelf query service."""

from __future__ import annotations

import cgi
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, unquote, urlparse

from ksq.feishu.client import FeishuApiError
from ksq.order.broker import OrderBrokerError
from ksq.web import (
    dashboard_api,
    edit_workspace,
    logs_api,
    order_api,
    state,
    test_order_api,
)
from ksq.web.import_api import import_uploaded_files
from ksq.web.loader import (
    apply_configured_paths_reload,
    load_from_configured_paths,
    load_uploaded_zip,
    parse_optional_path,
)
from ksq.web.logs_api import LogServiceError
from ksq.web.pages import (
    build_missing_rows,
    format_status_html,
    home_page_html,
    order_page_html,
    query_page_html,
    records_payload,
    resolve_static_file,
)


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, object]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw_body = handler.rfile.read(content_length)
    if not raw_body:
        raise ValueError("请求体为空。")
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象。")
    return payload


class QueryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(HTTPStatus.OK, home_page_html())
            return
        if path == "/query":
            self._send_html(HTTPStatus.OK, query_page_html())
            return
        if path == "/order":
            self._send_html(HTTPStatus.OK, order_page_html())
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/") :])
            return
        if path == "/api/logs/services":
            try:
                self._send_json(HTTPStatus.OK, logs_api.list_services())
            except LogServiceError as error:
                self._send_json(
                    HTTPStatus(error.status_code),
                    {"error": str(error)},
                )
            return
        if path == "/api/logs":
            query = parse_qs(parsed.query)
            service = (query.get("service") or ["0"])[0]
            since = (query.get("since") or [""])[0]
            try:
                tail = int((query.get("tail") or ["500"])[0])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
                return
            try:
                self._send_json(
                    HTTPStatus.OK, logs_api.fetch_logs(service, tail, since)
                )
            except LogServiceError as error:
                self._send_json(
                    HTTPStatus(error.status_code),
                    {"error": str(error)},
                )
            return
        if path == "/api/dashboard/status":
            query = parse_qs(parsed.query)
            try:
                tail = int((query.get("tail") or ["800"])[0])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
                return
            try:
                self._send_json(
                    HTTPStatus.OK, dashboard_api.get_dashboard_snapshot(tail)
                )
            except LogServiceError as error:
                self._send_json(
                    HTTPStatus(error.status_code),
                    {"error": str(error)},
                )
            return
        if path == "/api/dashboard/keyboard":
            try:
                self._send_json(HTTPStatus.OK, dashboard_api.list_keyboard_devices())
            except LogServiceError as error:
                self._send_json(
                    HTTPStatus(error.status_code),
                    {"error": str(error)},
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/dashboard/feishu/preview":
            try:
                self._send_json(
                    HTTPStatus.OK, dashboard_api.preview_feishu_submission()
                )
            except LogServiceError as error:
                self._send_json(
                    HTTPStatus(error.status_code),
                    {"error": str(error)},
                )
            return
        if path == "/api/dashboard/feishu/site-options":
            try:
                self._send_json(
                    HTTPStatus.OK, dashboard_api.list_feishu_site_options()
                )
            except FeishuApiError as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST
                    if error.status_code < 500
                    else HTTPStatus.BAD_GATEWAY,
                    {
                        "error": str(error),
                        "status_code": error.status_code,
                        "body": error.body,
                    },
                )
            return
        if path == "/api/status":
            with state.DATASET_LOCK:
                dataset = state.loaded_dataset
                load_method = state.data_load_method
                reloadable = load_method == "paths"
                capabilities = state.load_capabilities(load_method)
            self._send_json(
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
                    "dashboard_mode": dashboard_api.resolve_dashboard_mode(""),
                    "active_order_keys": dashboard_api.active_order_blocking_keys(),
                },
            )
            return
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
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "尚未加载数据，请先返回首页加载。"},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                records_payload(
                    dataset, tool_mapping, closed_loop_ids, unavailable_ids
                ),
            )
            return
        if path == "/api/export/files":
            try:
                with state.DATASET_LOCK:
                    payload = edit_workspace.list_export_files()
                self._send_json(HTTPStatus.OK, payload)
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/export/zip":
            try:
                with state.DATASET_LOCK:
                    body = edit_workspace.build_export_zip_bytes()
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "application/zip",
                    "knowledge_bundle_edited.zip",
                )
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/export/knowledge-zip":
            try:
                with state.DATASET_LOCK:
                    body = edit_workspace.build_knowledge_zip_bytes()
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "application/zip",
                    "knowledge_folder.zip",
                )
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/export/file":
            query = parse_qs(parsed.query)
            name = unquote((query.get("name") or [""])[0]).strip()
            try:
                with state.DATASET_LOCK:
                    filename, body, content_type = edit_workspace.build_export_file(
                        name
                    )
                self._send_bytes(HTTPStatus.OK, body, content_type, filename)
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
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
                        self._send_bytes(
                            HTTPStatus.OK,
                            body,
                            "text/csv; charset=utf-8",
                            "missing_knowledge.csv",
                        )
                    else:
                        body = edit_workspace.build_missing_knowledge_zip_bytes(rows)
                        self._send_bytes(
                            HTTPStatus.OK,
                            body,
                            "application/zip",
                            "missing_knowledge_templates.zip",
                        )
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/order/config":
            query = parse_qs(urlparse(self.path).query)
            mode = dashboard_api.resolve_dashboard_mode(
                (query.get("mode") or [""])[0]
            )
            self._send_json(HTTPStatus.OK, order_api.get_public_config(mode))
            return
        if path == "/api/test-order/state":
            try:
                self._send_json(HTTPStatus.OK, test_order_api.get_state())
            except (ValueError, FileNotFoundError, OSError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/test-order/export.csv":
            try:
                filename, body = test_order_api.export_pending_csv()
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "text/csv; charset=utf-8",
                    filename,
                )
            except (ValueError, FileNotFoundError, OSError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/test-order/export-ordered.csv":
            try:
                filename, body = test_order_api.export_ordered_csv()
                self._send_bytes(
                    HTTPStatus.OK,
                    body,
                    "text/csv; charset=utf-8",
                    filename,
                )
            except (ValueError, FileNotFoundError, OSError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/order/stores":
            try:
                query = parse_qs(urlparse(self.path).query)
                mode = dashboard_api.resolve_dashboard_mode(
                    (query.get("mode") or [""])[0]
                )
                self._send_json(HTTPStatus.OK, order_api.list_stores(mode))
            except OrderBrokerError as error:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": str(error), "upstream": error.body},
                )
            return
        if path.startswith("/api/order/tasks/"):
            task_id = unquote(path[len("/api/order/tasks/") :]).strip()
            if not task_id or "/" in task_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"})
                return
            try:
                status, data = order_api.get_task_detail(task_id)
            except OrderBrokerError as error:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": str(error), "upstream": error.body},
                )
                return
            self._send_json(HTTPStatus.OK, {"status": status, "data": data})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Page not found")

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/order/config":
                payload = read_json_body(self)
                mode = payload.get("mode")
                if mode is None:
                    query = parse_qs(urlparse(self.path).query)
                    mode = (query.get("mode") or [""])[0]
                mode = dashboard_api.resolve_dashboard_mode(mode)
                config = order_api.update_config(payload, mode)
                self._send_json(HTTPStatus.OK, config)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")
        except (LookupError, ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/order/token":
                payload = {}
                try:
                    payload = read_json_body(self)
                except ValueError:
                    payload = {}
                query = parse_qs(urlparse(self.path).query)
                mode = dashboard_api.resolve_dashboard_mode(
                    payload.get("mode") or (query.get("mode") or [""])[0]
                )
                result = order_api.refresh_token(mode)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/import":
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    },
                )
                with state.DATASET_LOCK:
                    result = import_uploaded_files(form)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/test-order/generate":
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.generate(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/test-order/import":
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.import_csv(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/test-order/mark-ordered":
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.mark_ordered(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/test-order/restore":
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.restore(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/test-order/clear":
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.clear_list(payload.get("which"))
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/order/create":
                state.require_full_data_source("药品下单")
                payload = read_json_body(self)
                try:
                    status, data, body = order_api.create_order(payload)
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": str(error), "upstream": error.body},
                    )
                    return
                task_id = order_api.extract_task_id(data) or ""
                order_session = dashboard_api.set_active_order_from_create(
                    task_id, body, "order"
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": status,
                        "data": data,
                        "request_body": body,
                        "task_id": task_id,
                        "order_session": order_session,
                    },
                )
                return
            if path == "/api/dashboard/order":
                payload = read_json_body(self)
                result = dashboard_api.set_active_order(payload)
                self._send_json(HTTPStatus.OK, {"ok": True, "order": result})
                return
            if path.startswith("/api/order/tasks/") and path.endswith("/cancel"):
                mid = path[len("/api/order/tasks/") : -len("/cancel")]
                task_id = unquote(mid).strip()
                if not task_id or "/" in task_id:
                    raise ValueError("task_id 无效。")
                payload = read_json_body(self)
                cancel_type = str(payload.get("cancel_type") or "manual").strip()
                cancel_reason = str(payload.get("cancel_reason") or "").strip()
                if not cancel_reason:
                    raise ValueError("cancel_reason 不能为空。")
                try:
                    status, data = order_api.cancel_task(
                        task_id, cancel_type, cancel_reason
                    )
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": str(error), "upstream": error.body},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"status": status, "data": data})
                return
            if path == "/load-paths":
                payload = read_json_body(self)
                knowledge_raw = payload.get("knowledge")
                shelves_raw = payload.get("shelves")
                if not isinstance(knowledge_raw, str) or not knowledge_raw.strip():
                    raise ValueError("knowledge 路径不能为空。")
                if not isinstance(shelves_raw, str) or not shelves_raw.strip():
                    raise ValueError("shelves 路径不能为空。")
                knowledge_path = Path(knowledge_raw).expanduser().resolve()
                shelves_path = Path(shelves_raw).expanduser().resolve()
                if not knowledge_path.is_dir():
                    raise FileNotFoundError(f"Knowledge 目录不存在：{knowledge_path}")
                if not shelves_path.is_file():
                    raise FileNotFoundError(f"库位表不存在：{shelves_path}")
                unavailable_path = parse_optional_path(
                    payload.get("unavailable"), "不可处理列表"
                )
                tool_mapping_path = parse_optional_path(
                    payload.get("tool_mapping"), "工具映射"
                )
                pick_strategy_path = parse_optional_path(
                    payload.get("pick_strategy"), "闭环吸取列表"
                )
                with state.DATASET_LOCK:
                    state.configured_knowledge = knowledge_path
                    state.configured_shelves = shelves_path
                    state.configured_unavailable = unavailable_path
                    state.configured_tool_mapping = tool_mapping_path
                    state.configured_pick_strategy = pick_strategy_path
                    dataset, tool_mapping, closed_loop_ids, unavailable_ids, elapsed = (
                        load_from_configured_paths()
                    )
                    state.loaded_dataset = dataset
                    state.loaded_tool_mapping = tool_mapping
                    state.loaded_closed_loop_ids = closed_loop_ids
                    state.loaded_unavailable_ids = (
                        None
                        if unavailable_path is None
                        else frozenset(unavailable_ids)
                    )
                    state.data_source_ready = True
                    state.data_load_method = "paths"
                    edit_workspace.init_workspace_from_loaded()
                    state.bump_data_revision()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "html": format_status_html(
                            dataset,
                            elapsed,
                            "已从本机路径加载",
                            str(knowledge_path),
                            str(shelves_path),
                            unavailable_path is not None,
                            tool_mapping_path is not None,
                            pick_strategy_path is not None,
                        ),
                        "missing_rows": build_missing_rows(dataset),
                        "unavailable_ids": unavailable_ids,
                        "has_unavailable": unavailable_path is not None,
                        "load_method": "paths",
                        "capabilities": state.load_capabilities("paths"),
                    },
                )
                return
            if path == "/load-upload":
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    },
                )
                started = time.perf_counter()
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
                with state.DATASET_LOCK:
                    state.loaded_dataset = dataset
                    state.loaded_tool_mapping = tool_mapping
                    state.loaded_closed_loop_ids = closed_loop_ids
                    state.loaded_unavailable_ids = (
                        None
                        if unavailable_path is None
                        else frozenset(unavailable_ids)
                    )
                    state.configured_knowledge = knowledge_path
                    state.configured_shelves = shelves_path
                    state.configured_unavailable = unavailable_path
                    state.configured_tool_mapping = tool_mapping_path
                    state.configured_pick_strategy = pick_strategy_path
                    # Bundle preview: query-only; do not treat as writable source.
                    state.data_source_ready = True
                    state.data_load_method = "bundle"
                    state.edit_workspace = None
                    state.bump_data_revision()
                self._send_json(
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
                return
            if path == "/api/edit/save":
                state.require_full_data_source("编辑保存")
                payload = read_json_body(self)
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
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/edit/persist":
                state.require_full_data_source("编辑落盘")
                with state.DATASET_LOCK:
                    result = edit_workspace.persist_dirty_files()
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/services/restart":
                payload = read_json_body(self)
                raw_services = payload.get("services")
                if not isinstance(raw_services, list):
                    raise ValueError("services 必须是字符串数组。")
                service_names = [str(item) for item in raw_services]
                try:
                    result = logs_api.restart_services(service_names)
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/logs/control":
                payload = read_json_body(self)
                service_id = str(payload.get("service") or "").strip()
                action = str(payload.get("action") or "").strip().lower()
                if not service_id:
                    raise ValueError("service 不能为空。")
                if action not in {"start", "restart", "stop"}:
                    raise ValueError("action 仅支持 start / restart / stop。")
                try:
                    result = logs_api.control_service(service_id, action)
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/confirm":
                try:
                    result = dashboard_api.confirm_and_maybe_submit_feishu()
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/feishu/preview":
                try:
                    result = dashboard_api.preview_feishu_submission()
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/feishu/site-options":
                try:
                    result = dashboard_api.list_feishu_site_options()
                except FeishuApiError as error:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST
                        if error.status_code < 500
                        else HTTPStatus.BAD_GATEWAY,
                        {
                            "error": str(error),
                            "status_code": error.status_code,
                            "body": error.body,
                        },
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/feishu/submit":
                try:
                    result = dashboard_api.submit_feishu_manual()
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/dismiss":
                payload = read_json_body(self)
                result = dashboard_api.dismiss_await(payload.get("fingerprint"))
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/keyboard":
                payload = read_json_body(self)
                restart_robot = bool(payload.get("restart_robot"))
                try:
                    # Lock is taken inside, around the settings write only: the
                    # optional container recreate must not hold the global lock.
                    result = dashboard_api.save_dashboard_settings(
                        payload, restart_robot
                    )
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/reload":
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
                self._send_json(HTTPStatus.OK, result)
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")
        except (
            LookupError,
            ValueError,
            FileNotFoundError,
            json.JSONDecodeError,
            OSError,
            OrderBrokerError,
        ) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _send_static(self, relative_path: str) -> None:
        file_path = resolve_static_file(relative_path)
        if file_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Static file not found")
            return
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *arguments: object) -> None:
        return
