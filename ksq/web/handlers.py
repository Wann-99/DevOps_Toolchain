"""HTTP request handlers for the knowledge shelf query service."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from itertools import chain
from typing import Dict, Optional
from urllib.parse import urlparse
import json
import mimetypes

from ksq.constants import APP_VERSION
from ksq.data import progress as load_progress
from ksq.order.broker import OrderBrokerError
from ksq.robot import logs as logs_api
from ksq.robot.logs import LogServiceError
from ksq.robot.service import RobotApiError
from ksq.runtime_logging import get_logger
from ksq.web import auth, files_api
from ksq.web.pages import login_page_html, resolve_static_file
from ksq.web.request_utils import _drain_request_body, read_json_body
from ksq.web.routes import data as data_routes, logs as logs_routes, dashboard as dashboard_routes, test_orders as test_orders_routes, mapping as mapping_routes, map as map_routes, orders as orders_routes


LOGGER = get_logger("http")


class RequestDispatcher:
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

    def _write_allowed(self, session, path) -> bool:
        if path.startswith("/api/map/") or path == "/api/order/config" or path in auth.VIEWER_FORBIDDEN_POST_PATHS:
            return self._require_admin(session)
        if path.startswith(("/api/files/", "/api/terminal/", "/desktop/")):
            try:
                return files_api.authorize_request(self, session)
            except (OSError, ValueError) as error:
                self._send_json(403 if isinstance(error, PermissionError) else 400, {"error": str(error)})
                return False
        return True

    def authorize_write(self) -> bool:
        path = urlparse(self.path).path
        if self.command == "POST" and path == "/api/auth/login":
            return True
        session = self._require_session(path)
        return session is not None and self._write_allowed(session, path)

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
        files_api.close_terminals(auth.token_from_cookie(self.headers.get("Cookie", "")))
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
        if files_api.handle_request(self, session):
            return
        for route in (orders_routes, data_routes, logs_routes, dashboard_routes, test_orders_routes, mapping_routes, map_routes):
            if route.handle_get(self, session):
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
        if session is None or not self._write_allowed(session, path):
            _drain_request_body(self)
            return
        try:
            for route in (orders_routes, test_orders_routes, map_routes):
                if route.handle_put(self, session):
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
        path = urlparse(self.path).path
        if path not in load_progress.LOAD_ENDPOINTS:
            self._do_POST()
            return
        session = self._current_session()
        if session is None:
            self._do_POST()
            return
        self._ksq_request_body_consumed = 0
        try:
            with load_progress.track(
                self.headers.get("X-Load-ID", ""),
                str(session.get("username") or ""),
            ):
                self._do_POST()
        except ValueError as error:
            _drain_request_body(self)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _do_POST(self) -> None:
        # A handler instance may serve multiple HTTP/1.1 requests.  Reset the
        # per-request counter before any authentication branch can drain it.
        self._ksq_request_body_consumed = 0
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            self._handle_login()
            return
        session = self._require_session(path)
        if session is None:
            if path.startswith(("/api/files/", "/api/terminal/")):
                self.close_connection = True
                return
            _drain_request_body(self)
            return
        if files_api.handle_request(self, session):
            return
        if path == "/api/auth/logout":
            _drain_request_body(self)
            self._handle_logout()
            return
        # 普通用户仅拦截少数编辑类端点，其余操作一律放行。
        if not self._write_allowed(session, path):
            _drain_request_body(self)
            return
        try:
            for route in (orders_routes, data_routes, test_orders_routes, dashboard_routes, logs_routes, map_routes, mapping_routes):
                if route.handle_post(self, session):
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
        load_progress.finish(str(payload.get("error") or "加载失败")
                             if int(status) >= 400 else "")
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
        if urlparse(self.path).path.startswith(("/api/files/", "/api/terminal/")):
            self.send_header("Cache-Control", "no-store")
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


class QueryHandler(RequestDispatcher, BaseHTTPRequestHandler):
    """Standard-library adapter retained for host-side contract checks."""
