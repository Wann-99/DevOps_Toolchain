from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

from ksq.dashboard import service as dashboard_api
from ksq.feishu.client import FeishuApiError
from ksq.order import active as active_orders
from ksq.robot import keyboard as keyboard
from ksq.robot.logs import LogServiceError
from ksq.web import auth
from ksq.web.request_utils import _drain_request_body, _validate_dashboard_order_payload, read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/dashboard/status":
        query = parse_qs(parsed.query)
        try:
            tail = int((query.get("tail") or ["800"])[0])
        except ValueError:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "tail 无效。"})
            return True
        force = (query.get("refresh") or [""])[0] == "1"
        try:
            handler._send_json(
                HTTPStatus.OK,
                dashboard_api.get_dashboard_monitor_snapshot(
                    tail, force=force
                ),
            )
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
        return True
    if path == "/api/dashboard/keyboard":
        try:
            handler._send_json(HTTPStatus.OK, keyboard.list_keyboard_devices())
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/dashboard/feishu/preview":
        try:
            handler._send_json(
                HTTPStatus.OK, dashboard_api.preview_feishu_submission()
            )
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
        except FeishuApiError as error:
            handler._send_json(
                HTTPStatus.BAD_REQUEST
                if error.status_code < 500
                else HTTPStatus.BAD_GATEWAY,
                {
                    "error": str(error),
                    "status_code": error.status_code,
                    "body": error.body,
                },
            )
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/dashboard/order":
        payload = read_json_body(handler)
        _validate_dashboard_order_payload(payload)
        result = active_orders.set_active_order(payload)
        handler._send_json(HTTPStatus.OK, {"ok": True, "order": result})
        return True
    if path == "/api/dashboard/confirm":
        _drain_request_body(handler)
        try:
            result = dashboard_api.confirm_and_maybe_submit_feishu()
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/dashboard/feishu/preview":
        _drain_request_body(handler)
        try:
            result = dashboard_api.preview_feishu_submission()
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return True
        except FeishuApiError as error:
            handler._send_json(
                HTTPStatus.BAD_REQUEST
                if error.status_code < 500
                else HTTPStatus.BAD_GATEWAY,
                {
                    "error": str(error),
                    "status_code": error.status_code,
                    "body": error.body,
                },
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/dashboard/feishu/submit":
        _drain_request_body(handler)
        try:
            result = dashboard_api.submit_feishu_manual()
        except LogServiceError as error:
            handler._send_json(
                HTTPStatus(error.status_code),
                {"error": str(error)},
            )
            return True
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/dashboard/dismiss":
        payload = read_json_body(handler)
        result = active_orders.dismiss_await(payload.get("fingerprint"))
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/dashboard/keyboard":
        payload = read_json_body(handler)
        if session.get("role") != auth.ROLE_ADMIN:
            # 仪表板自动确认可操作；设置页字段（含工作模式）不可修改。
            if set(payload) - {"auto_confirm", "restart_robot"} or payload.get("restart_robot", False) is not False:
                handler._send_json(HTTPStatus.FORBIDDEN, {"error": "设置编辑仅管理员可用。"})
                return True
            payload = {"auto_confirm": payload["auto_confirm"]} if "auto_confirm" in payload else {}
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
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return True
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
ROUTES = {'GET': ('/api/dashboard/status', '/api/dashboard/keyboard', '/api/dashboard/feishu/preview'), 'POST': ('/api/dashboard/order', '/api/dashboard/confirm', '/api/dashboard/feishu/preview', '/api/dashboard/feishu/submit', '/api/dashboard/dismiss', '/api/dashboard/keyboard')}
