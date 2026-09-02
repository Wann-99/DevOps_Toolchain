"""HTTP client for Order Broker APIs."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import quote, urlencode


class OrderBrokerError(RuntimeError):
    def __init__(self, message: str, status_code: int, body: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _normalize_server(server: str) -> str:
    return server.rstrip("/")


def _request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, object]],
    token: Optional[str],
) -> Tuple[int, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = int(error.code)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = raw
        raise OrderBrokerError(
            f"Order Broker 请求失败：{method} {url} → HTTP {status}",
            status,
            body,
        ) from error
    except urllib.error.URLError as error:
        raise OrderBrokerError(
            f"无法连接 Order Broker：{url}（{error.reason}）",
            0,
            {"error": str(error.reason)},
        ) from error

    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _extract_access_token(status: int, body: object) -> str:
    if isinstance(body, dict):
        code = body.get("code")
        if code not in (None, 0, "0"):
            msg = str(body.get("msg") or body.get("message") or "未知错误").strip()
            raise OrderBrokerError(
                f"获取 Token 失败：{msg}（code={code}）",
                status,
                body,
            )
        payload = body.get("data")
        token_data = payload if isinstance(payload, dict) else body
    else:
        token_data = body
    if not isinstance(token_data, dict):
        raise OrderBrokerError("Token 响应格式无效。", status, body)
    token = token_data.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise OrderBrokerError("Token 响应缺少 access_token。", status, body)
    return token.strip()


def fetch_access_token(
    server: str,
    client_id: str,
    client_secret: str,
    auth_mode: str,
) -> str:
    """
    auth_mode:
      - client: test Broker → POST /api/client/token
      - user_login: prod Broker → POST /api/users/login
    """
    mode = str(auth_mode or "client").strip().lower()
    if mode == "user_login":
        url = f"{_normalize_server(server)}/api/users/login"
        payload: Dict[str, object] = {
            "username": client_id,
            "password": client_secret,
        }
    elif mode == "client":
        url = f"{_normalize_server(server)}/api/client/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
    else:
        raise ValueError(f"不支持的 Token 认证方式：{auth_mode}")
    status, body = _request_json("POST", url, payload, None)
    if status < 200 or status >= 300:
        raise OrderBrokerError(f"获取 Token 失败：HTTP {status}", status, body)
    return _extract_access_token(status, body)


def list_my_stores(server: str, token: str) -> Tuple[int, object]:
    url = f"{_normalize_server(server)}/api/retail-stores/mine"
    return _request_json("GET", url, None, token)


def create_robot_task(
    server: str, token: str, body: Dict[str, object]
) -> Tuple[int, object]:
    url = f"{_normalize_server(server)}/api/robot-tasks"
    return _request_json("POST", url, body, token)


def get_robot_task(server: str, token: str, task_id: str) -> Tuple[int, object]:
    encoded = quote(task_id, safe="")
    url = f"{_normalize_server(server)}/api/robot-tasks/{encoded}"
    return _request_json("GET", url, None, token)


def list_robot_tasks(
    server: str,
    token: str,
    store_id: str,
    page_size: int,
    order_by: str,
    status: str,
    timezone_name: str,
    cursor: str = "",
) -> Tuple[int, object]:
    query: Dict[str, object] = {
        "store_id": store_id,
        "page_size": page_size,
        "order_by": order_by,
        "tz": timezone_name,
    }
    if status:
        query["status"] = status
    if cursor:
        query["cursor"] = cursor
    url = f"{_normalize_server(server)}/api/robot-tasks?{urlencode(query)}"
    return _request_json("GET", url, None, token)


def cancel_robot_task(
    server: str,
    token: str,
    task_id: str,
    cancel_type: str,
    cancel_reason: str,
) -> Tuple[int, object]:
    encoded = quote(task_id, safe="")
    url = f"{_normalize_server(server)}/api/robot-tasks/{encoded}/cancel"
    return _request_json(
        "POST",
        url,
        {"cancel_type": cancel_type, "cancel_reason": cancel_reason},
        token,
    )


def manual_claim_order(
    server: str, token: str, order_no: str
) -> Tuple[int, object]:
    encoded = quote(order_no, safe="")
    url = f"{_normalize_server(server)}/api/orders/{encoded}/manual-claim"
    return _request_json("POST", url, None, token)


def manual_complete_order(
    server: str, token: str, order_no: str
) -> Tuple[int, object]:
    encoded = quote(order_no, safe="")
    url = f"{_normalize_server(server)}/api/orders/{encoded}/manual-complete"
    return _request_json("POST", url, None, token)


def update_robot_task(
    server: str, token: str, task_id: str, fields: Dict[str, object]
) -> Tuple[int, object]:
    """PUT /api/robot-tasks/{task_id}：更新零售单号等任务字段。"""
    encoded = quote(task_id, safe="")
    url = f"{_normalize_server(server)}/api/robot-tasks/{encoded}"
    return _request_json("PUT", url, fields, token)


def list_business_modes(server: str, token: str) -> Tuple[int, object]:
    url = f"{_normalize_server(server)}/api/business-modes"
    return _request_json("GET", url, None, token)


def get_business_config(
    server: str, token: str, store_id: str
) -> Tuple[int, object]:
    encoded = quote(store_id, safe="")
    url = f"{_normalize_server(server)}/api/retail-stores/{encoded}/business-config"
    return _request_json("GET", url, None, token)


def update_business_config(
    server: str, token: str, store_id: str, body: Dict[str, object]
) -> Tuple[int, object]:
    encoded = quote(store_id, safe="")
    url = f"{_normalize_server(server)}/api/retail-stores/{encoded}/business-config"
    return _request_json("PUT", url, body, token)
