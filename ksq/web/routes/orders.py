from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urlparse

from ksq.dashboard import settings as dashboard_settings
from ksq.data import state as state
from ksq.order import active as active_orders
from ksq.order import service as order_api
from ksq.order.broker import OrderBrokerError
from ksq.web import auth
from ksq.web.request_utils import _drain_request_body, read_json_body


def handle_get(handler, session) -> bool:
    """Dispatch order reads after the main handler has validated the session."""
    parsed = urlparse(handler.path)
    path = parsed.path
    if not path.startswith("/api/order/"):
        return False
    if path == "/api/order/config":
        query = parse_qs(urlparse(handler.path).query)
        mode = dashboard_settings.resolve_dashboard_mode(
            (query.get("mode") or [""])[0]
        )
        # client_secret 只下发给管理员：本接口不校验角色，普通用户也能读。
        handler._send_json(
            HTTPStatus.OK,
            order_api.get_public_config(
                mode,
                include_secret=session.get("role") == auth.ROLE_ADMIN,
            ),
        )
        return True
    if path == "/api/order/stores":
        try:
            query = parse_qs(urlparse(handler.path).query)
            mode = dashboard_settings.resolve_dashboard_mode(
                (query.get("mode") or [""])[0]
            )
            handler._send_json(HTTPStatus.OK, order_api.list_stores(mode))
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/order/tasks":
        query = parse_qs(parsed.query)
        try:
            mode = dashboard_settings.resolve_dashboard_mode("")
            result = order_api.list_tasks(
                mode=mode,
                page=(query.get("page") or ["1"])[0],
                page_size=(query.get("page_size") or ["10"])[0],
                order_by=(query.get("order_by") or ["desc"])[0],
                status=(query.get("status") or [""])[0],
                timezone_name=(query.get("tz") or ["Asia/Shanghai"])[0],
                refresh=(query.get("refresh") or [""])[0],
            )
            handler._send_json(HTTPStatus.OK, result)
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
        return True
    if path == "/api/order/business-modes":
        try:
            handler._send_json(HTTPStatus.OK, order_api.list_business_modes())
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
        return True
    if path == "/api/order/business-config":
        query = parse_qs(parsed.query)
        try:
            result = order_api.get_business_config(
                (query.get("store_id") or [""])[0]
            )
            handler._send_json(HTTPStatus.OK, result)
        except (ValueError, FileNotFoundError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
        return True
    if path.startswith("/api/order/tasks/"):
        task_id = unquote(path[len("/api/order/tasks/") :]).strip()
        if not task_id or "/" in task_id:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"})
            return True
        try:
            status, data = order_api.get_task_detail(task_id)
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, {"status": status, "data": data})
        return True
    return False


def handle_put(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/order/config":
        payload = read_json_body(handler)
        mode = payload.get("mode")
        if mode is None:
            query = parse_qs(urlparse(handler.path).query)
            mode = (query.get("mode") or [""])[0]
        mode = dashboard_settings.resolve_dashboard_mode(mode)
        config = order_api.update_config(payload, mode)
        handler._send_json(HTTPStatus.OK, config)
        return True
    if path == "/api/order/business-config":
        payload = read_json_body(handler)
        try:
            result = order_api.update_business_config(
                payload.get("store_id"),
                payload.get("business_mode_code"),
                payload.get("is_accepting_orders"),
            )
        except order_api.ProductionOrderWriteForbidden as error:
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path.startswith("/api/order/tasks/"):
        task_id = unquote(path[len("/api/order/tasks/") :]).strip()
        if not task_id or "/" in task_id:
            _drain_request_body(handler)
            handler._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"}
            )
            return True
        payload = read_json_body(handler)
        try:
            result = order_api.update_task_retail_order(
                task_id,
                payload.get("retail_order_id"),
                payload.get("retail_order_time"),
            )
        except order_api.ProductionOrderWriteForbidden as error:
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/order/preflight":
        _drain_request_body(handler)
        try:
            order_api.ensure_order_creation_allowed()
        except order_api.OrderQueueConflict as error:
            handler._send_json(HTTPStatus.CONFLICT, error.payload())
            return True
        handler._send_json(HTTPStatus.OK, {"ok": True})
        return True
    if path == "/api/order/token":
        payload = {}
        try:
            payload = read_json_body(handler)
        except ValueError:
            payload = {}
        query = parse_qs(urlparse(handler.path).query)
        mode = dashboard_settings.resolve_dashboard_mode(
            payload.get("mode") or (query.get("mode") or [""])[0]
        )
        try:
            result = order_api.refresh_token(mode)
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/order/create":
        state.require_full_data_source("药品下单")
        payload = read_json_body(handler)
        unavailable_items = order_api.unavailable_order_items(payload.get("items"))
        try:
            status, data, body, order_session = (
                order_api.create_registered_order(payload, "order")
            )
        except order_api.OrderQueueConflict as error:
            handler._send_json(HTTPStatus.CONFLICT, {
                **error.payload(), "unavailable_items": unavailable_items,
            })
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                {**order_api.broker_error_payload(error), "unavailable_items": unavailable_items},
            )
            return True
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {
                "error": str(error), "unavailable_items": unavailable_items,
            })
            return True
        task_id = order_api.extract_task_id(data) or ""
        handler._send_json(
            HTTPStatus.OK,
            {
                "status": status,
                "data": data,
                "request_body": body,
                "task_id": task_id,
                "order_session": order_session,
                "queue": active_orders.order_queue_status(),
            },
        )
        return True
    if path == "/api/order/current/cancel":
        payload = read_json_body(handler)
        try:
            result = order_api.operate_current_order(
                "cancel", payload.get("cancel_reason")
            )
        except order_api.ProductionOrderWriteForbidden as error:
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except order_api.CurrentOrderConflict as error:
            handler._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path in {
        "/api/order/current/manual-claim",
        "/api/order/current/manual-complete",
    }:
        _drain_request_body(handler)
        action = (
            "manual_claim"
            if path.endswith("/manual-claim")
            else "manual_complete"
        )
        try:
            result = order_api.operate_current_order(action)
        except order_api.ProductionOrderWriteForbidden as error:
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except order_api.CurrentOrderConflict as error:
            handler._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path.startswith("/api/order/tasks/") and (
        path.endswith("/cancel")
        or path.endswith("/manual-claim")
        or path.endswith("/manual-complete")
    ):
        remainder = path[len("/api/order/tasks/") :]
        task_id, _, suffix = remainder.rpartition("/")
        task_id = unquote(task_id).strip()
        if not task_id or "/" in task_id:
            _drain_request_body(handler)
            handler._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "task_id 无效。"}
            )
            return True
        action = {
            "cancel": "cancel",
            "manual-claim": "manual_claim",
            "manual-complete": "manual_complete",
        }[suffix]
        payload = {}
        try:
            payload = read_json_body(handler)
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
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except order_api.TaskOperationConflict as error:
            handler._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path.startswith("/api/order/orders/") and (
        path.endswith("/manual-claim") or path.endswith("/manual-complete")
    ):
        _drain_request_body(handler)
        remainder = path[len("/api/order/orders/") :]
        order_no, _, suffix = remainder.rpartition("/")
        order_no = unquote(order_no).strip()
        if not order_no or "/" in order_no:
            _drain_request_body(handler)
            handler._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "order_no 无效。"}
            )
            return True
        action = (
            "manual_claim"
            if suffix == "manual-claim"
            else "manual_complete"
        )
        try:
            result = order_api.operate_order_action(action, order_no)
        except order_api.ProductionOrderWriteForbidden as error:
            handler._send_json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            return True
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return True
        except OrderBrokerError as error:
            handler._send_json(
                HTTPStatus.BAD_GATEWAY,
                order_api.broker_error_payload(error),
            )
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'PUT': ('/api/order/config', '/api/order/business-config', '/api/order/tasks/{task_id:path}'), 'POST': ('/api/order/preflight', '/api/order/token', '/api/order/create', '/api/order/current/cancel', '/api/order/current/manual-claim', '/api/order/current/manual-complete', '/api/order/tasks/{task_id:path}', '/api/order/orders/{order_no:path}'), 'GET': ('/api/order/config', '/api/order/stores', '/api/order/tasks', '/api/order/business-modes', '/api/order/business-config', '/api/order/tasks/{task_id:path}')}
