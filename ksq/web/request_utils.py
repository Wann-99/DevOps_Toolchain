from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace
from typing import Dict
import json
import math


def read_form(handler):
    """Use ASGI uploads, or parse a stdlib-adapter request with python-multipart."""
    if hasattr(handler, "form"):
        return handler.form
    from python_multipart import parse_form

    form = {}
    def on_file(upload):
        upload.file_object.seek(0)
        name = upload.field_name.decode("utf-8")
        form.setdefault(name, []).append(SimpleNamespace(
            filename=upload.file_name.decode("utf-8"), file=upload.file_object,
        ))
    headers = {name: handler.headers.get(name, "").encode("latin-1")
               for name in ("Content-Type", "Content-Length")}
    parse_form(headers, handler.rfile, None, on_file)
    return form


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
