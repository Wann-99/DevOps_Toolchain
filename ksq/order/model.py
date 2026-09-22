"""Pure order normalization, status rules and time calculations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional
import re


_TIMER_STOP_REASONS = frozenset(
    {"human_prompt", "confirm", "broker_ended", "order_ended"}
)


_STATUS_LABELS = {
    "pending": "待处理",
    "idle": "空闲",
    "started": "开始处理商品",
    "processing": "处理中",
    "await_confirm": "人工确认",
    "await_error": "报错·请求人工处理",
    "success": "完成",
    "failed": "失败",
    "skipped": "未执行",
    "order_ended": "工单已结束",
    "order_closed": "工单已确认关闭",
}


_BROKER_STATUS_LABELS = {
    "pending": "等待中",
    "dispatched": "已拆单",
    "running": "运行中",
    "success": "完成",
    "error": "失败",
    "cancel": "已取消",
    "manual_cancel": "人工取消",
    "manual_canceled": "人工取消",
    "awaiting_pack": "等待打包",
    "manual_transferred_completed": "人工转单完成",
    "manual_transferred": "人工转单",
    "manual_claimed_in_progress": "人工处理中",
    "manual_claimed_completed": "人工处理完成",
}


# Broker 的人工流有两套命名：claimed_* 是实测在跑的那套，transferred_* 也在状态表里，
# 来源不明但一并认。持有态 = 机器人已交出订单、等人收尾，还差「标记完成」这一步。
_BROKER_MANUAL_HELD = frozenset({"manual_claimed_in_progress", "manual_transferred"})


_BROKER_MANUAL_DONE = frozenset(
    {"manual_claimed_completed", "manual_transferred_completed"}
)


# Broker statuses that mean the order has ended (or entered end-of-order human phase).
_BROKER_ORDER_ENDED = (
    frozenset({"success", "error", "cancel", "manual_cancel", "manual_canceled", "awaiting_pack"})
    | _BROKER_MANUAL_HELD
    | _BROKER_MANUAL_DONE
)


# 人工持有态不算 terminal：后面还有「标记完成」一步。
_BROKER_ORDER_TERMINAL = (
    frozenset({"success", "error", "cancel", "manual_cancel", "manual_canceled"})
    | _BROKER_MANUAL_DONE
)


def _parse_ts(raw: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    match = re.match(
        r"^(?P<head>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?(?P<tz>.*)$",
        value,
    )
    if match is not None:
        frac = (
            (match.group("frac") or "")[:6].ljust(6, "0")
            if match.group("frac")
            else ""
        )
        value = (
            match.group("head")
            + (("." + frac) if frac else "")
            + (match.group("tz") or "")
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ts_to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_seconds(
    start: Optional[datetime], end: Optional[datetime]
) -> Optional[float]:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _order_item_codes(order: Optional[Dict[str, object]]) -> set:
    codes: set = set()
    if order is None or not isinstance(order.get("items"), list):
        return codes
    for raw in order["items"]:  # type: ignore[index]
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or raw.get("barcode") or "").strip()
        if code:
            codes.add(code)
    return codes


def _infer_order_source(order_source: object, platform_order_no: object) -> str:
    value = str(order_source or "").strip().lower()
    if value:
        return value
    platform = str(platform_order_no or "").strip().upper()
    prefixes = (
        ("ELEM", "eleme"),
        ("MT", "meituan"),
        ("JD", "jd"),
        ("DY", "dy"),
        ("DSL", "dsl"),
    )
    for prefix, source in prefixes:
        if platform.startswith(prefix):
            return source
    return ""


def _normalize_order_quantity(raw: object, index: int) -> int:
    """Normalize quantities from HTTP and Broker recovery payloads."""
    if raw is None:
        return 1
    if isinstance(raw, bool):
        raise ValueError(f"items[{index}].quantity 必须是正整数。")
    if isinstance(raw, int):
        quantity = raw
    elif isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]*", raw.strip()):
        quantity = int(raw.strip())
    else:
        raise ValueError(f"items[{index}].quantity 必须是正整数。")
    if quantity <= 0:
        raise ValueError(f"items[{index}].quantity 必须是正整数。")
    return quantity


def _build_active_order(
    payload: Dict[str, object], *, registered_at: object = ""
) -> Dict[str, object]:
    task_id = str(payload.get("task_id") or "").strip()
    order_no = str(payload.get("order_no") or "").strip()
    platform_order_no = str(payload.get("platform_order_no") or "").strip()
    order_source = _infer_order_source(
        payload.get("order_source"), platform_order_no
    )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("items 必须是数组。")
    items: List[Dict[str, object]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}] 必须是对象。")
        barcode = str(
            raw.get("barcode") or raw.get("code") or raw.get("sku_code") or ""
        ).strip()
        sku_id = str(raw.get("sku_id") or "").strip()
        item_id = str(raw.get("item_id") or "").strip()
        code = barcode or item_id
        if not code:
            raise ValueError(f"items[{index}] 缺少 barcode/item_id。")
        items.append(
            {
                "index": index + 1,
                "code": code,
                "item_id": item_id or code,
                "barcode": barcode or code,
                "sku_id": sku_id,
                "name": str(
                    raw.get("name")
                    or raw.get("common_name")
                    or raw.get("药品名称")
                    or ""
                ).strip(),
                "location_code": str(raw.get("location_code") or "").strip(),
                "quantity": _normalize_order_quantity(raw.get("quantity"), index),
                "group_id": str(raw.get("group_id") or "").strip(),
                "group_field": str(raw.get("group_field") or "").strip(),
            }
        )
    registered_dt = _parse_ts(str(registered_at or ""))
    if registered_dt is None:
        registered_dt = datetime.now(timezone.utc)
    order = {
        "task_id": task_id,
        "order_no": order_no,
        "platform_order_no": platform_order_no,
        "order_source": order_source,
        "item_count": len(items),
        "items": items,
        "item_states": {},
        "registered_at": _ts_to_iso(registered_dt),
        "source": str(payload.get("source") or "order").strip() or "order",
        "lifecycle": _default_lifecycle(),
        "ui": {"dismissed_fingerprint": ""},
    }
    return order


def _order_has_pending_confirmation(order: Optional[Dict[str, object]]) -> bool:
    """Robot key waits survive Broker terminal states until logs show resume."""
    if not isinstance(order, dict):
        return False
    if order.get("pending_confirm"):
        return True
    lifecycle = order.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("needs_confirm"):
        return True
    states = order.get("item_states")
    return isinstance(states, dict) and any(
        isinstance(item, dict) and (
            item.get("needs_confirm")
            or item.get("status") in {"await_confirm", "await_error"}
        )
        for item in states.values()
    )


def _order_is_queue_terminal(order: Optional[Dict[str, object]]) -> bool:
    if not isinstance(order, dict):
        return False
    lifecycle = order.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return str(lifecycle.get("broker_status") or "").strip() in _BROKER_ORDER_TERMINAL


_TERMINAL_ITEM_STATUSES = frozenset({"success", "failed", "skipped"})


def _unwrap_broker_task(payload: object) -> Optional[Dict[str, object]]:
    if not isinstance(payload, dict):
        return None
    node: object = payload.get("data") if "data" in payload else payload
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        inner = node["data"]
        if inner.get("task_id") or inner.get("status") or inner.get("order_no"):
            node = inner
    if not isinstance(node, dict):
        return None
    return node


def _order_no_from_task_dict(task: Dict[str, object]) -> str:
    direct = str(task.get("order_no") or "").strip()
    if direct:
        return direct
    params = task.get("params")
    if isinstance(params, dict):
        nested = str(params.get("order_no") or "").strip()
        if nested:
            return nested
        meta = params.get("metadata")
        if isinstance(meta, dict):
            return str(meta.get("order_no") or "").strip()
    return ""


def _platform_order_no_from_task_dict(task: Dict[str, object]) -> str:
    direct = str(task.get("platform_order_no") or "").strip()
    if direct:
        return direct
    params = task.get("params")
    if isinstance(params, dict):
        nested = str(params.get("platform_order_no") or "").strip()
        if nested:
            return nested
        meta = params.get("metadata")
        if isinstance(meta, dict):
            return str(meta.get("platform_order_no") or "").strip()
    return ""


def _items_from_ob_params(params: object) -> List[Dict[str, object]]:
    if not isinstance(params, dict):
        return []
    raw_items = params.get("items")
    if not isinstance(raw_items, list):
        return []
    items: List[Dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        barcode = str(raw.get("barcode") or "").strip()
        item_id = str(raw.get("item_id") or "").strip()
        code = barcode or item_id
        if not code:
            continue
        try:
            quantity = int(raw.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1
        items.append(
            {
                "item_id": item_id or code,
                "barcode": barcode or code,
                "name": str(
                    raw.get("item_name") or raw.get("common_name") or raw.get("name") or ""
                ).strip(),
                "location_code": str(raw.get("location_code") or "").strip(),
                "quantity": quantity if quantity >= 1 else 1,
            }
        )
    return items


def _default_lifecycle() -> Dict[str, object]:
    return {
        "ended": False,
        "closed": False,
        "end_reason": "",
        "end_source": "",
        "ended_at": None,
        "closed_at": None,
        "timer_stopped_at": None,
        "frozen_elapsed_seconds": None,
        "timer_stop_reason": "",
        "broker_status": "",
        "broker_status_label": "",
        "label": "进行中",
    }
