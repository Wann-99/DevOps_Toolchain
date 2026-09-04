"""HTTP request handlers for the knowledge shelf query service."""

from __future__ import annotations

import cgi
import json
import math
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from itertools import chain
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ksq.constants import APP_VERSION
from ksq.feishu.client import FeishuApiError
from ksq.order.broker import OrderBrokerError
from ksq.web import (
    auth,
    dashboard_api,
    edit_workspace,
    logs_api,
    order_api,
    robot_map_api,
    state,
    test_order_api,
)
from ksq.web.robot_map_api import RobotApiError
from ksq.web.import_api import import_uploaded_files
from ksq.web.loader import (
    apply_configured_paths_reload,
    load_from_configured_paths,
    load_uploaded_zip,
    parse_optional_path,
    resolve_knowledge_path,
    resolve_input_path,
)
from ksq.web.logs_api import LogServiceError
from ksq.runtime_logging import get_logger
from ksq.web.pages import (
    build_missing_rows,
    configured_path_field_values,
    format_status_html,
    home_page_html,
    login_page_html,
    order_page_html,
    path_field_bases,
    query_page_html,
    records_payload,
    resolve_static_file,
)


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, object]:
    content_length = _request_content_length(handler)
    raw_body = handler.rfile.read(content_length)
    _mark_request_body_consumed(handler, len(raw_body))
    if not raw_body:
        raise ValueError("请求体为空。")
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象。")
    return payload


def _parse_finite_float(value: object, field: str) -> float:
    """Parse a finite numeric request field at the HTTP trust boundary."""
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是数字。") from error
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数字。")
    return number


def _expected_robot_base_url(payload: Dict[str, object]) -> str:
    expected = payload.get("expected_robot_base_url")
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("底盘连接信息已过期，请刷新地图后重试。")
    return expected


def _mark_request_body_consumed(
    handler: BaseHTTPRequestHandler, amount: int
) -> None:
    """记录已从当前 request stream 读取的字节数。

    ``BaseHTTPRequestHandler`` 复用 HTTP/1.1 连接；当路由在鉴权或权限
    检查处提前返回时，必须只读取当前请求剩余的 body，不能误读下一条
    request。记录读取进度也让异常路径可以安全地完成 drain。
    """
    consumed = getattr(handler, "_ksq_request_body_consumed", 0)
    try:
        consumed = max(0, int(consumed))
    except (TypeError, ValueError):
        consumed = 0
    try:
        amount = max(0, int(amount))
    except (TypeError, ValueError):
        amount = 0
    setattr(handler, "_ksq_request_body_consumed", consumed + amount)


def _request_content_length(handler: BaseHTTPRequestHandler) -> int:
    try:
        return max(0, int(handler.headers.get("Content-Length", "0")))
    except (TypeError, ValueError):
        return 0


def _drain_request_body(handler: BaseHTTPRequestHandler) -> None:
    """Consume only the unread part of the current HTTP request body."""
    content_length = _request_content_length(handler)
    consumed = getattr(handler, "_ksq_request_body_consumed", 0)
    try:
        consumed = min(content_length, max(0, int(consumed)))
    except (TypeError, ValueError):
        consumed = 0
    remaining = content_length - consumed
    while remaining > 0:
        chunk = handler.rfile.read(min(65536, remaining))
        if not chunk:
            break
        consumed += len(chunk)
        remaining -= len(chunk)
    setattr(handler, "_ksq_request_body_consumed", consumed)


def _validate_dashboard_order_payload(payload: Dict[str, object]) -> None:
    """Validate the internal active-order sync payload at the HTTP boundary."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为空。")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 必须是非空数组。")
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ValueError(f"items[{index}] 必须是对象。")
        quantity = raw_item.get("quantity")
        # bool is an int subclass, but is not a meaningful order quantity.
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError(f"items[{index}].quantity 必须是正整数。")
        if quantity <= 0:
            raise ValueError(f"items[{index}].quantity 必须是正整数。")


_LOAD_PATH_STATE_FIELDS = (
    "configured_knowledge",
    "configured_knowledge_root",
    "configured_shelves",
    "configured_unavailable",
    "configured_tool_mapping",
    "configured_pick_strategy",
    "_explicit_config_keys",
    "loaded_dataset",
    "loaded_tool_mapping",
    "loaded_closed_loop_ids",
    "loaded_unavailable_ids",
    "data_source_ready",
    "data_load_method",
    "edit_workspace",
    "data_revision",
)


def _snapshot_load_path_state() -> Dict[str, object]:
    return {
        name: getattr(state, name)
        for name in _LOAD_PATH_STATE_FIELDS
    }


def _restore_load_path_state(snapshot: Dict[str, object]) -> None:
    for name, value in snapshot.items():
        setattr(state, name, value)


LOGGER = get_logger("http")


class QueryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _current_session(self) -> Optional[Dict[str, object]]:
        return auth.session_from_cookie(self.headers.get("Cookie", ""))

    def _require_session(self, path: str) -> Optional[Dict[str, object]]:
        """返回会话；未登录时 API 返回 401，页面跳转到登录页。"""
        session = self._current_session()
        if session is not None:
            return session
        if path.startswith("/api/") or path.startswith("/load"):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "未登录或会话已过期，请重新登录。"},
            )
        else:
            self._send_redirect("/login")
        return None

    def _require_admin(self, session: Dict[str, object]) -> bool:
        if session.get("role") == auth.ROLE_ADMIN:
            return True
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "当前为普通用户，该编辑操作仅管理员可用。"},
        )
        return False

    def _handle_login(self) -> None:
        try:
            payload = read_json_body(self)
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not username or not password:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "请输入用户名和密码。"}
            )
            return
        entry = auth.verify_credentials(username, password)
        if entry is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, {"error": "用户名或密码错误。"}
            )
            return
        token = auth.create_session(entry)
        body = json.dumps(
            {"ok": True, "user": auth.public_user(entry)}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", auth.session_cookie_header(token))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_logout(self) -> None:
        auth.destroy_session(
            auth.token_from_cookie(self.headers.get("Cookie", ""))
        )
        body = b'{"ok": true}'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", auth.clear_cookie_header())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/static/"):
            self._send_static(path[len("/static/") :])
            return
        if path == "/api/health":
            # 公开健康检查端点（start.sh wait_for_service 使用）。
            self._send_json(HTTPStatus.OK, {"ok": True, "version": APP_VERSION})
            return
        if path == "/login":
            if self._current_session() is not None:
                self._send_redirect("/")
                return
            self._send_html(HTTPStatus.OK, login_page_html())
            return
        if path == "/api/auth/me":
            session = self._current_session()
            if session is None:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "未登录。"})
                return
            self._send_json(HTTPStatus.OK, auth.public_user(session))
            return
        session = self._require_session(path)
        if session is None:
            return
        if path == "/":
            self._send_html(HTTPStatus.OK, home_page_html())
            return
        if path == "/query":
            self._send_html(HTTPStatus.OK, query_page_html())
            return
        if path == "/order":
            self._send_html(HTTPStatus.OK, order_page_html())
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
        if path == "/api/logs/stream":
            query = parse_qs(parsed.query)
            service = (query.get("service") or ["0"])[0]
            try:
                tail = int((query.get("tail") or ["800"])[0])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
                return
            last_event_id = self.headers.get("Last-Event-ID", "")
            self._send_log_stream(service, tail, last_event_id)
            return
        if path == "/api/logs":
            query = parse_qs(parsed.query)
            service = (query.get("service") or ["0"])[0]
            since = (query.get("since") or [""])[0]
            cursor = (query.get("cursor") or [""])[0]
            try:
                tail = int((query.get("tail") or ["500"])[0])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
                return
            try:
                self._send_json(
                    HTTPStatus.OK,
                    logs_api.fetch_logs(service, tail, since, cursor),
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
            force = (query.get("refresh") or [""])[0] == "1"
            try:
                self._send_json(
                    HTTPStatus.OK,
                    dashboard_api.get_dashboard_monitor_snapshot(
                        tail, force=force
                    ),
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
            # client_secret 只下发给管理员：本接口不校验角色，普通用户也能读。
            self._send_json(
                HTTPStatus.OK,
                order_api.get_public_config(
                    mode,
                    include_secret=session.get("role") == auth.ROLE_ADMIN,
                ),
            )
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
                    order_api.broker_error_payload(error),
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/order/tasks":
            query = parse_qs(parsed.query)
            try:
                mode = dashboard_api.resolve_dashboard_mode("")
                result = order_api.list_tasks(
                    mode=mode,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["10"])[0],
                    order_by=(query.get("order_by") or ["desc"])[0],
                    status=(query.get("status") or [""])[0],
                    timezone_name=(query.get("tz") or ["Asia/Shanghai"])[0],
                    refresh=(query.get("refresh") or [""])[0],
                )
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except OrderBrokerError as error:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    order_api.broker_error_payload(error),
                )
            return
        if path == "/api/order/business-modes":
            try:
                self._send_json(HTTPStatus.OK, order_api.list_business_modes())
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except OrderBrokerError as error:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    order_api.broker_error_payload(error),
                )
            return
        if path == "/api/order/business-config":
            query = parse_qs(parsed.query)
            try:
                result = order_api.get_business_config(
                    (query.get("store_id") or [""])[0]
                )
                self._send_json(HTTPStatus.OK, result)
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except OrderBrokerError as error:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    order_api.broker_error_payload(error),
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
                    order_api.broker_error_payload(error),
                )
                return
            self._send_json(HTTPStatus.OK, {"status": status, "data": data})
            return
        if path == "/api/map/settings":
            self._send_json(HTTPStatus.OK, robot_map_api.load_settings())
            return
        if path == "/api/map/robot-info":
            try:
                self._send_json(HTTPStatus.OK, robot_map_api.get_robot_info())
            except RobotApiError as error:
                self._send_json(
                    HTTPStatus(error.status_code)
                    if 400 <= error.status_code < 600
                    else HTTPStatus.BAD_GATEWAY,
                    {"error": str(error)},
                )
            return
        if path == "/api/map/speed-limit":
            query = parse_qs(parsed.query)
            expected_base_url = (
                query.get("expected_robot_base_url") or [None]
            )[0]
            try:
                max_speed = robot_map_api.get_max_moving_speed(
                    expected_base_url=expected_base_url
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "min_speed_mps": round(max_speed * 0.1, 6),
                        "max_speed_mps": max_speed,
                        "default_speed_mps": round(max_speed * 0.8, 6),
                    },
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RobotApiError as error:
                self._send_json(
                    HTTPStatus(error.status_code)
                    if 400 <= error.status_code < 600
                    else HTTPStatus.BAD_GATEWAY,
                    {"error": str(error)},
                )
            return
        if path == "/api/map/power":
            try:
                self._send_json(HTTPStatus.OK, robot_map_api.get_power_status())
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/pois":
            try:
                self._send_json(HTTPStatus.OK, {"pois": robot_map_api.list_pois()})
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/pose":
            try:
                self._send_json(HTTPStatus.OK, robot_map_api.get_current_pose())
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/home-pose":
            try:
                self._send_json(
                    HTTPStatus.OK, {"pose": robot_map_api.get_home_pose()}
                )
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/telemetry":
            # The map collector returns one coherent scan/pose/quality snapshot.
            # Keep this endpoint read-only and expose stale snapshots as JSON so
            # the UI can render the last known frame without treating a transient
            # chassis timeout as a missing response.
            try:
                self._send_json(
                    HTTPStatus.OK, robot_map_api.get_telemetry_snapshot()
                )
            except RobotApiError as error:
                self._send_json(
                    HTTPStatus(error.status_code)
                    if 400 <= error.status_code < 600
                    else HTTPStatus.BAD_GATEWAY,
                    {"error": str(error)},
                )
            return
        if path == "/api/map/image":
            try:
                png_bytes, meta = robot_map_api.get_map_image()
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Map-Origin-X", str(meta["origin_x"]))
            self.send_header("X-Map-Origin-Y", str(meta["origin_y"]))
            self.send_header("X-Map-Resolution", str(meta["resolution"]))
            self.send_header("X-Map-Width", str(meta["width"]))
            self.send_header("X-Map-Height", str(meta["height"]))
            self.end_headers()
            self.wfile.write(png_bytes)
            return
        if path == "/api/map/zones":
            try:
                self._send_json(HTTPStatus.OK, robot_map_api.get_zones())
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/events":
            try:
                self._send_json(HTTPStatus.OK, {"events": robot_map_api.get_events()})
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path == "/api/map/current-action":
            query = parse_qs(parsed.query)
            expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
            try:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "active": True,
                        "action": robot_map_api.get_current_action(
                            expected_base_url=expected_base_url
                        ),
                    },
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RobotApiError as error:
                # Slamware returns 404 when no action is running; expose that
                # normal idle state as data instead of a failed status request.
                if error.status_code == 404:
                    self._send_json(
                        HTTPStatus.OK, {"active": False, "action": None}
                    )
                else:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path in ("/api/map/path", "/api/map/milestones"):
            query = parse_qs(parsed.query)
            expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
            reader = (
                robot_map_api.get_remaining_path
                if path == "/api/map/path"
                else robot_map_api.get_remaining_milestones
            )
            try:
                self._send_json(
                    HTTPStatus.OK,
                    reader(expected_base_url=expected_base_url),
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RobotApiError as error:
                if error.status_code == 404:
                    self._send_json(HTTPStatus.OK, {"path_points": []})
                else:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if path.startswith("/api/map/actions/"):
            action_id = unquote(path[len("/api/map/actions/") :]).strip()
            if not action_id or "/" in action_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "action_id 无效。"})
                return
            query = parse_qs(parsed.query)
            expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
            try:
                self._send_json(
                    HTTPStatus.OK,
                    robot_map_api.get_action_status(
                        action_id, expected_base_url=expected_base_url
                    ),
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RobotApiError as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        self._send_not_found(
            path, "Endpoint not found" if path.startswith("/api/") else "Page not found"
        )

    def do_PUT(self) -> None:
        # PUT can also be sent on a persistent HTTP/1.1 connection.  Drain a
        # rejected request before returning so its body cannot become the next
        # request line (the same invariant as do_POST).
        self._ksq_request_body_consumed = 0
        path = urlparse(self.path).path
        session = self._require_session(path)
        if session is None or not self._require_admin(session):
            _drain_request_body(self)
            return
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
            if path == "/api/test-order/config":
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.update_config(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/order/business-config":
                payload = read_json_body(self)
                try:
                    result = order_api.update_business_config(
                        payload.get("store_id"),
                        payload.get("business_mode_code"),
                        payload.get("is_accepting_orders"),
                    )
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/order/tasks/"):
                task_id = unquote(path[len("/api/order/tasks/") :]).strip()
                if not task_id or "/" in task_id:
                    _drain_request_body(self)
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"}
                    )
                    return
                payload = read_json_body(self)
                try:
                    result = order_api.update_task_retail_order(
                        task_id,
                        payload.get("retail_order_id"),
                        payload.get("retail_order_time"),
                    )
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/settings":
                payload = read_json_body(self)
                _expected_robot_base_url(payload)
                try:
                    settings = robot_map_api.save_settings(payload)
                except robot_map_api.RobotConnectionSwitchRequired as error:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": str(error), "code": "force_switch_required"},
                    )
                    return
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, settings)
                return
            _drain_request_body(self)
            self._send_not_found(path, "Endpoint not found")
        except (LookupError, ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as error:
            _drain_request_body(self)
            LOGGER.warning(
                "PUT 请求处理失败 path=%s error=%s",
                urlparse(getattr(self, "path", "")).path,
                error,
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_POST(self) -> None:
        # A handler instance may serve multiple HTTP/1.1 requests.  Reset the
        # per-request counter before any authentication branch can drain it.
        self._ksq_request_body_consumed = 0
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            self._handle_login()
            return
        session = self._require_session(path)
        if session is None:
            _drain_request_body(self)
            return
        if path == "/api/auth/logout":
            _drain_request_body(self)
            self._handle_logout()
            return
        if path == "/api/dashboard/order" and session.get("role") != auth.ROLE_ADMIN:
            _drain_request_body(self)
            self._require_admin(session)
            return
        # 普通用户仅拦截少数编辑类端点，其余操作一律放行。
        if (
            session.get("role") != auth.ROLE_ADMIN
            and path in auth.VIEWER_FORBIDDEN_POST_PATHS
        ):
            _drain_request_body(self)
            self._require_admin(session)
            return
        try:
            if path == "/api/order/preflight":
                _drain_request_body(self)
                try:
                    order_api.ensure_order_creation_allowed()
                except order_api.OrderQueueConflict as error:
                    self._send_json(HTTPStatus.CONFLICT, error.payload())
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
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
                try:
                    result = order_api.refresh_token(mode)
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
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
                _mark_request_body_consumed(self, _request_content_length(self))
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
                state.require_full_data_source("测试下单")
                payload = read_json_body(self)
                with state.DATASET_LOCK:
                    result = test_order_api.clear_list(payload.get("which"))
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/order/create":
                state.require_full_data_source("药品下单")
                payload = read_json_body(self)
                try:
                    status, data, body, order_session = (
                        order_api.create_registered_order(payload, "order")
                    )
                except order_api.OrderQueueConflict as error:
                    self._send_json(HTTPStatus.CONFLICT, error.payload())
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                task_id = order_api.extract_task_id(data) or ""
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": status,
                        "data": data,
                        "request_body": body,
                        "task_id": task_id,
                        "order_session": order_session,
                        "queue": dashboard_api.order_queue_status(),
                    },
                )
                return
            if path == "/api/order/current/cancel":
                payload = read_json_body(self)
                try:
                    result = order_api.operate_current_order(
                        "cancel", payload.get("cancel_reason")
                    )
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except order_api.CurrentOrderConflict as error:
                    self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path in {
                "/api/order/current/manual-claim",
                "/api/order/current/manual-complete",
            }:
                _drain_request_body(self)
                action = (
                    "manual_claim"
                    if path.endswith("/manual-claim")
                    else "manual_complete"
                )
                try:
                    result = order_api.operate_current_order(action)
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except order_api.CurrentOrderConflict as error:
                    self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/order/tasks/") and (
                path.endswith("/cancel")
                or path.endswith("/manual-claim")
                or path.endswith("/manual-complete")
            ):
                remainder = path[len("/api/order/tasks/") :]
                task_id, _, suffix = remainder.rpartition("/")
                task_id = unquote(task_id).strip()
                if not task_id or "/" in task_id:
                    _drain_request_body(self)
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"}
                    )
                    return
                action = {
                    "cancel": "cancel",
                    "manual-claim": "manual_claim",
                    "manual-complete": "manual_complete",
                }[suffix]
                payload = {}
                try:
                    payload = read_json_body(self)
                except ValueError:
                    payload = {}
                try:
                    result = order_api.operate_task(
                        action,
                        task_id,
                        payload.get("cancel_reason"),
                        payload.get("cancel_type") or "user",
                    )
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except order_api.TaskOperationConflict as error:
                    self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path.startswith("/api/order/orders/") and (
                path.endswith("/manual-claim") or path.endswith("/manual-complete")
            ):
                _drain_request_body(self)
                remainder = path[len("/api/order/orders/") :]
                order_no, _, suffix = remainder.rpartition("/")
                order_no = unquote(order_no).strip()
                if not order_no or "/" in order_no:
                    _drain_request_body(self)
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "order_no 无效。"}
                    )
                    return
                action = (
                    "manual_claim"
                    if suffix == "manual-claim"
                    else "manual_complete"
                )
                try:
                    result = order_api.operate_order_action(action, order_no)
                except order_api.ProductionOrderWriteForbidden as error:
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
                    return
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                except OrderBrokerError as error:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        order_api.broker_error_payload(error),
                    )
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/dashboard/order":
                payload = read_json_body(self)
                _validate_dashboard_order_payload(payload)
                result = dashboard_api.set_active_order(payload)
                self._send_json(HTTPStatus.OK, {"ok": True, "order": result})
                return
            if path == "/load-paths":
                payload = read_json_body(self)
                knowledge_raw = payload.get("knowledge")
                shelves_raw = payload.get("shelves")
                if not isinstance(knowledge_raw, str) or not knowledge_raw.strip():
                    raise ValueError("knowledge 路径不能为空。")
                if not isinstance(shelves_raw, str) or not shelves_raw.strip():
                    raise ValueError("shelves 路径不能为空。")
                knowledge_base, config_base = path_field_bases()
                knowledge_path = resolve_knowledge_path(
                    knowledge_raw, knowledge_base
                )
                shelves_path = resolve_input_path(
                    shelves_raw, "库位表", config_base
                )
                if not knowledge_path.is_dir():
                    raise FileNotFoundError(f"Knowledge 目录不存在：{knowledge_path}")
                if not shelves_path.is_file():
                    raise FileNotFoundError(f"库位表不存在：{shelves_path}")
                unavailable_path = parse_optional_path(
                    payload.get("unavailable"), "不可处理列表", config_base
                )
                tool_mapping_path = parse_optional_path(
                    payload.get("tool_mapping"), "工具映射", config_base
                )
                pick_strategy_path = parse_optional_path(
                    payload.get("pick_strategy"), "闭环吸取列表", config_base
                )
                with state.DATASET_LOCK:
                    snapshot = _snapshot_load_path_state()
                    try:
                        state.configured_knowledge = knowledge_path
                        state.configured_shelves = shelves_path
                        state.configured_unavailable = unavailable_path
                        state.configured_tool_mapping = tool_mapping_path
                        state.configured_pick_strategy = pick_strategy_path
                        # Mark user-submitted non-empty paths as explicit so
                        # reload_config_pnp_paths() preserves them over config.py.
                        explicit_keys = {"knowledge", "shelves"}
                        if unavailable_path is not None:
                            explicit_keys.add("unavailable")
                        if tool_mapping_path is not None:
                            explicit_keys.add("tool_mapping")
                        if pick_strategy_path is not None:
                            explicit_keys.add("pick_strategy")
                        state._explicit_config_keys = (
                            state._explicit_config_keys | explicit_keys
                        )
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
                    except Exception:
                        _restore_load_path_state(snapshot)
                        raise
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
            if path == "/load-auto":
                _drain_request_body(self)
                paths: Dict[str, str] = {}
                load_snapshot: Optional[Dict[str, object]] = None
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
                        self._send_json(
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
                        return
                    with state.DATASET_LOCK:
                        load_snapshot = _snapshot_load_path_state()
                        # One-click load discards page/import overrides, while
                        # restoring startup CLI values and their priority.
                        # Clear a stale root when returning to legacy VfmApp
                        # mode; root mode stores the selected templates base.
                        state.configured_knowledge_root = state._cli_knowledge_root
                        if state._cli_knowledge_path is not None:
                            state.configured_knowledge = state._cli_knowledge_path
                        for key, value in state._cli_config_paths.items():
                            setattr(state, f"configured_{key}", value)
                        state._explicit_config_keys = frozenset(
                            state._cli_config_paths
                        )
                        result = apply_configured_paths_reload()
                        paths = configured_path_field_values()
                        dataset = state.loaded_dataset
                        has_unavailable = (
                            state.configured_unavailable is not None
                        )
                        has_tool_mapping = (
                            state.configured_tool_mapping is not None
                        )
                        has_pick_strategy = (
                            state.configured_pick_strategy is not None
                        )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "html": format_status_html(
                                dataset,
                                result["elapsed_seconds"],
                                "已从本机路径加载",
                                str(state.configured_knowledge),
                                str(state.configured_shelves),
                                has_unavailable,
                                has_tool_mapping,
                                has_pick_strategy,
                            ),
                            "missing_rows": build_missing_rows(dataset),
                            "unavailable_ids": result["unavailable_ids"],
                            "has_unavailable": has_unavailable,
                            "load_method": "paths",
                            "capabilities": state.load_capabilities("paths"),
                            "paths": paths,
                        },
                    )
                    return
                except Exception as error:
                    if load_snapshot is not None:
                        with state.DATASET_LOCK:
                            _restore_load_path_state(load_snapshot)
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(error), "paths": paths},
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
                _mark_request_body_consumed(self, _request_content_length(self))
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
                # Persist has no request fields, but clients still send a JSON
                # body.  Drain it so the next keep-alive request starts cleanly.
                _drain_request_body(self)
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
                _drain_request_body(self)
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
                _drain_request_body(self)
                try:
                    result = dashboard_api.preview_feishu_submission()
                except LogServiceError as error:
                    self._send_json(
                        HTTPStatus(error.status_code),
                        {"error": str(error)},
                    )
                    return
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
                _drain_request_body(self)
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
                if session.get("role") != auth.ROLE_ADMIN:
                    # 普通用户放行工作模式切换与自动确认开关；键盘/ETM/飞书等配置项
                    # 保持服务端当前值，且不触发机器人容器重建。
                    payload = {
                        key: value
                        for key, value in {
                            "mode": payload.get("mode"),
                            "auto_confirm": payload.get("auto_confirm"),
                        }.items()
                        if value is not None
                    }
                restart_raw = payload.get("restart_robot", False)
                if not isinstance(restart_raw, bool):
                    raise ValueError("restart_robot 必须是布尔值。")
                restart_robot = restart_raw
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
            if path == "/api/map/navigate":
                payload = read_json_body(self)
                try:
                    x = _parse_finite_float(payload["x"], "x")
                    y = _parse_finite_float(payload["y"], "y")
                except (KeyError, TypeError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "x/y 必须是数字。"})
                    return
                yaw = payload.get("yaw")
                if yaw is not None:
                    yaw = _parse_finite_float(yaw, "yaw")
                speed_mps = None
                if "speed_mps" in payload:
                    if "speed_ratio" in payload:
                        raise ValueError("speed_mps 与 speed_ratio 不能同时设置。")
                    speed_mps = _parse_finite_float(
                        payload["speed_mps"], "speed_mps"
                    )
                    if speed_mps <= 0:
                        raise ValueError("巡逻速度必须大于 0 m/s。")
                speed_ratio = _parse_finite_float(
                    payload.get("speed_ratio", 0.8), "speed_ratio"
                )
                if speed_ratio < 0.1 or speed_ratio > 1:
                    raise ValueError("speed_ratio 必须在 0.1~1 之间。")
                try:
                    move_options = {
                        "yaw": yaw,
                        "precise": bool(payload.get("precise", True)),
                        "speed_ratio": speed_ratio,
                        "expected_base_url": _expected_robot_base_url(payload),
                    }
                    if speed_mps is not None:
                        move_options["speed_mps"] = speed_mps
                    result = robot_map_api.move_to(x, y, **move_options)
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/patrol":
                payload = read_json_body(self)
                try:
                    raw_targets = payload.get("targets")
                    if not isinstance(raw_targets, list) or not raw_targets:
                        raise ValueError("巡逻路线至少需要一个停留点。")
                    targets = []
                    for index, target in enumerate(raw_targets, start=1):
                        if not isinstance(target, dict):
                            raise ValueError(f"第 {index} 个巡逻点格式无效。")
                        targets.append(
                            {
                                "x": _parse_finite_float(
                                    target.get("x"), f"第 {index} 个点 x"
                                ),
                                "y": _parse_finite_float(
                                    target.get("y"), f"第 {index} 个点 y"
                                ),
                            }
                        )
                    speed_mps = _parse_finite_float(
                        payload.get("speed_mps"), "speed_mps"
                    )
                    if speed_mps <= 0:
                        raise ValueError("巡逻速度必须大于 0 m/s。")
                    result = robot_map_api.series_move_to(
                        targets,
                        speed_mps=speed_mps,
                        expected_base_url=_expected_robot_base_url(payload),
                    )
                except ValueError as error:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/actions/cancel":
                payload = read_json_body(self)
                try:
                    robot_map_api.cancel_current_action(
                        expected_base_url=_expected_robot_base_url(payload)
                    )
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/map/gohome":
                payload = read_json_body(self)
                try:
                    result = robot_map_api.go_home(
                        expected_base_url=_expected_robot_base_url(payload)
                    )
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/relocate":
                payload = read_json_body(self)
                try:
                    result = robot_map_api.recover_localization(
                        expected_base_url=_expected_robot_base_url(payload)
                    )
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/pois":
                payload = read_json_body(self)
                name = str(payload.get("name") or "").strip()
                if not name:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "停留点名称不能为空。"})
                    return
                try:
                    x = _parse_finite_float(payload["x"], "x")
                    y = _parse_finite_float(payload["y"], "y")
                except (KeyError, TypeError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "x/y 必须是数字。"})
                    return
                try:
                    result = robot_map_api.create_poi(
                        name,
                        x,
                        y,
                        expected_base_url=_expected_robot_base_url(payload),
                    )
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/map/pois/delete":
                payload = read_json_body(self)
                poi_id = str(payload.get("id") or "").strip()
                if not poi_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "id 不能为空。"})
                    return
                try:
                    robot_map_api.delete_poi(
                        poi_id,
                        expected_base_url=_expected_robot_base_url(payload),
                    )
                except RobotApiError as error:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/reload":
                _drain_request_body(self)
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
            _drain_request_body(self)
            self._send_not_found(path, "Endpoint not found")
        except (
            LookupError,
            ValueError,
            FileNotFoundError,
            json.JSONDecodeError,
            OSError,
            OrderBrokerError,
            RobotApiError,
        ) as error:
            # A validation/precondition error can occur before the route's
            # normal body reader (for example ``require_full_data_source``).
            # Finish the current body before replying on a persistent socket.
            _drain_request_body(self)
            LOGGER.warning(
                "请求处理失败 method=%s path=%s error=%s",
                getattr(self, "command", "?"),
                urlparse(self.path).path,
                error,
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_not_found(self, path: str, message: str) -> None:
        if path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": message})
            return
        self.send_error(HTTPStatus.NOT_FOUND, message)

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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: Dict[str, object]) -> None:
        if int(status) >= 400:
            LOGGER.warning(
                "HTTP 请求返回错误 method=%s path=%s status=%s error=%s",
                getattr(self, "command", "?"),
                urlparse(self.path).path,
                int(status),
                str(payload.get("error") or "")[:500],
            )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_log_stream(
        self, service: str, tail: int, last_event_id: str
    ) -> None:
        events = logs_api.stream_log_events(service, tail, last_event_id)
        try:
            first_event = next(events)
        except LogServiceError as error:
            self._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in chain((first_event,), events):
                frame = logs_api.encode_sse_event(event)
                self.wfile.write(logs_api.encode_http_chunk(frame))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            events.close()

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
        # 只记录路径，不记录查询字符串，避免 Token 等敏感参数进入日志。
        client_address = getattr(self, "client_address", None)
        remote = client_address[0] if client_address else "?"
        LOGGER.info(
            "HTTP %s %s status=%s bytes=%s remote=%s",
            getattr(self, "command", "?"),
            urlparse(self.path).path,
            arguments[1] if len(arguments) > 1 else "?",
            arguments[2] if len(arguments) > 2 else "?",
            remote,
        )

    def handle_error(self, request: object, client_address: object) -> None:
        LOGGER.exception("未处理的 HTTP 请求异常 client=%s", client_address)
