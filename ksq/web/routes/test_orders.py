from __future__ import annotations

from http import HTTPStatus
from urllib.parse import urlparse

from ksq.data import state as state
from ksq.order import test_service as test_order_api
from ksq.web.request_utils import read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/test-order/state":
        try:
            with state.DATASET_LOCK:
                result = test_order_api.get_state()
            handler._send_json(HTTPStatus.OK, result)
        except (ValueError, FileNotFoundError, OSError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/test-order/export.csv":
        try:
            filename, body = test_order_api.export_pending_csv()
            handler._send_bytes(
                HTTPStatus.OK,
                body,
                "text/csv; charset=utf-8",
                filename,
            )
        except (ValueError, FileNotFoundError, OSError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    if path == "/api/test-order/export-ordered.csv":
        try:
            filename, body = test_order_api.export_ordered_csv()
            handler._send_bytes(
                HTTPStatus.OK,
                body,
                "text/csv; charset=utf-8",
                filename,
            )
        except (ValueError, FileNotFoundError, OSError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    return False


def handle_put(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/test-order/config":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.update_config(payload)
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/test-order/generate":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.generate(payload)
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/test-order/import":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.import_csv(payload)
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/test-order/mark-ordered":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.mark_ordered(payload)
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/test-order/restore":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.restore(payload)
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/test-order/clear":
        state.require_full_data_source("测试下单")
        payload = read_json_body(handler)
        with state.DATASET_LOCK:
            result = test_order_api.clear_list(payload.get("which"))
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'GET': ('/api/test-order/state', '/api/test-order/export.csv', '/api/test-order/export-ordered.csv'), 'PUT': ('/api/test-order/config',), 'POST': ('/api/test-order/generate', '/api/test-order/import', '/api/test-order/mark-ordered', '/api/test-order/restore', '/api/test-order/clear')}
