"""Order Broker HTTP API helpers used by the web handler."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ksq.constants import ORDER_CONFIG_FILE, ORDER_CONFIG_PROD_FILE
from ksq.order import broker
from ksq.order.config import (
    load_order_config,
    merge_config_update,
    public_order_config,
    save_order_config,
    validate_order_config,
)
from ksq.order.payload import build_create_task_body
from ksq.web import state


class CurrentOrderConflict(RuntimeError):
    """The requested action is not valid for the current active order."""


class TaskOperationConflict(RuntimeError):
    """The requested action is not valid for the referenced Broker task."""


class ProductionOrderWriteForbidden(RuntimeError):
    """Order mutations are intentionally disabled in production mode."""


class OrderQueueConflict(RuntimeError):
    """The two-order local queue is already full."""


_KNOWN_TASK_STATUSES = frozenset(
    {
        "pending",
        "dispatched",
        "running",
        "success",
        "error",
        "cancel",
        "awaiting_pack",
        "manual_claimed_in_progress",
        "manual_claimed_completed",
        "manual_transferred",
        "manual_transferred_completed",
    }
)
_CANCELABLE_TASK_STATUSES = frozenset(
    {"pending", "dispatched", "running", "awaiting_pack"}
)
# 转人工之后 Broker 实际给的是 manual_claimed_in_progress，不是 manual_transferred。
# 两个都收：manual_transferred 仍在状态表里，来源不明，不确定就别删。
_MANUAL_COMPLETABLE_STATUSES = frozenset(
    {"manual_claimed_in_progress", "manual_transferred"}
)
_TASK_LIST_CACHE_SECONDS = 30.0
_TASK_LIST_UPSTREAM_PAGE_SIZE = 50
_TASK_LIST_MAX_BATCHES = 1000
_TASK_LIST_CACHE_LOCK = threading.Lock()
_TASK_LIST_CACHE: Dict[Tuple[str, ...], Tuple[float, List[object]]] = {}
_ORDER_CREATE_LOCK = threading.Lock()


def config_file_for_mode(mode: object) -> Path:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return ORDER_CONFIG_PROD_FILE
    if value not in {"", "test"}:
        raise ValueError("mode 仅支持 test 或 prod。")
    return ORDER_CONFIG_FILE


def get_public_config(mode: object = "test") -> Dict[str, object]:
    config_file = config_file_for_mode(mode)
    config = load_order_config(config_file)
    payload = public_order_config(config)
    payload["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    payload["config_file"] = str(config_file)
    return payload


def _token_cache_key(config: Dict[str, object]) -> str:
    return (
        str(config.get("server") or "").strip()
        + "|"
        + str(config.get("client_id") or "").strip()
    )


def _cached_token(config: Dict[str, object]) -> Optional[str]:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if isinstance(tokens, dict):
            cached = tokens.get(key)
            if isinstance(cached, str) and cached:
                return cached
        # Backward compatible single-slot cache for the default config path.
        if state.order_access_token and key == getattr(
            state, "order_access_token_key", ""
        ):
            return state.order_access_token
    return None


def _store_token(config: Dict[str, object], token: str) -> None:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if not isinstance(tokens, dict):
            tokens = {}
            state.order_access_tokens = tokens
        tokens[key] = token
        state.order_access_token = token
        state.order_access_token_key = key


def _clear_token(config: Dict[str, object]) -> None:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if isinstance(tokens, dict):
            tokens.pop(key, None)
        if getattr(state, "order_access_token_key", "") == key:
            state.order_access_token = None
            state.order_access_token_key = ""


def update_config(
    payload: Dict[str, object], mode: object = "test"
) -> Dict[str, object]:
    config_file = config_file_for_mode(mode)
    current = load_order_config(config_file)
    merged = merge_config_update(current, payload)
    saved = save_order_config(config_file, merged)
    _clear_token(saved)
    clear_task_list_cache()
    result = public_order_config(saved)
    result["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    result["config_file"] = str(config_file)
    return result


def _token_auth_mode(mode: object) -> str:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return "user_login"
    return "client"


def _ensure_token(config: Dict[str, object], mode: object = "test") -> str:
    validate_order_config(config)
    cached = _cached_token(config)
    if cached:
        return cached
    token = broker.fetch_access_token(
        str(config["server"]),
        str(config["client_id"]),
        str(config["client_secret"]),
        _token_auth_mode(mode),
    )
    _store_token(config, token)
    return token


def refresh_token(mode: object = "test") -> Dict[str, object]:
    config = load_order_config(config_file_for_mode(mode))
    _clear_token(config)
    token = _ensure_token(config, mode)
    return {"ok": True, "token_preview": token[:12] + "..."}


def _mode_for_config_file(config_file: Path) -> str:
    return "prod" if Path(config_file).resolve() == ORDER_CONFIG_PROD_FILE.resolve() else "test"


def _request_with_token_retry(
    config: Dict[str, object],
    mode: object,
    request: Callable[[str], Tuple[int, object]],
) -> Tuple[int, object]:
    token = _ensure_token(config, mode)
    try:
        return request(token)
    except broker.OrderBrokerError as error:
        if error.status_code not in {401, 403}:
            raise
        _clear_token(config)
        return request(_ensure_token(config, mode))


def _unwrap_task(payload: object) -> Optional[Dict[str, object]]:
    if not isinstance(payload, dict):
        return None
    node: object = payload.get("data") if "data" in payload else payload
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        inner = node["data"]
        if inner.get("task_id") or inner.get("status") or inner.get("order_no"):
            node = inner
    return node if isinstance(node, dict) else None


def _task_order_no(task: Dict[str, object]) -> str:
    direct = str(task.get("order_no") or "").strip()
    if direct:
        return direct
    params = task.get("params")
    if not isinstance(params, dict):
        return ""
    nested = str(params.get("order_no") or "").strip()
    if nested:
        return nested
    metadata = params.get("metadata")
    return (
        str(metadata.get("order_no") or "").strip()
        if isinstance(metadata, dict)
        else ""
    )


def list_stores(mode: object = "test") -> Dict[str, object]:
    config = load_order_config(config_file_for_mode(mode))
    token = _ensure_token(config, mode)
    try:
        status, data = broker.list_my_stores(str(config["server"]), token)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            status, data = broker.list_my_stores(str(config["server"]), token)
        else:
            raise
    payload = data.get("data") if isinstance(data, dict) else data
    stores = payload if isinstance(payload, list) else []
    return {"status": status, "stores": stores}


def create_order(payload: Dict[str, object]) -> Tuple[int, object, Dict[str, object]]:
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode(payload.get("mode"))
    try:
        dashboard_api.ensure_order_queue_capacity()
    except ValueError as error:
        raise OrderQueueConflict(str(error)) from error
    blocking = set(dashboard_api.active_order_blocking_keys())
    raw_items = payload.get("items")
    if blocking and isinstance(raw_items, list):
        conflicts = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            for key_name in ("item_id", "barcode", "code", "sku_code"):
                value = str(raw.get(key_name) or "").strip()
                if value and value in blocking:
                    conflicts.append(value)
                    break
        if conflicts:
            unique = ", ".join(dict.fromkeys(conflicts))
            raise ValueError(
                f"当前工单仍在处理中，请勿重复下单：{unique}"
            )
    config = load_order_config(config_file_for_mode(mode))
    body = build_create_task_body(config, payload.get("items"))
    token = _ensure_token(config, mode)
    try:
        status, data = broker.create_robot_task(str(config["server"]), token, body)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            status, data = broker.create_robot_task(str(config["server"]), token, body)
        else:
            raise
    return status, data, body


def create_registered_order(
    payload: Dict[str, object], source: str = "order"
) -> Tuple[int, object, Dict[str, object], Dict[str, object]]:
    """Serialize Broker creation and local queue registration."""
    from ksq.web import dashboard_api

    with _ORDER_CREATE_LOCK:
        status, data, body = create_order(payload)
        task_id = extract_task_id(data) or ""
        try:
            session = dashboard_api.register_created_order(task_id, body, source)
        except ValueError as error:
            # Capacity was checked while holding _ORDER_CREATE_LOCK; this only
            # protects against an unrelated manual dashboard overwrite.
            raise OrderQueueConflict(str(error)) from error
    return status, data, body, session


def get_task_detail(
    task_id: str, config_file: Optional[Path] = None
) -> Tuple[int, object]:
    from ksq.web import dashboard_api

    if config_file is not None:
        path = config_file
        mode = _mode_for_config_file(path)
    else:
        mode = dashboard_api.resolve_dashboard_mode("")
        path = config_file_for_mode(mode)
    config = load_order_config(path)
    token = _ensure_token(config, mode)
    try:
        return broker.get_robot_task(str(config["server"]), token, task_id)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            return broker.get_robot_task(str(config["server"]), token, task_id)
        raise


def clear_task_list_cache() -> None:
    with _TASK_LIST_CACHE_LOCK:
        _TASK_LIST_CACHE.clear()


def _task_list_payload(data: object) -> Dict[str, object]:
    payload: object = data.get("data") if isinstance(data, dict) else data
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload if isinstance(payload, dict) else {}


def _task_identity(task: object, index: int) -> str:
    if not isinstance(task, dict):
        return f"value:{index}:{task!r}"
    task_id = str(task.get("task_id") or "").strip()
    if task_id:
        return "task:" + task_id
    detail = task.get("task_detail")
    order_no = (
        str(detail.get("order_no") or "").strip()
        if isinstance(detail, dict)
        else ""
    )
    return "row:" + "|".join(
        (str(task.get("create_time") or ""), order_no, str(index))
    )


def _fetch_all_tasks(
    config: Dict[str, object],
    mode: str,
    store_id: str,
    order_by: str,
    status: str,
    timezone_name: str,
) -> List[object]:
    tasks: List[object] = []
    seen_tasks = set()
    seen_cursors = set()
    cursor = ""
    for batch_index in range(_TASK_LIST_MAX_BATCHES):
        _, data = _request_with_token_retry(
            config,
            mode,
            lambda token, current_cursor=cursor: broker.list_robot_tasks(
                str(config["server"]),
                token,
                store_id,
                _TASK_LIST_UPSTREAM_PAGE_SIZE,
                order_by,
                status,
                timezone_name,
                current_cursor,
            ),
        )
        task_payload = _task_list_payload(data)
        raw_tasks = task_payload.get("tasks")
        batch = raw_tasks if isinstance(raw_tasks, list) else []
        for index, task in enumerate(batch):
            identity = _task_identity(task, len(tasks) + index)
            if identity in seen_tasks:
                continue
            seen_tasks.add(identity)
            tasks.append(task)
        if not bool(task_payload.get("has_more")):
            return tasks
        next_cursor = str(task_payload.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            raise broker.OrderBrokerError(
                "Order Broker 任务列表分页游标无效。",
                502,
                {
                    "error": "invalid_task_cursor",
                    "next_cursor": next_cursor,
                    "batch": batch_index + 1,
                },
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise broker.OrderBrokerError(
        "Order Broker 任务列表超过最大分页次数。",
        502,
        {"error": "task_list_page_limit", "limit": _TASK_LIST_MAX_BATCHES},
    )


def list_tasks(
    mode: object,
    page: object = 1,
    page_size: object = 10,
    order_by: object = "desc",
    status: object = "",
    timezone_name: object = "Asia/Shanghai",
    refresh: object = False,
) -> Dict[str, object]:
    resolved_mode = str(mode or "test").strip().lower()
    if resolved_mode not in {"test", "prod"}:
        raise ValueError("mode 仅支持 test 或 prod。")
    try:
        page_value = int(page)
        page_size_value = int(page_size)
    except (TypeError, ValueError) as error:
        raise ValueError("page 和 page_size 必须是整数。") from error
    if page_value < 1:
        raise ValueError("page 必须大于等于 1。")
    if page_size_value < 1 or page_size_value > 50:
        raise ValueError("page_size 必须在 1 到 50 之间。")
    order_value = str(order_by or "desc").strip().lower()
    if order_value not in {"asc", "desc"}:
        raise ValueError("order_by 仅支持 asc 或 desc。")
    status_value = str(status or "").strip().lower()
    if status_value and status_value not in _KNOWN_TASK_STATUSES:
        raise ValueError("status 不是已知的任务状态。")
    timezone_value = str(timezone_name or "Asia/Shanghai").strip()
    if timezone_value != "Asia/Shanghai":
        raise ValueError("tz 仅支持 Asia/Shanghai。")

    config = load_order_config(config_file_for_mode(resolved_mode))
    validate_order_config(config)
    store_id = str(config.get("store_id") or "").strip()
    force_refresh = str(refresh or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cache_key = (
        resolved_mode,
        str(config.get("server") or "").strip(),
        str(config.get("client_id") or "").strip(),
        store_id,
        order_value,
        status_value,
        timezone_value,
    )
    now = time.monotonic()
    with _TASK_LIST_CACHE_LOCK:
        cached = _TASK_LIST_CACHE.get(cache_key)
    cache_hit = bool(
        not force_refresh
        and cached is not None
        and now - cached[0] <= _TASK_LIST_CACHE_SECONDS
    )
    if cache_hit and cached is not None:
        all_tasks = cached[1]
    else:
        all_tasks = _fetch_all_tasks(
            config,
            resolved_mode,
            store_id,
            order_value,
            status_value,
            timezone_value,
        )
        with _TASK_LIST_CACHE_LOCK:
            _TASK_LIST_CACHE[cache_key] = (time.monotonic(), all_tasks)
    total = len(all_tasks)
    total_pages = max(1, (total + page_size_value - 1) // page_size_value)
    page_value = min(page_value, total_pages)
    start = (page_value - 1) * page_size_value
    tasks = all_tasks[start : start + page_size_value]
    return {
        "mode": resolved_mode,
        "store_id": store_id,
        "page": page_value,
        "page_size": page_size_value,
        "order_by": order_value,
        "status": status_value,
        "tasks": tasks,
        "total": total,
        "total_pages": total_pages,
        "has_more": page_value < total_pages,
        "cached": cache_hit,
        "data_source": "broker",
    }


def _current_task_context() -> Tuple[Dict[str, object], str, Dict[str, object], str]:
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode("")
    if mode == "prod":
        raise ProductionOrderWriteForbidden("生产模式不允许修改工单。")
    active = dashboard_api.get_active_order()
    if not isinstance(active, dict):
        raise CurrentOrderConflict("当前没有活动工单。")
    task_id = str(active.get("task_id") or "").strip()
    if not task_id:
        raise CurrentOrderConflict("当前工单缺少 task_id。")
    config = load_order_config(config_file_for_mode(mode))
    _, data = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.get_robot_task(str(config["server"]), token, task_id),
    )
    task = _unwrap_task(data)
    if task is None:
        raise CurrentOrderConflict("当前工单的 Broker 详情格式无效。")
    returned_task_id = str(task.get("task_id") or task_id).strip()
    if returned_task_id != task_id:
        raise CurrentOrderConflict("Broker 返回的任务与当前工单不一致。")
    order_no = _task_order_no(task)
    if not order_no:
        raise CurrentOrderConflict("当前工单缺少 order_no。")
    return config, task_id, task, order_no


def operate_current_order(action: str, cancel_reason: object = "") -> Dict[str, object]:
    action_value = str(action or "").strip().lower()
    if action_value not in {"cancel", "manual_claim", "manual_complete"}:
        raise ValueError("不支持的当前工单操作。")
    config, task_id, task, order_no = _current_task_context()
    task_status = str(task.get("status") or "").strip().lower()
    if action_value == "cancel":
        if task_status not in _CANCELABLE_TASK_STATUSES:
            raise CurrentOrderConflict(
                f"当前工单状态 {task_status or '未知'} 不允许取消。"
            )
        reason = str(cancel_reason or "").strip()
        if not reason:
            raise ValueError("cancel_reason 不能为空。")
        status_code, data = _request_with_token_retry(
            config,
            "test",
            lambda token: broker.cancel_robot_task(
                str(config["server"]), token, task_id, "user", reason
            ),
        )
    elif action_value == "manual_claim":
        if task_status != "running":
            raise CurrentOrderConflict(
                f"当前工单状态 {task_status or '未知'} 不允许转人工。"
            )
        status_code, data = _request_with_token_retry(
            config,
            "test",
            lambda token: broker.manual_claim_order(
                str(config["server"]), token, order_no
            ),
        )
    else:
        if task_status not in _MANUAL_COMPLETABLE_STATUSES:
            raise CurrentOrderConflict(
                f"当前工单状态 {task_status or '未知'} 不允许人工完成，"
                f"需要 {'/'.join(sorted(_MANUAL_COMPLETABLE_STATUSES))}。"
            )
        status_code, data = _request_with_token_retry(
            config,
            "test",
            lambda token: broker.manual_complete_order(
                str(config["server"]), token, order_no
            ),
        )
    return {
        "ok": True,
        "action": action_value,
        "task_id": task_id,
        "order_no": order_no,
        "status": status_code,
        "data": data,
    }


def operate_task(
    action: str,
    task_id: str,
    cancel_reason: object = "",
    cancel_type: object = "user",
) -> Dict[str, object]:
    from ksq.web import dashboard_api

    action_value = str(action or "").strip().lower()
    if action_value not in {"cancel", "manual_claim", "manual_complete"}:
        raise ValueError("不支持的任务操作。")
    task_id_value = str(task_id or "").strip()
    if not task_id_value:
        raise ValueError("task_id 不能为空。")
    mode = dashboard_api.resolve_dashboard_mode("")
    if mode == "prod":
        raise ProductionOrderWriteForbidden("生产模式不允许修改工单。")
    config = load_order_config(config_file_for_mode(mode))
    _, detail = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.get_robot_task(
            str(config["server"]), token, task_id_value
        ),
    )
    task = _unwrap_task(detail)
    if task is None:
        raise TaskOperationConflict("任务的 Broker 详情格式无效。")
    returned_task_id = str(task.get("task_id") or task_id_value).strip()
    if returned_task_id != task_id_value:
        raise TaskOperationConflict("Broker 返回的任务与请求的 task_id 不一致。")
    task_status = str(task.get("status") or "").strip().lower()
    order_no = _task_order_no(task)
    if action_value == "cancel":
        if task_status not in _CANCELABLE_TASK_STATUSES:
            raise TaskOperationConflict(
                f"任务状态 {task_status or '未知'} 不允许取消。"
            )
        reason = str(cancel_reason or "").strip()
        if not reason:
            raise ValueError("cancel_reason 不能为空。")
        cancel_type_value = str(cancel_type or "").strip() or "user"
        status_code, data = _request_with_token_retry(
            config,
            mode,
            lambda token: broker.cancel_robot_task(
                str(config["server"]), token, task_id_value, cancel_type_value, reason
            ),
        )
    elif action_value == "manual_claim":
        if task_status != "running":
            raise TaskOperationConflict(
                f"任务状态 {task_status or '未知'} 不允许转人工。"
            )
        if not order_no:
            raise TaskOperationConflict("任务缺少 order_no。")
        status_code, data = _request_with_token_retry(
            config,
            mode,
            lambda token: broker.manual_claim_order(
                str(config["server"]), token, order_no
            ),
        )
    else:
        if task_status not in _MANUAL_COMPLETABLE_STATUSES:
            raise TaskOperationConflict(
                f"任务状态 {task_status or '未知'} 不允许人工完成，"
                f"需要 {'/'.join(sorted(_MANUAL_COMPLETABLE_STATUSES))}。"
            )
        if not order_no:
            raise TaskOperationConflict("任务缺少 order_no。")
        status_code, data = _request_with_token_retry(
            config,
            mode,
            lambda token: broker.manual_complete_order(
                str(config["server"]), token, order_no
            ),
        )
    clear_task_list_cache()
    result: Dict[str, object] = {
        "ok": True,
        "action": action_value,
        "task_id": task_id_value,
        "order_no": order_no,
        "status": status_code,
        "data": data,
    }
    active = dashboard_api.get_active_order()
    active_task_id = (
        str(active.get("task_id") or "").strip() if isinstance(active, dict) else ""
    )
    if active_task_id == task_id_value:
        # 仪表板活跃订单的生命周期由 get_dashboard_snapshot 轮询同步；
        # 此处仅回传队列状态供前端即时展示。
        result["queue"] = dashboard_api.order_queue_status()
    return result


_SENSITIVE_RESPONSE_KEYS = frozenset(
    {
        "authorization",
        "password",
        "clientsecret",
        "client_secret",
        "api_key",
        "apikey",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "token",
    }
)
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)((?:authorization|password|client[_-]?secret|access[_-]?token|"
    r"refresh[_-]?token|api[_-]?key|token)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\"'\s,;}\]]+)"
)
_BEARER_TEXT_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def sanitize_upstream(value: object) -> object:
    if isinstance(value, dict):
        cleaned: Dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            if normalized_key in _SENSITIVE_RESPONSE_KEYS:
                cleaned[key_text] = "***"
            else:
                cleaned[key_text] = sanitize_upstream(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_upstream(item) for item in value]
    if isinstance(value, str):
        redacted = _BEARER_TEXT_PATTERN.sub("Bearer ***", value)
        return _SENSITIVE_TEXT_PATTERN.sub(r"\1***", redacted)
    return value


def _find_response_value(value: object, keys: Tuple[str, ...]) -> object:
    if not isinstance(value, dict):
        return None
    lowered = {str(key).lower(): item for key, item in value.items()}
    for key in keys:
        found = lowered.get(key)
        if found not in (None, "", [], {}):
            return found
    for nested_key in ("data", "context"):
        nested = lowered.get(nested_key)
        if isinstance(nested, dict):
            found = _find_response_value(nested, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def broker_error_payload(error: broker.OrderBrokerError) -> Dict[str, object]:
    upstream = sanitize_upstream(error.body)
    summary = _find_response_value(
        upstream, ("msg", "message", "detail", "error", "error_message")
    )
    if isinstance(summary, dict):
        summary = _find_response_value(
            summary, ("msg", "message", "detail", "error_message")
        ) or json.dumps(summary, ensure_ascii=False)
    elif isinstance(summary, list):
        summary = json.dumps(summary, ensure_ascii=False)
    code = _find_response_value(upstream, ("code", "error_code"))
    request_id = _find_response_value(
        upstream, ("request_id", "trace_id", "requestid", "traceid")
    )
    return {
        "error": str(summary or error),
        "upstream_status": error.status_code,
        "upstream_code": code,
        "request_id": str(request_id or ""),
        "upstream": upstream,
    }


def extract_task_id(response_body: object) -> Optional[str]:
    if not isinstance(response_body, dict):
        return None
    data = response_body.get("data")
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    task_id = response_body.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        return task_id.strip()
    return None
