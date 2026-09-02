"""Order Broker HTTP API helpers used by the web handler."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from ksq.constants import ORDER_CONFIG_FILE, ORDER_CONFIG_PROD_FILE
from ksq.order import broker
from ksq.order.config import (
    load_order_config,
    merge_config_update,
    public_order_config,
    save_order_config,
    validate_order_config,
)
from ksq.order.payload import build_create_task_body, generate_order_no
from ksq.runtime_logging import get_logger
from ksq.web import state


LOGGER = get_logger("order")


class CurrentOrderConflict(RuntimeError):
    """The requested action is not valid for the current active order."""


class TaskOperationConflict(RuntimeError):
    """The requested action is not valid for the referenced Broker task."""


class ProductionOrderWriteForbidden(RuntimeError):
    """Order mutations are intentionally disabled in production mode."""


class OrderQueueConflict(RuntimeError):
    """A local order state prevents creating another Broker task."""

    def __init__(self, message: str, code: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint

    def payload(self) -> Dict[str, str]:
        return {"error": str(self), "error_code": self.code, "hint": self.hint}


_KNOWN_TASK_STATUSES = frozenset(
    {
        "pending",
        "dispatched",
        "running",
        "success",
        "error",
        "cancel",
        "manual_cancel",
        "manual_canceled",
        "awaiting_pack",
        "manual_claimed_in_progress",
        "manual_claimed_completed",
        "manual_transferred",
        "manual_transferred_completed",
    }
)
# Broker 的 /api/robot-tasks 只接受这几个值作为 status 过滤条件；manual_* 只会出现在
# 任务返回里，作为过滤条件会被上游拒绝（status must be one of: ...），需改在本地过滤。
_BROKER_FILTER_STATUSES = frozenset(
    {
        "pending",
        "dispatched",
        "running",
        "success",
        "error",
        "cancel",
        "awaiting_pack",
    }
)
# 取消/转人工/人工完成不做本地状态门槛（对齐 devtools：直发，由 Broker 裁决）。
# The dashboard polls the list periodically; a long cache makes Broker status
# appear stuck until the user clicks "刷新列表"。
# TTL 必须显著大于前端 ORDER_LIST_POLL_MS，否则每次轮询都撞上刚过期的缓存，
# 缓存形同不存在：任务达 690 条时拉全量需 14 次跨公网往返（4~7s）。
_TASK_LIST_CACHE_SECONDS = 20.0
_TASK_LIST_UPSTREAM_PAGE_SIZE = 50
_TASK_LIST_MAX_BATCHES = 1000
_TASK_LIST_CACHE_LOCK = threading.Lock()
_TASK_LIST_CACHE: Dict[Tuple[str, ...], Tuple[float, List[object]]] = {}
# 正在后台刷新的 cache_key，同一 key 同时只跑一个（单飞）。
_TASK_LIST_REFRESHING: Set[Tuple[str, ...]] = set()
_ORDER_CREATE_LOCK = threading.Lock()


def config_file_for_mode(mode: object) -> Path:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return ORDER_CONFIG_PROD_FILE
    if value not in {"", "test"}:
        raise ValueError("mode 仅支持 test 或 prod。")
    return ORDER_CONFIG_FILE


def get_public_config(
    mode: object = "test", include_secret: bool = False
) -> Dict[str, object]:
    config_file = config_file_for_mode(mode)
    config = load_order_config(config_file)
    payload = public_order_config(config, include_secret=include_secret)
    payload["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    payload["config_file"] = str(config_file)
    payload["token_ready"] = _cached_token(config) is not None
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
                if not _token_expired(cached):
                    return cached
                tokens.pop(key, None)
        # Backward compatible single-slot cache for the default config path.
        if state.order_access_token and key == getattr(
            state, "order_access_token_key", ""
        ):
            if not _token_expired(state.order_access_token):
                return state.order_access_token
            state.order_access_token = None
            state.order_access_token_key = ""
    return None


def _token_expired(token: str) -> bool:
    """Read JWT exp for UI/cache validity; opaque tokens expire on Broker rejection."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        expires_at = float(decoded["exp"])
    except (
        binascii.Error,
        IndexError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
    return expires_at <= time.time()


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
    # 本函数只由 PUT /api/order/config 调用，该路由已强制管理员会话，
    # 回显带上 secret 以便页面保存后仍能看到当前值。
    result = public_order_config(saved, include_secret=True)
    result["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    result["config_file"] = str(config_file)
    result["token_ready"] = False
    return result


def _token_auth_mode(mode: object) -> str:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return "user_login"
    return "client"


def _ensure_token(
    config: Dict[str, object], mode: object = "test", *, require_store: bool = True
) -> str:
    validate_order_config(config, require_store=require_store)
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
    return {"ok": True, "token_preview": token[:12] + "...", "token_ready": True}


def _mode_for_config_file(config_file: Path) -> str:
    return "prod" if Path(config_file).resolve() == ORDER_CONFIG_PROD_FILE.resolve() else "test"


def _request_with_token_retry(
    config: Dict[str, object],
    mode: object,
    request: Callable[[str], Tuple[int, object]],
) -> Tuple[int, object]:
    token = _ensure_token(config, mode)
    token_retried = False
    while True:
        try:
            result = request(token)
        except broker.OrderBrokerError as error:
            if error.status_code not in {401, 403} or token_retried:
                raise
            token_retried = True
            _clear_token(config)
            token = _ensure_token(config, mode)
            continue
        if _business_code(result[1]) == 4014 and not token_retried:
            token_retried = True
            _clear_token(config)
            token = _ensure_token(config, mode)
            continue
        return result


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
    # 门店列表本身用于选择 store_id，不应被下单字段 store_id 反向阻塞。
    token = _ensure_token(config, mode, require_store=False)
    try:
        status, data = broker.list_my_stores(str(config["server"]), token)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode, require_store=False)
            status, data = broker.list_my_stores(str(config["server"]), token)
        else:
            raise
    _ensure_broker_response_succeeded("查询门店", status, data)
    payload = data.get("data") if isinstance(data, dict) else data
    stores = payload if isinstance(payload, list) else []
    return {"status": status, "stores": stores, "token_ready": True}


def _local_request_body(
    body: Dict[str, object], raw_items: object
) -> Dict[str, object]:
    """Keep UI grouping metadata locally without sending it to Broker."""
    if not isinstance(raw_items, list) or not isinstance(body.get("items"), list):
        return body
    local_body = dict(body)
    local_items = [
        dict(item)
        for item in body["items"]  # type: ignore[index]
        if isinstance(item, dict)
    ]
    for item, raw in zip(local_items, raw_items):
        if not isinstance(raw, dict):
            continue
        for key in ("sku_id", "group_id", "group_field"):
            value = str(raw.get(key) or "").strip()
            if value:
                item[key] = value
    local_body["items"] = local_items
    return local_body


# Broker 业务错误码（响应体 code 非 0，HTTP 可能是 200）处理指引。
_BROKER_CODE_HINTS: Dict[int, str] = {
    4014: "Token 已失效：请重新下单，系统会自动刷新 Token 后重试一次。",
    4511: "该门店没有注册机器人：请先把机器人注册到对应门店。",
    4524: "该门店未开启系统接单：请先在门店侧打开接单开关。",
    4552: "商品未注册或库位不存在：请用 SKU 库位管理工具确认该商品与库位已注册。",
    4601: "订单号重复：系统已自动更换单号重试一次，若仍失败请再次下单。",
    4800: "门店并发任务数超限：请到「仪表板 → 门店任务列表」取消等待中/运行中/人工转单的任务后再下单。"
}


def _business_code(data: object) -> int:
    """响应体里的业务错误码（code 非 0）；无则 0。"""
    if not isinstance(data, dict):
        return 0
    raw_code = data.get("code")
    if raw_code is None:
        return 0
    if isinstance(raw_code, str):
        raw_code = raw_code.strip()
        if not raw_code or raw_code == "0":
            return 0
    elif isinstance(raw_code, bool):
        return 1 if raw_code else 0
    elif isinstance(raw_code, int):
        return raw_code
    elif isinstance(raw_code, float):
        if not math.isfinite(raw_code) or not raw_code.is_integer():
            return -1
        return int(raw_code)
    else:
        return -1
    if isinstance(raw_code, str):
        digits = raw_code.lstrip("+-")
        if not digits or not digits.isdigit():
            return -1
        return int(raw_code)
    # A malformed non-empty business code is still a failed envelope;
    # accepting it as success can make callers process empty data.
    return -1


def _ensure_task_action_succeeded(
    action: str, status: int, data: object
) -> None:
    labels = {
        "cancel": "取消任务",
        "manual_claim": "转人工",
        "manual_complete": "完成人工处理",
        "update": "更新任务",
        "get_business_config": "查询门店业务配置",
        "update_business_config": "更新门店业务配置",
    }
    _ensure_broker_response_succeeded(labels.get(action, "任务操作"), status, data)


def _ensure_broker_response_succeeded(
    operation: str, status: int, data: object
) -> None:
    """Reject HTTP failures and HTTP-200 responses with a nonzero code."""
    code = _business_code(data)
    if not code and 200 <= status < 300:
        return
    message = ""
    if isinstance(data, dict):
        message = str(data.get("msg") or data.get("message") or "").strip()
    detail = (
        f"{message or 'Broker 拒绝操作'}（code={code}）"
        if code
        else f"HTTP {status}"
    )
    raise broker.OrderBrokerError(
        f"{operation}失败：{detail}",
        status,
        data,
    )


def ensure_order_creation_allowed() -> None:
    from ksq.web import dashboard_api

    if dashboard_api.active_order_requires_manual_completion():
        raise OrderQueueConflict(
            "上一单仍在等待人工确认，当前机器人流程可能已无法继续。",
            "PREVIOUS_ORDER_REQUIRES_COMPLETION",
            "请先到「仪表板 → 门店任务列表」完成上一单，再重新下单。",
        )
    if dashboard_api.active_order_blocks_new_order():
        raise OrderQueueConflict(
            "上一单尚未完成，当前不能下单，请等待上一单完成。",
            "PREVIOUS_ORDER_IN_PROGRESS",
            "请等待仪表板中的上一单完成后再下单。",
        )


def create_order(payload: Dict[str, object]) -> Tuple[int, object, Dict[str, object]]:
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode(payload.get("mode"))
    ensure_order_creation_allowed()
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
            for key_name in ("sku_id", "item_id", "barcode", "code", "sku_code"):
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
    token_retried = False
    duplicate_retried = False
    while True:
        try:
            status, data = broker.create_robot_task(str(config["server"]), token, body)
        except broker.OrderBrokerError as error:
            # HTTP 401/403 与业务码 4014（client token is invalid）都刷新凭据重试一次
            if (
                error.status_code in {401, 403} or _business_code(error.body) == 4014
            ) and not token_retried:
                token_retried = True
                _clear_token(config)
                token = _ensure_token(config, mode)
                continue
            raise
        code = _business_code(data)
        if not code:
            return status, data, _local_request_body(body, raw_items)
        # HTTP 200 但业务码非 0：按指引自动重试一次，仍不行才抛给上层
        if code == 4014 and not token_retried:
            token_retried = True
            _clear_token(config)
            token = _ensure_token(config, mode)
            continue
        if code == 4601 and not duplicate_retried:
            duplicate_retried = True
            body = dict(body, order_no=generate_order_no())
            continue
        message = ""
        if isinstance(data, dict):
            message = str(data.get("msg") or data.get("message") or "").strip()
        raise broker.OrderBrokerError(
            f"Broker 拒绝下单：{message or '未知错误'}（code={code}）",
            status,
            data,
        )


def create_registered_order(
    payload: Dict[str, object], source: str = "order"
) -> Tuple[int, object, Dict[str, object], Dict[str, object]]:
    """Serialize Broker creation and local queue registration."""
    from ksq.web import dashboard_api

    with _ORDER_CREATE_LOCK:
        status, data, body = create_order(payload)
        task_id = extract_task_id(data) or ""
        if not task_id:
            raise broker.OrderBrokerError(
                "Broker 响应缺少 task_id，下单结果无法注册。", status, data
            )
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
        result = broker.get_robot_task(str(config["server"]), token, task_id)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            result = broker.get_robot_task(str(config["server"]), token, task_id)
        else:
            raise
    _ensure_broker_response_succeeded("查询任务详情", result[0], result[1])
    return result


def clear_task_list_cache() -> None:
    with _TASK_LIST_CACHE_LOCK:
        _TASK_LIST_CACHE.clear()
        _TASK_LIST_REFRESHING.clear()


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
        # 不能再用 status 接返回值：它是本函数的过滤参数，且被下方 lambda 按引用
        # 闭包捕获；一旦被覆盖成 HTTP 状态码，第二页就会把 200 当过滤值发给 Broker。
        http_status, data = _request_with_token_retry(
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
        _ensure_broker_response_succeeded("查询任务列表", http_status, data)
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


def _store_task_list(
    cache_key: Tuple[str, ...],
    config: Dict[str, object],
    mode: str,
    store_id: str,
    order_by: str,
    upstream_status: str,
    timezone_name: str,
) -> List[object]:
    """拉全量任务并写入缓存，返回新数据。"""
    tasks = _fetch_all_tasks(
        config, mode, store_id, order_by, upstream_status, timezone_name
    )
    with _TASK_LIST_CACHE_LOCK:
        _TASK_LIST_CACHE[cache_key] = (time.monotonic(), tasks)
    return tasks


def _start_background_task_list_refresh(
    cache_key: Tuple[str, ...],
    config: Dict[str, object],
    mode: str,
    store_id: str,
    order_by: str,
    upstream_status: str,
    timezone_name: str,
) -> bool:
    """后台刷新已过期的缓存，不阻塞前端。

    拉全量需 14 次跨公网往返（4~7s），让前端等它就会把分页/刷新按钮卡死，
    所以过期时先返回旧值，刷新交给后台。失败只记日志并保留旧缓存。
    """
    with _TASK_LIST_CACHE_LOCK:
        if cache_key in _TASK_LIST_REFRESHING:
            return False
        _TASK_LIST_REFRESHING.add(cache_key)

    def worker() -> None:
        try:
            _store_task_list(
                cache_key,
                config,
                mode,
                store_id,
                order_by,
                upstream_status,
                timezone_name,
            )
        except Exception:
            LOGGER.warning("后台刷新任务列表失败，保留旧缓存", exc_info=True)
        finally:
            with _TASK_LIST_CACHE_LOCK:
                _TASK_LIST_REFRESHING.discard(cache_key)

    # daemon：服务退出时不留悬挂线程。
    threading.Thread(
        target=worker, name="ksq-task-list-refresh", daemon=True
    ).start()
    return True


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
    # 上游不支持的过滤值（manual_*）不往上发，改为拉全量后本地筛
    upstream_status = (
        status_value if status_value in _BROKER_FILTER_STATUSES else ""
    )

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
        upstream_status,
        timezone_value,
    )
    now = time.monotonic()
    with _TASK_LIST_CACHE_LOCK:
        cached = _TASK_LIST_CACHE.get(cache_key)
    fresh = bool(
        cached is not None and now - cached[0] <= _TASK_LIST_CACHE_SECONDS
    )
    stale = False
    if force_refresh or cached is None:
        # 手动刷新或完全无缓存：只能同步等一次
        cache_hit = False
        all_tasks = _store_task_list(
            cache_key,
            config,
            resolved_mode,
            store_id,
            order_value,
            upstream_status,
            timezone_value,
        )
    elif fresh:
        cache_hit = True
        all_tasks = cached[1]
    else:
        # 过期：先返回旧值，刷新交给后台，前端不等 4~7s
        cache_hit = True
        stale = True
        all_tasks = cached[1]
        _start_background_task_list_refresh(
            cache_key,
            config,
            resolved_mode,
            store_id,
            order_value,
            upstream_status,
            timezone_value,
        )
    # 订单状态直出 Broker 原值，不做任何本地覆盖。
    display_tasks: List[object] = list(all_tasks)
    if status_value and not upstream_status:
        display_tasks = [
            task
            for task in display_tasks
            if isinstance(task, dict)
            and str(task.get("status") or "").strip().lower() == status_value
        ]
    total = len(display_tasks)
    total_pages = max(1, (total + page_size_value - 1) // page_size_value)
    page_value = min(page_value, total_pages)
    start = (page_value - 1) * page_size_value
    tasks = display_tasks[start : start + page_size_value]
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
        "stale": stale,
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
    if action_value == "cancel":
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
        status_code, data = _request_with_token_retry(
            config,
            "test",
            lambda token: broker.manual_claim_order(
                str(config["server"]), token, order_no
            ),
        )
    else:
        status_code, data = _request_with_token_retry(
            config,
            "test",
            lambda token: broker.manual_complete_order(
                str(config["server"]), token, order_no
            ),
        )
    _ensure_task_action_succeeded(action_value, status_code, data)
    clear_task_list_cache()
    from ksq.web import dashboard_api

    dashboard_api.invalidate_broker_order_cache(task_id)
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
    order_no = _task_order_no(task)
    if action_value == "cancel":
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
        if not order_no:
            raise TaskOperationConflict("任务缺少 order_no。")
        status_code, data = _request_with_token_retry(
            config,
            mode,
            lambda token: broker.manual_complete_order(
                str(config["server"]), token, order_no
            ),
        )
    _ensure_task_action_succeeded(action_value, status_code, data)
    clear_task_list_cache()
    dashboard_api.invalidate_broker_order_cache(task_id_value)
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
        # 此处保留订单状态摘要供前端即时刷新。
        result["queue"] = dashboard_api.order_queue_status()
    return result


def _test_mode_write_config() -> Tuple[Dict[str, object], str]:
    """订单/门店写操作仅测试模式开放；返回已校验的配置与模式。"""
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode("")
    if mode == "prod":
        raise ProductionOrderWriteForbidden("生产模式不允许修改工单。")
    config = load_order_config(config_file_for_mode(mode))
    validate_order_config(config)
    return config, mode


def update_task_retail_order(
    task_id: str,
    retail_order_id: object = "",
    retail_order_time: object = "",
) -> Dict[str, object]:
    """PUT /api/robot-tasks/{task_id}：下发零售单号 / 零售单时间（仅测试模式）。"""
    task_id_value = str(task_id or "").strip()
    if not task_id_value:
        raise ValueError("task_id 不能为空。")
    fields: Dict[str, object] = {}
    order_id = str(retail_order_id or "").strip()
    order_time = str(retail_order_time or "").strip()
    if order_id:
        fields["retail_order_id"] = order_id
    if order_time:
        fields["retail_order_time"] = order_time
    if not fields:
        raise ValueError("retail_order_id 和 retail_order_time 至少填写一项。")
    config, mode = _test_mode_write_config()
    status, data = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.update_robot_task(
            str(config["server"]), token, task_id_value, fields
        ),
    )
    _ensure_task_action_succeeded("update", status, data)
    clear_task_list_cache()
    from ksq.web import dashboard_api

    dashboard_api.invalidate_broker_order_cache(task_id_value)
    return {
        "ok": True,
        "task_id": task_id_value,
        "fields": fields,
        "status": status,
        "data": data,
    }


def operate_order_action(action: str, order_no: str) -> Dict[str, object]:
    """按订单号直发人工转单/人工完成（对齐 devtools：无本地状态门槛）。"""
    action_value = str(action or "").strip().lower()
    if action_value not in {"manual_claim", "manual_complete"}:
        raise ValueError("不支持的订单操作。")
    order_no_value = str(order_no or "").strip()
    if not order_no_value:
        raise ValueError("order_no 不能为空。")
    config, mode = _test_mode_write_config()
    status, data = _request_with_token_retry(
        config,
        mode,
        lambda token: (
            broker.manual_claim_order(str(config["server"]), token, order_no_value)
            if action_value == "manual_claim"
            else broker.manual_complete_order(
                str(config["server"]), token, order_no_value
            )
        ),
    )
    _ensure_task_action_succeeded(action_value, status, data)
    clear_task_list_cache()
    from ksq.web import dashboard_api

    dashboard_api.invalidate_broker_order_cache("")
    return {
        "ok": True,
        "action": action_value,
        "order_no": order_no_value,
        "status": status,
        "data": data,
    }


def _resolve_store_id(config: Dict[str, object], store_id: object) -> str:
    store = str(store_id or "").strip() or str(
        config.get("store_id") or ""
    ).strip()
    if not store:
        raise ValueError("store_id 不能为空。")
    return store


def list_business_modes() -> Dict[str, object]:
    """GET /api/business-modes：可选业务模式列表（只读，双模式可用）。"""
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode("")
    config = load_order_config(config_file_for_mode(mode))
    validate_order_config(config)
    status, data = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.list_business_modes(str(config["server"]), token),
    )
    _ensure_broker_response_succeeded("查询业务模式", status, data)
    payload = data.get("data") if isinstance(data, dict) else data
    modes = payload if isinstance(payload, list) else []
    return {"ok": True, "status": status, "modes": modes}


def get_business_config(store_id: object = "") -> Dict[str, object]:
    """GET /api/retail-stores/{store_id}/business-config（只读，双模式可用）。"""
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode("")
    config = load_order_config(config_file_for_mode(mode))
    validate_order_config(config)
    store = _resolve_store_id(config, store_id)
    status, data = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.get_business_config(
            str(config["server"]), token, store
        ),
    )
    _ensure_task_action_succeeded("get_business_config", status, data)
    return {
        "ok": True,
        "store_id": store,
        "status": status,
        "data": data,
    }


def update_business_config(
    store_id: object = "",
    business_mode_code: object = "",
    is_accepting_orders: object = None,
) -> Dict[str, object]:
    """PUT /api/retail-stores/{store_id}/business-config（仅测试模式）。

    Broker 硬性校验：business_mode_code 必须在激活模式列表（4551），且响应
    模型 is_accepting_orders 必填（5001）。未修改的字段先 GET 当前配置回填，
    PUT 请求体两个字段永远带全。
    """
    config, mode = _test_mode_write_config()
    store = _resolve_store_id(config, store_id)
    mode_code = str(business_mode_code or "").strip()
    accepting = is_accepting_orders if isinstance(is_accepting_orders, bool) else None
    if not mode_code and accepting is None:
        raise ValueError("business_mode_code 和 is_accepting_orders 至少修改一项。")
    if not mode_code or accepting is None:
        _, current = _request_with_token_retry(
            config,
            mode,
            lambda token: broker.get_business_config(
                str(config["server"]), token, store
            ),
        )
        current_data = current.get("data") if isinstance(current, dict) else current
        if isinstance(current_data, dict):
            if not mode_code:
                mode_code = str(
                    current_data.get("business_mode_code") or ""
                ).strip()
            if accepting is None and isinstance(
                current_data.get("is_accepting_orders"), bool
            ):
                accepting = current_data["is_accepting_orders"]  # type: ignore[index]
    if not mode_code:
        raise ValueError("该门店当前未配置业务模式，请显式填写 business_mode_code。")
    if accepting is None:
        raise ValueError("未能获取门店当前接单状态，请显式传入 is_accepting_orders。")
    body = {"business_mode_code": mode_code, "is_accepting_orders": accepting}
    status, data = _request_with_token_retry(
        config,
        mode,
        lambda token: broker.update_business_config(
            str(config["server"]), token, store, body
        ),
    )
    _ensure_task_action_succeeded("update_business_config", status, data)
    return {
        "ok": True,
        "store_id": store,
        "fields": body,
        "status": status,
        "data": data,
    }


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
    hint = ""
    try:
        hint = _BROKER_CODE_HINTS.get(int(code), "") if code not in (None, "") else ""
    except (TypeError, ValueError):
        hint = ""
    return {
        "error": str(summary or error),
        "upstream_status": error.status_code,
        "upstream_code": code,
        "request_id": str(request_id or ""),
        "hint": hint,
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
