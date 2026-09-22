"""Current and waiting orders: registration, capacity and confirmation."""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, List, Optional

from ksq.dashboard import cache as dashboard_cache
from ksq.order import model as order_model
from ksq.order import store as order_store


# ponytail: bounded local FIFO; Broker owns execution scheduling.
_ORDER_QUEUE_LIMIT = 10
_QUEUE_FULL_MESSAGE = (
    f"当前已有{_ORDER_QUEUE_LIMIT}单（当前单 1 单、等待单 {_ORDER_QUEUE_LIMIT - 1} 单），"
    "请等待当前单结束。"
)


def set_active_order(
    payload: Dict[str, object], *, registered_at: object = ""
) -> Dict[str, object]:
    order = order_model._build_active_order(payload, registered_at=registered_at)

    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        queued = _queued_orders_unlocked()
        for waiting in queued:
            if waiting.get("task_id") == order.get("task_id"):
                # ETM may identify the waiting task as current before the next poll.
                order = deepcopy(waiting)
                break
        remaining = [item for item in queued if item.get("task_id") != order.get("task_id")]
        if remaining:
            order["queued_orders"] = remaining
        order_store._ACTIVE_ORDER_LOADED = True
        order_store._ACTIVE_ORDER = order
        order_store._save_active_order_unlocked()
    dashboard_cache._invalidate_dashboard_snapshot_cache()
    return deepcopy(order)


def _queued_orders_unlocked() -> List[Dict[str, object]]:
    if not isinstance(order_store._ACTIVE_ORDER, dict):
        return []
    raw = order_store._ACTIVE_ORDER.get("queued_orders")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _promote_queued_order_unlocked() -> bool:
    """Advance only after Broker ends the order and the robot's key wait clears."""
    queued = _queued_orders_unlocked()
    if (
        not queued
        or not order_model._order_is_queue_terminal(order_store._ACTIVE_ORDER)
        or order_model._order_has_pending_confirmation(order_store._ACTIVE_ORDER)
    ):
        return False
    promoted = deepcopy(queued[0])
    if len(queued) > 1:
        promoted["queued_orders"] = deepcopy(queued[1:])
    else:
        promoted.pop("queued_orders", None)
    order_store._ACTIVE_ORDER = promoted
    order_store._save_active_order_unlocked()
    dashboard_cache._invalidate_dashboard_snapshot_cache()
    return True


def promote_queued_order_if_ready() -> bool:
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        return _promote_queued_order_unlocked()


def order_queue_status() -> Dict[str, object]:
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        current = order_store._ACTIVE_ORDER
        queued = _queued_orders_unlocked()
        return {
            "capacity": _ORDER_QUEUE_LIMIT,
            "total": (1 if isinstance(current, dict) else 0) + len(queued),
            "queued_count": len(queued),
            "full": len(queued) >= _ORDER_QUEUE_LIMIT - 1,
            "queued": [
                {key: item.get(key) for key in ("task_id", "order_no", "item_count", "registered_at")}
                for item in queued
            ],
        }


def ensure_order_queue_capacity() -> None:
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        _promote_queued_order_unlocked()
        if len(_queued_orders_unlocked()) >= _ORDER_QUEUE_LIMIT - 1:
            raise ValueError(_QUEUE_FULL_MESSAGE)


def register_created_order(
    task_id: str, request_body: Dict[str, object], source: str
) -> Dict[str, object]:
    """Register a Broker-created order without replacing an unfinished order."""
    items = request_body.get("items")
    if not isinstance(items, list):
        items = []
    payload = {
        "task_id": task_id,
        "order_no": str(request_body.get("order_no") or "").strip(),
        "platform_order_no": str(request_body.get("platform_order_no") or "").strip(),
        "order_source": str(request_body.get("order_source") or "").strip(),
        "items": items,
        "source": source,
    }
    candidate = order_model._build_active_order(payload)

    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        order_store._ACTIVE_ORDER_LOADED = True
        _promote_queued_order_unlocked()
        active_id = "" if order_store._ACTIVE_ORDER is None else str(order_store._ACTIVE_ORDER.get("task_id") or "")
        if active_id and active_id == task_id:
            result = deepcopy(order_store._ACTIVE_ORDER)
            result["queue_position"] = 0
            result["queued"] = False
            return result
        queued = _queued_orders_unlocked()
        for position, queued_order in enumerate(queued, 1):
            if str(queued_order.get("task_id") or "") == task_id:
                return dict(deepcopy(queued_order), queue_position=position, queued=True)
        if order_store._ACTIVE_ORDER is None or (
            order_model._order_is_queue_terminal(order_store._ACTIVE_ORDER)
            and not order_model._order_has_pending_confirmation(order_store._ACTIVE_ORDER)
            and not queued
        ):
            order_store._ACTIVE_ORDER = candidate
            position = 0
        else:
            if len(queued) >= _ORDER_QUEUE_LIMIT - 1:
                raise ValueError(_QUEUE_FULL_MESSAGE)
            queued.append(candidate)
            order_store._ACTIVE_ORDER["queued_orders"] = queued
            position = len(queued)
        order_store._save_active_order_unlocked()
    dashboard_cache._invalidate_dashboard_snapshot_cache()
    result = deepcopy(candidate)
    result["queue_position"] = position
    result["queued"] = position > 0
    return result


def set_active_order_from_create(
    task_id: str, request_body: Dict[str, object], source: str
) -> Dict[str, object]:
    items = request_body.get("items")
    if not isinstance(items, list):
        items = []
    return set_active_order(
        {
            "task_id": task_id,
            "order_no": str(request_body.get("order_no") or "").strip(),
            "platform_order_no": str(
                request_body.get("platform_order_no") or ""
            ).strip(),
            "order_source": str(request_body.get("order_source") or "").strip(),
            "items": items,
            "source": source,
        }
    )


def get_active_order() -> Optional[Dict[str, object]]:
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        if order_store._ACTIVE_ORDER is None:
            return None
        return deepcopy(order_store._ACTIVE_ORDER)


def dismiss_await(fingerprint: object) -> Dict[str, object]:
    """Shared across devices: mark confirm modal as dismissed for this await."""
    value = str(fingerprint or "").strip()
    if not value:
        raise ValueError("fingerprint 不能为空。")
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        if order_store._ACTIVE_ORDER is None:
            raise ValueError("当前没有活动工单。")
        ui = order_store._ACTIVE_ORDER.get("ui")
        if not isinstance(ui, dict):
            ui = {}
            order_store._ACTIVE_ORDER["ui"] = ui
        ui["dismissed_fingerprint"] = value
        order_store._save_active_order_unlocked()
    dashboard_cache._invalidate_dashboard_snapshot_cache()
    return {"ok": True, "dismissed_fingerprint": value}


def active_order_blocking_keys() -> List[str]:
    """SKU keys tied to current and waiting orders (multi-device guard)."""
    current = get_active_order()
    if current is None:
        return []
    orders = [current]
    queued = current.get("queued_orders")
    if isinstance(queued, list):
        orders.extend(item for item in queued if isinstance(item, dict))
    keys: List[str] = []
    seen: set = set()
    for order in orders:
        life = order.get("lifecycle")
        if isinstance(life, dict) and order_model._order_is_queue_terminal(order):
            continue
        states_raw = order.get("item_states")
        states: Dict[str, object] = states_raw if isinstance(states_raw, dict) else {}
        raw_items = order.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or raw.get("barcode") or "").strip()
            item_id = str(raw.get("item_id") or "").strip()
            state_key = code or item_id
            status = ""
            if state_key:
                item_state = states.get(state_key)
                if isinstance(item_state, dict):
                    status = str(item_state.get("status") or "").strip()
            if status in order_model._TERMINAL_ITEM_STATUSES:
                continue
            for value in (code, item_id, str(raw.get("barcode") or "").strip(), str(raw.get("sku_id") or "").strip()):
                if value and value not in seen:
                    seen.add(value)
                    keys.append(value)
    return keys


def active_order_requires_manual_completion() -> bool:
    """Whether a previous key prompt is still unresolved for the active order.

    保留单品待确认/报错和 Broker 人工持有态；不把日志结束标记当作完成。
    """
    order = get_active_order()
    if not isinstance(order, dict) or order_model._order_is_queue_terminal(order):
        return False
    lifecycle = order.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("broker_status") in order_model._BROKER_MANUAL_HELD:
        return True
    states = order.get("item_states")
    return bool(
        isinstance(states, dict)
        and any(
            isinstance(item, dict)
            and (
                bool(item.get("needs_confirm"))
                or str(item.get("status") or "") in {"await_confirm", "await_error"}
            )
            for item in states.values()
        )
    )


def clear_active_order() -> None:

    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ACTIVE_ORDER_LOADED = True
        order_store._ACTIVE_ORDER = None
        order_store._save_active_order_unlocked()
    dashboard_cache._invalidate_dashboard_snapshot_cache()
