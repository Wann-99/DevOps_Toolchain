from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

from ksq.robot import logs as logs_api
from ksq.robot.logs import LogServiceError
from ksq.web.request_utils import read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/logs/services":
        try:
            handler._send_json(HTTPStatus.OK, logs_api.list_services())
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
        return True
    if path == "/api/logs/stream":
        query = parse_qs(parsed.query)
        service = (query.get("service") or ["0"])[0]
        try:
            tail = int((query.get("tail") or ["800"])[0])
        except ValueError:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
            return True
        last_event_id = handler.headers.get("Last-Event-ID", "")
        handler._send_log_stream(service, tail, last_event_id)
        return True
    if path == "/api/logs":
        query = parse_qs(parsed.query)
        service = (query.get("service") or ["0"])[0]
        since = (query.get("since") or [""])[0]
        cursor = (query.get("cursor") or [""])[0]
        try:
            tail = int((query.get("tail") or ["500"])[0])
        except ValueError:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
            return True
        try:
            handler._send_json(
                HTTPStatus.OK,
                logs_api.fetch_logs(service, tail, since, cursor),
            )
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/services/restart":
        payload = read_json_body(handler)
        raw_services = payload.get("services")
        if not isinstance(raw_services, list):
            raise ValueError("services 必须是字符串数组。")
        service_names = [str(item) for item in raw_services]
        try:
            result = logs_api.restart_services(service_names)
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/logs/control":
        payload = read_json_body(handler)
        service_id = str(payload.get("service") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if not service_id:
            raise ValueError("service 不能为空。")
        if action not in {"start", "restart", "stop"}:
            raise ValueError("action 仅支持 start / restart / stop。")
        try:
            result = logs_api.control_service(service_id, action)
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'GET': ('/api/logs/services', '/api/logs/stream', '/api/logs'), 'POST': ('/api/services/restart', '/api/logs/control')}
