"""Dashboard snapshots, confirmation recovery, and background monitoring."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote
import json
import time
import urllib.error
import urllib.request

from ksq.constants import DEFAULT_ETM_BASE_URL
from ksq.dashboard import cache as dashboard_cache
from ksq.dashboard import parsing as log_parser
from ksq.dashboard import settings as dashboard_settings
from ksq.feishu.submit import maybe_submit_feishu_form, preview_feishu_form, should_submit_on_confirm, should_submit_on_human_prompt
from ksq.order import active as active_orders
from ksq.order import model as order_model
from ksq.order import service as order_api
from ksq.order import store as order_store
from ksq.robot import keyboard as keyboard
from ksq.robot.logs import LogServiceError, fetch_logs, inspect_container
from ksq.runtime_logging import get_logger


_RECOVERED_ORDER_TIMEZONE = timezone(timedelta(hours=8))


_FEISHU_LOG_CACHE_MAX_LINES = 20000


LOGGER = get_logger("dashboard")


_BROKER_TASK_IDENTITY_CACHE_TTL_SECONDS = 3.0


_BROKER_TASK_IDENTITY_CACHE_LOCK = Lock()


_BROKER_TASK_IDENTITY_CACHE: Dict[str, Tuple[float, str]] = {}


_DASHBOARD_MONITOR_TAIL = 2500


_DASHBOARD_MONITOR_ACTIVE_SECONDS = 1.0


_DASHBOARD_MONITOR_IDLE_SECONDS = 4.0


_DASHBOARD_REFRESH_LOCK = Lock()


_DASHBOARD_MONITOR_THREAD: Optional[Thread] = None


_DASHBOARD_MONITOR_STOP: Optional[Event] = None


def _dismissed_fingerprint_from_order(order: Optional[Dict[str, object]]) -> str:
    if not isinstance(order, dict):
        return ""
    ui = order.get("ui")
    if not isinstance(ui, dict):
        return ""
    return str(ui.get("dismissed_fingerprint") or "").strip()


def _build_order_from_log_task(
    task_id: str, codes: List[str]
) -> Dict[str, object]:
    items: List[Dict[str, object]] = []
    for index, code in enumerate(codes):
        value = str(code or "").strip()
        if not value:
            continue
        items.append(
            {
                "index": index + 1,
                "code": value,
                "item_id": value,
                "barcode": value,
                "name": "",
                "location_code": "",
                "quantity": 1,
            }
        )
    return active_orders.set_active_order(
        {
            "task_id": task_id,
            "order_no": "",
            "platform_order_no": "",
            "items": items,
            "source": "log",
        }
    )


def _expand_order_items_from_log(
    order: Optional[Dict[str, object]],
    codes_by_task: Dict[str, List[str]],
    focus_task_id: str,
) -> Optional[Dict[str, object]]:
    """Append newly seen SKUs under the same robot task into the current order."""
    if order is None:
        return None
    focus_task_id = str(focus_task_id or "").strip()
    log_codes = list(codes_by_task.get(focus_task_id, [])) if focus_task_id else []
    if not log_codes:
        return order
    existing = order_model._order_item_codes(order)
    items: List[Dict[str, object]] = []
    if isinstance(order.get("items"), list):
        items = [deepcopy(raw) for raw in order["items"] if isinstance(raw, dict)]  # type: ignore[index]
    changed = False
    for code in log_codes:
        value = str(code or "").strip()
        if not value or value in existing:
            continue
        items.append(
            {
                "index": len(items) + 1,
                "code": value,
                "item_id": value,
                "barcode": value,
                "name": "",
                "location_code": "",
                "quantity": 1,
            }
        )
        existing.add(value)
        changed = True
    if not changed:
        return order
    updated = deepcopy(order)
    updated["items"] = items
    updated["item_count"] = len(items)
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is not None and str(order_store._ACTIVE_ORDER.get("task_id") or "") == str(
            order.get("task_id") or ""
        ):
            order_store._ACTIVE_ORDER["items"] = deepcopy(items)
            order_store._ACTIVE_ORDER["item_count"] = len(items)
    return updated


def _persist_code_aliases(order: Optional[Dict[str, object]], aliases: Dict[str, str]) -> None:
    """把别名写回当前工单持久化，滚出日志窗口后仍可用。"""
    if not isinstance(order, dict) or not aliases:
        return
    task_id = str(order.get("task_id") or "").strip()
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is None:
            return
        if task_id and str(order_store._ACTIVE_ORDER.get("task_id") or "") != task_id:
            return
        existing = order_store._ACTIVE_ORDER.get("code_aliases")
        if isinstance(existing, dict) and all(
            existing.get(key) == value for key, value in aliases.items()
        ):
            return
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(aliases)
        order_store._ACTIVE_ORDER["code_aliases"] = merged
        order_store._save_active_order_unlocked()


def _resolve_active_order(
    order: Optional[Dict[str, object]], raw_logs: str
) -> Optional[Dict[str, object]]:
    """
    Keep showing the current order until the next order starts, then refresh.
    Same task_id may gain more SKUs over time — those are expanded separately.
    """
    latest_task_id, codes_by_task, last_seen_by_task = log_parser._discover_log_tasks(raw_logs)
    if order is None:
        if not latest_task_id or not log_parser._has_recent_log_activity(last_seen_by_task):
            return None
        return _build_order_from_log_task(
            latest_task_id, codes_by_task.get(latest_task_id, [])
        )

    active_task_id = str(order.get("task_id") or "").strip()
    lifecycle = order.get("lifecycle") if isinstance(order.get("lifecycle"), dict) else {}
    source = str(order.get("source") or "").strip()
    ended = bool(lifecycle.get("ended") or lifecycle.get("closed"))
    matched = log_parser._match_log_task_for_order(
        order, codes_by_task, latest_task_id, last_seen_by_task
    )

    if latest_task_id and active_task_id and latest_task_id != active_task_id:
        # Same order barcodes may continue under a remapped robot task_id.
        if matched == latest_task_id:
            order = _expand_order_items_from_log(order, codes_by_task, matched)
            return order
        # Broker-created orders are authoritative.  The task_id in robot logs
        # is an execution-internal id and must never replace the Broker id.
        # Log-only orders may still follow the latest log task as before.
        if source == "log" and not (
            str(order.get("order_no") or "").strip()
            or str(order.get("platform_order_no") or "").strip()
        ):
            return _build_order_from_log_task(
                latest_task_id, codes_by_task.get(latest_task_id, [])
            )
        return order

    if latest_task_id and not active_task_id:
        return _build_order_from_log_task(
            latest_task_id, codes_by_task.get(latest_task_id, [])
        )
    if matched:
        order = _expand_order_items_from_log(order, codes_by_task, matched)
    return order


def _list_task_identity(task: object) -> Tuple[str, str]:
    """Read order identity from either a Broker list row or its task_detail."""
    nodes: List[Dict[str, object]] = []
    if isinstance(task, dict):
        nodes.append(task)
        detail = task.get("task_detail")
        if isinstance(detail, dict):
            nodes.append(detail)
    order_no = ""
    platform_no = ""
    for node in nodes:
        order_no = order_no or order_model._order_no_from_task_dict(node)
        platform_no = platform_no or order_model._platform_order_no_from_task_dict(node)
    return order_no, platform_no


def _recovered_order_registered_at(task: object) -> str:
    """Use the upstream order time as the stale-log boundary after recovery."""
    if not isinstance(task, dict):
        return ""
    detail = task.get("task_detail")
    nodes = [task]
    if isinstance(detail, dict):
        nodes.append(detail)
    for node in list(nodes):
        params = node.get("params")
        if isinstance(params, dict):
            nodes.append(params)
    for node in nodes:
        for field in ("create_time", "order_time"):
            parsed = order_model._parse_ts(str(node.get(field) or ""))
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                # Broker list times are explicitly requested in Asia/Shanghai.
                parsed = parsed.replace(tzinfo=_RECOVERED_ORDER_TIMEZONE)
            return str(order_model._ts_to_iso(parsed) or "")
    return ""


def _reconcile_broker_task_id(
    order: Optional[Dict[str, object]], mode: str
) -> Optional[Dict[str, object]]:
    """Replace a log-internal task id with the Broker id for the same order.

    Robot logs have their own execution task id.  When an order was first
    discovered from logs, the Broker id can only be recovered by matching the
    stable order_no/platform_order_no in the Broker task list.
    """
    # Older deployments could persist a robot-internal task_id even for an
    # order-created record.  The stable order_no is authoritative for every
    # source, so repair that legacy state instead of limiting this to log-only.
    if not isinstance(order, dict):
        return order
    order_no = str(order.get("order_no") or "").strip()
    platform_no = str(order.get("platform_order_no") or "").strip()
    current_id = str(order.get("task_id") or "").strip()
    if not order_no or not current_id:
        return order
    cache_key = "|".join((str(mode or "test"), order_no, platform_no))
    now = time.monotonic()
    with _BROKER_TASK_IDENTITY_CACHE_LOCK:
        cached = _BROKER_TASK_IDENTITY_CACHE.get(cache_key)
        if cached is not None and now - cached[0] <= _BROKER_TASK_IDENTITY_CACHE_TTL_SECONDS:
            broker_id = cached[1]
        else:
            broker_id = ""
    if cached is None or now - cached[0] > _BROKER_TASK_IDENTITY_CACHE_TTL_SECONDS:
        try:
            from ksq.order.broker import OrderBrokerError

            result = order_api.list_tasks(
                mode=mode,
                page=1,
                page_size=50,
                order_by="desc",
                status="",
                timezone_name="Asia/Shanghai",
                refresh=False,
            )
            candidates: List[Tuple[int, str]] = []
            for task in result.get("tasks", []) if isinstance(result, dict) else []:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("task_id") or "").strip()
                if not task_id:
                    continue
                row_order_no, row_platform_no = _list_task_identity(task)
                if row_order_no != order_no:
                    continue
                if platform_no and row_platform_no and row_platform_no != platform_no:
                    continue
                score = 1 + (2 if platform_no and row_platform_no == platform_no else 0)
                if task_id == current_id:
                    score += 10
                candidates.append((score, task_id))
            broker_id = max(candidates, default=(0, ""))[1]
        except (OrderBrokerError, ValueError, FileNotFoundError, KeyError, TypeError, OSError):
            broker_id = ""
        with _BROKER_TASK_IDENTITY_CACHE_LOCK:
            _BROKER_TASK_IDENTITY_CACHE[cache_key] = (time.monotonic(), broker_id)
    if not broker_id or broker_id == current_id:
        return order

    updated = deepcopy(order)
    updated["task_id"] = broker_id
    updated["robot_task_id"] = current_id
    updated["source"] = "order"
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is not None:
            active_id = str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
            active_order_no = str(order_store._ACTIVE_ORDER.get("order_no") or "").strip()
            if active_id == current_id or active_order_no == order_no:
                order_store._ACTIVE_ORDER["task_id"] = broker_id
                order_store._ACTIVE_ORDER["robot_task_id"] = current_id
                order_store._ACTIVE_ORDER["source"] = "order"
                order_store._save_active_order_unlocked()
                updated = deepcopy(order_store._ACTIVE_ORDER)
    return updated


def _promote_latest_broker_order(order: Optional[Dict[str, object]], mode: str) -> Optional[Dict[str, object]]:
    """Recover a newer running Broker order when the persisted active order is terminal.

    A network/process restart can happen after Broker creates an order but
    before the local active-order file is updated.  In that case the UI keeps
    pointing at the old completed order and blocks the new one.  Read-only
    Broker list data is enough to repair the single-active-order pointer.
    """
    if mode != "test":
        return order
    if isinstance(order, dict) and (
        order.get("queued_orders") or order_model._order_has_pending_confirmation(order)
    ):
        # Registered waiting orders advance after lifecycle reconciliation;
        # recovering from the newest list row would lose their local metadata.
        return order
    current_id = str(order.get("task_id") or "").strip() if isinstance(order, dict) else ""
    current_broker: Dict[str, object] = {}
    if current_id:
        current_broker = order_api._fetch_broker_order(current_id, mode)
        if current_broker.get("ok") and not current_broker.get("terminal"):
            return order
        if (
            not current_broker.get("ok")
            and str(order.get("source") or "").strip() != "log"
        ):
            return order
    try:

        result = order_api.list_tasks(
            mode=mode,
            page=1,
            page_size=50,
            order_by="desc",
            status="",
            timezone_name="Asia/Shanghai",
            refresh=False,
        )
    except Exception:
        return order
    tasks = result.get("tasks", []) if isinstance(result, dict) else []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        status = str(task.get("status") or "").strip().lower()
        if not task_id or task_id == current_id or status not in (
            {"pending", "dispatched", "running", "awaiting_pack"}
            | order_model._BROKER_MANUAL_HELD
        ):
            continue
        promoted_order_no, promoted_platform_no = _list_task_identity(task)
        detail = task.get("task_detail") if isinstance(task.get("task_detail"), dict) else {}
        raw_items = detail.get("items") if isinstance(detail, dict) else None
        if not isinstance(raw_items, list):
            raw_items = task.get("items") if isinstance(task.get("items"), list) else []
        items: List[Dict[str, object]] = []
        for index, raw in enumerate(raw_items, 1):
            if not isinstance(raw, dict):
                continue
            barcode = str(raw.get("barcode") or "").strip()
            item_id = str(raw.get("item_id") or "").strip()
            code = barcode or item_id
            if not code:
                continue
            items.append(
                {
                    "index": index,
                    "code": code,
                    "item_id": item_id or code,
                    "barcode": barcode or code,
                    "name": str(raw.get("item_name") or raw.get("name") or "").strip(),
                    "location_code": str(raw.get("location_code") or "").strip(),
                    "quantity": raw.get("quantity") or 1,
                }
            )
        candidate = order_model._build_active_order(
            {
                "task_id": task_id,
                "order_no": promoted_order_no,
                "platform_order_no": promoted_platform_no,
                "items": items,
                "source": "order",
            },
            registered_at=_recovered_order_registered_at(task),
        )
        with order_store._ACTIVE_ORDER_LOCK:

            active_id = (
                ""
                if order_store._ACTIVE_ORDER is None
                else str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
            )
            # A create request may replace the active order while Broker list is
            # in flight. Never let that older refresh overwrite the new order.
            if active_id != current_id:
                return deepcopy(order_store._ACTIVE_ORDER) if order_store._ACTIVE_ORDER is not None else None
            order_store._ACTIVE_ORDER = candidate
            order_store._save_active_order_unlocked()
        return candidate
    return order


def _etm_get_json(base_url: str, path: str) -> Dict[str, object]:
    url = f"{base_url.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise LogServiceError(
            f"ETM 请求失败：GET {url} → HTTP {error.code} {body[:160]}",
            502,
        ) from error
    except urllib.error.URLError as error:
        raise LogServiceError(
            f"无法连接 ETM：{url}（{error.reason}）",
            503,
        ) from error
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LogServiceError(f"ETM 响应不是 JSON：{url}", 502) from error
    if not isinstance(payload, dict):
        raise LogServiceError(f"ETM 响应格式无效：{url}", 502)
    return payload


def _broker_item_location(entry: Dict[str, object]) -> str:
    """从 Broker 条目里取库位：优先顶层 location_code，缺失时从 locations[0] 还原。"""
    value = str(entry.get("location_code") or "").strip()
    if value:
        return value
    locations = entry.get("locations")
    if not isinstance(locations, list):
        return ""
    for location in locations:
        if not isinstance(location, dict):
            continue
        customer = str(location.get("customer_location_code") or "").strip()
        if customer:
            return customer
        parts = [
            str(location.get(key) or "").strip()
            for key in ("shelf_number", "level", "bin_unit")
        ]
        if all(parts):
            return "".join(parts)
    return ""


def _broker_order_items(broker: Dict[str, object]) -> List[Dict[str, object]]:
    """提取 Broker 带回的子任务条目（下单时提交、由 Broker 原样返回）。

    单任务详情放在 params.items，列表行放在 task_detail.items，两者结构一致。
    这里是名称与库位的权威来源：日志只能给出条码与状态，而本地下单快照的
    名称/库位来自导入 CSV，CSV 往往没有这两列，取本地值会得到空。
    """
    if not broker.get("ok"):
        return []
    raw = broker.get("raw")
    if not isinstance(raw, dict):
        return []
    container: Optional[List[object]] = None
    for key in ("params", "task_detail"):
        node = raw.get(key)
        if isinstance(node, dict) and isinstance(node.get("items"), list):
            container = node["items"]  # type: ignore[assignment]
            break
    if container is None and isinstance(raw.get("items"), list):
        container = raw["items"]  # type: ignore[assignment]
    if not container:
        return []
    items: List[Dict[str, object]] = []
    for index, entry in enumerate(container, start=1):
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "index": index,
                "item_id": str(entry.get("item_id") or "").strip(),
                "sku_id": str(entry.get("sku_id") or "").strip(),
                "barcode": str(entry.get("barcode") or "").strip(),
                "name": str(
                    entry.get("item_name")
                    or entry.get("common_name")
                    or entry.get("alias")
                    or ""
                ).strip(),
                "location_code": _broker_item_location(entry),
                "quantity": entry.get("quantity") or 1,
            }
        )
    return items


# 日志/本地条目与 Broker 条目对齐时可用的标识，任一命中即视为同一个商品。
_ITEM_MATCH_FIELDS = ("item_id", "sku_id", "barcode", "code")


def _broker_item_index(
    broker_items: List[Dict[str, object]]
) -> Dict[str, Dict[str, object]]:
    index: Dict[str, Dict[str, object]] = {}
    for item in broker_items:
        for key in ("item_id", "sku_id", "barcode"):
            value = str(item.get(key) or "").strip()
            if value:
                index.setdefault(value, item)
    return index


def _match_broker_item(
    task: Dict[str, object], index: Dict[str, Dict[str, object]]
) -> Optional[Dict[str, object]]:
    for key in _ITEM_MATCH_FIELDS:
        value = str(task.get(key) or "").strip()
        if value and value in index:
            return index[value]
    return None


def _apply_broker_item_details(
    tasks: List[Dict[str, object]], broker: Dict[str, object]
) -> None:
    """用 Broker 带回的条目信息覆盖子任务的标识与展示字段。

    只在 Broker 给出非空值时覆盖，不会把已有数据抹成空。状态与计时仍由
    日志解析提供，两边按 item_id / sku_id / 条码 任一命中对齐。

    barcode / sku_id / item_id 也按 Broker 为准覆盖：日志的 item 行编号可能是
    sku_id，而上游的 barcode/item_id 字段又会回退成那个编号，导致「69码」里装的
    是 sku_id。Broker 条目同时带真正的 barcode 和 sku_id，是这三个字段的权威来源，
    比依赖日志里 sku_id->69码 别名的出现时序可靠。
    """
    index = _broker_item_index(_broker_order_items(broker))
    if not index:
        return
    for task in tasks:
        matched = _match_broker_item(task, index)
        if matched is None:
            continue
        for field in ("name", "location_code", "barcode", "sku_id", "item_id"):
            value = str(matched.get(field) or "").strip()
            if value:
                task[field] = value
        task["broker_matched"] = True


def _broker_from_etm_cloud(cloud: Dict[str, object], task_id: str) -> Dict[str, object]:
    broker_status = str(cloud.get("status") or "").strip()
    return {
        "ok": True,
        "http_status": 200,
        "task_id": str(cloud.get("task_id") or task_id).strip(),
        "order_no": order_model._order_no_from_task_dict(cloud),
        "platform_order_no": order_model._platform_order_no_from_task_dict(cloud),
        "status": broker_status,
        "status_label": order_model._BROKER_STATUS_LABELS.get(
            broker_status, broker_status or "未知"
        ),
        "ended": broker_status in order_model._BROKER_ORDER_ENDED,
        "terminal": broker_status in order_model._BROKER_ORDER_TERMINAL,
        "create_time": str(cloud.get("create_time") or ""),
        "raw": cloud,
        "source": "etm",
    }


def _sync_order_from_etm(
    order: Optional[Dict[str, object]], settings: Dict[str, object]
) -> Tuple[Optional[Dict[str, object]], Dict[str, object]]:
    """Production mode: prefer Edge Task Manager next/cloud for order identity."""
    base = dashboard_settings._normalize_etm_base_url(settings.get("etm_base_url"))
    etm: Dict[str, object] = {
        "ok": False,
        "reachable": False,
        "base_url": base,
        "next_task_id": "",
        "cloud_ok": False,
        "error": "",
    }
    try:
        next_body = _etm_get_json(base, "/api/v1/tasks/next")
    except LogServiceError as error:
        etm["error"] = str(error)
        return order, etm
    etm["reachable"] = True
    next_data = next_body.get("data")
    next_task_id = ""
    if isinstance(next_data, dict):
        next_task_id = str(next_data.get("task_id") or "").strip()
        etm["next_task_id"] = next_task_id
    if order_model._order_has_pending_confirmation(order):
        next_task_id = ""
    candidate = next_task_id
    if not candidate and order is not None:
        candidate = str(order.get("task_id") or "").strip()
    if not candidate:
        etm["ok"] = True
        return order, etm
    try:
        cloud_body = _etm_get_json(
            base, f"/api/v1/tasks/cloud/{quote(candidate, safe='')}"
        )
    except LogServiceError as error:
        etm["error"] = str(error)
        etm["ok"] = True
        return order, etm
    cloud = cloud_body.get("data")
    if not isinstance(cloud, dict):
        etm["ok"] = True
        etm["error"] = "ETM cloud 无任务详情"
        return order, etm
    etm["cloud_ok"] = True
    etm["ok"] = True
    etm["status"] = str(cloud.get("status") or "").strip()
    order_no = order_model._order_no_from_task_dict(cloud)
    platform_no = order_model._platform_order_no_from_task_dict(cloud)
    items = order_model._items_from_ob_params(cloud.get("params"))
    current_id = "" if order is None else str(order.get("task_id") or "").strip()
    if next_task_id and next_task_id != current_id:
        order = active_orders.set_active_order(
            {
                "task_id": next_task_id,
                "order_no": order_no,
                "platform_order_no": platform_no,
                "items": items,
                "source": "etm",
            },
            registered_at=_recovered_order_registered_at(cloud),
        )
        return order, etm
    if order is None:
        order = active_orders.set_active_order(
            {
                "task_id": candidate,
                "order_no": order_no,
                "platform_order_no": platform_no,
                "items": items,
                "source": "etm",
            },
            registered_at=_recovered_order_registered_at(cloud),
        )
        return order, etm
    # Enrich current session without wiping remembered item_states.
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is not None:
            if order_no and not order_store._ACTIVE_ORDER.get("order_no"):
                order_store._ACTIVE_ORDER["order_no"] = order_no
            if platform_no and not order_store._ACTIVE_ORDER.get("platform_order_no"):
                order_store._ACTIVE_ORDER["platform_order_no"] = platform_no
            if items:
                existing = order_model._order_item_codes(order_store._ACTIVE_ORDER)
                merged_items = list(order_store._ACTIVE_ORDER.get("items") or [])  # type: ignore[arg-type]
                if not isinstance(merged_items, list):
                    merged_items = []
                for item in items:
                    code = str(item.get("barcode") or item.get("item_id") or "").strip()
                    if not code or code in existing:
                        # Refresh name/location when we only had barcode stubs.
                        for row in merged_items:
                            if not isinstance(row, dict):
                                continue
                            row_code = str(
                                row.get("code") or row.get("barcode") or ""
                            ).strip()
                            if row_code != code:
                                continue
                            if item.get("name") and not row.get("name"):
                                row["name"] = item.get("name")
                            if item.get("location_code") and not row.get(
                                "location_code"
                            ):
                                row["location_code"] = item.get("location_code")
                        continue
                    merged_items.append(
                        {
                            "index": len(merged_items) + 1,
                            "code": code,
                            "item_id": item.get("item_id") or code,
                            "barcode": item.get("barcode") or code,
                            "name": item.get("name") or "",
                            "location_code": item.get("location_code") or "",
                            "quantity": item.get("quantity") or 1,
                        }
                    )
                    existing.add(code)
                order_store._ACTIVE_ORDER["items"] = merged_items
                order_store._ACTIVE_ORDER["item_count"] = len(merged_items)
            order = deepcopy(order_store._ACTIVE_ORDER)
    return order, etm


_STATUS_RANK = {
    "pending": 0,
    "skipped": 0,
    "started": 1,
    "processing": 2,
    "await_confirm": 3,
    "await_error": 3,
    "success": 4,
    "failed": 4,
}


def _status_rank(status: object) -> int:
    return _STATUS_RANK.get(str(status or "pending"), 0)


def _merge_item_state(
    remembered: Optional[Dict[str, object]], fresh: Dict[str, object]
) -> Dict[str, object]:
    """Keep the stronger state so completed items are not lost when logs roll off."""
    if remembered is None:
        return deepcopy(fresh)
    remembered_parent = str(remembered.get("parent_task_id") or "").strip()
    fresh_parent = str(fresh.get("parent_task_id") or "").strip()
    if (
        remembered_parent
        and fresh_parent
        and remembered_parent != fresh_parent
        and str(remembered.get("status") or "") in {"success", "failed"}
    ):
        # Same SKU executed again under a different task (re-order): the fresh
        # state is a new execution and must override the remembered terminal
        # state of the previous order.
        return deepcopy(fresh)
    merged = deepcopy(remembered)
    fresh_status = str(fresh.get("status") or "pending")
    old_status = str(merged.get("status") or "pending")
    if _status_rank(fresh_status) > _status_rank(old_status):
        merged = deepcopy(fresh)
    elif _status_rank(fresh_status) == _status_rank(old_status):
        # Same tier: refresh with newer timing / lines when present.
        for key in (
            "seq_id",
            "parent_task_id",
            "location_code",
            "started_at",
            "await_at",
            "ended_at",
            "elapsed_to_await_seconds",
            "elapsed_seconds",
            "duration_seconds",
            "await_kind",
            "await_line",
            "start_line",
            "end_line",
            "status_label",
            "active",
            "needs_confirm",
        ):
            value = fresh.get(key)
            if value not in (None, "", []):
                merged[key] = value
        fresh_events = fresh.get("events")
        if isinstance(fresh_events, list) and fresh_events:
            merged["events"] = deepcopy(fresh_events)
    else:
        # Fresh is weaker (e.g. pending after log rolled off): keep remembered,
        # but still refresh identity fields if missing.
        for key in ("seq_id", "parent_task_id", "location_code"):
            if not merged.get(key) and fresh.get(key):
                merged[key] = fresh.get(key)
    # Never let a terminal remembered state be downgraded.
    if old_status in {"success", "failed"} and fresh_status in {
        "pending",
        "skipped",
        "started",
        "processing",
    }:
        merged["status"] = old_status
        merged["status_label"] = order_model._STATUS_LABELS.get(old_status, old_status)
        merged["active"] = False
        merged["needs_confirm"] = False
        if remembered.get("duration_seconds") is not None:
            merged["duration_seconds"] = remembered.get("duration_seconds")
        if remembered.get("ended_at"):
            merged["ended_at"] = remembered.get("ended_at")
        if remembered.get("end_line"):
            merged["end_line"] = remembered.get("end_line")
    merged["code"] = fresh.get("code") or merged.get("code") or ""
    return merged


def _remembered_states(order: Optional[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    if order is None:
        return {}
    raw = order.get("item_states")
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, Dict[str, object]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            result[str(key)] = value
    return result


def _persist_item_states(order: Optional[Dict[str, object]], tasks: List[Dict[str, object]]) -> None:
    if order is None:
        return
    task_id = str(order.get("task_id") or "").strip()
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is None:
            return
        active_id = str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
        if task_id and active_id and active_id != task_id:
            return
        memory = order_store._ACTIVE_ORDER.setdefault("item_states", {})
        state_changed = False
        if not isinstance(memory, dict):
            memory = {}
            order_store._ACTIVE_ORDER["item_states"] = memory
            state_changed = True
        for task in tasks:
            code = str(task.get("code") or "").strip()
            if not code:
                continue
            previous = memory.get(code) if isinstance(memory.get(code), dict) else None
            merged = _merge_item_state(previous, task)
            if previous != merged:
                if previous is not None:
                    previous_stable = {
                        key: value
                        for key, value in previous.items()
                        if key != "elapsed_seconds"
                    }
                    merged_stable = {
                        key: value
                        for key, value in merged.items()
                        if key != "elapsed_seconds"
                    }
                    if previous_stable == merged_stable:
                        continue
                memory[code] = merged
                state_changed = True
        # Keep item list aligned with observed tasks.
        existing = {
            str(raw.get("code") or raw.get("barcode") or "").strip()
            for raw in (order_store._ACTIVE_ORDER.get("items") or [])
            if isinstance(raw, dict)
        }
        # 日志的 item 行编号可能是 sku_id（见 _ITEM_START_RE 处注释），而已有条目是按
        # 69码 建的。code_aliases 是订单已记录的 sku_id -> 69码，先翻译再判重，
        # 否则同一药品会以两种标识各存一条，虚增子任务数、让进度永远差一格。
        # 注意：69码 与 sku_id 是两个不同字段，这里只做翻译，不把两者归入同一集合。
        raw_aliases = order_store._ACTIVE_ORDER.get("code_aliases")
        aliases = raw_aliases if isinstance(raw_aliases, dict) else {}
        items = list(order_store._ACTIVE_ORDER.get("items") or [])
        if not isinstance(items, list):
            items = []
        items_changed = False
        for task in tasks:
            code = str(task.get("code") or "").strip()
            if not code:
                continue
            # code 命中别名表的键 ⇒ 它是 sku_id，对应值才是 69码。
            sku_id = str(task.get("sku_id") or "").strip()
            barcode_of_code = str(aliases.get(code) or "").strip()
            if barcode_of_code:
                sku_id = sku_id or code
            resolved_code = barcode_of_code or code
            if resolved_code in existing:
                continue
            entry = {
                "index": len(items) + 1,
                "code": resolved_code,
                "item_id": task.get("item_id") or resolved_code,
                "barcode": task.get("barcode") or resolved_code,
                "name": task.get("name") or "",
                "location_code": task.get("location_code") or "",
                "quantity": task.get("quantity") or 1,
            }
            if sku_id:
                entry["sku_id"] = sku_id
            items.append(entry)
            existing.add(resolved_code)
            items_changed = True
        if items_changed:
            order_store._ACTIVE_ORDER["items"] = items
            order_store._ACTIVE_ORDER["item_count"] = len(items)
        if state_changed or items_changed:
            order_store._save_active_order_unlocked()


def _merge_feishu_log_cache_lines(
    previous: object, raw_logs: object, max_lines: int = _FEISHU_LOG_CACHE_MAX_LINES
) -> List[str]:
    """Accumulate log lines seen during polling without duplicating the tail."""
    lines = [str(line) for line in previous if str(line).strip()] if isinstance(previous, list) else []
    known = set(lines)
    for line in str(raw_logs or "").splitlines():
        value = str(line).strip()
        if value and value not in known:
            lines.append(value)
            known.add(value)
    return lines[-max_lines:]


def _persist_feishu_log_cache(
    order: Optional[Dict[str, object]], raw_logs: object
) -> None:
    """Keep the polling-time log window for the final Feishu document build."""
    if order is None:
        return
    task_id = str(order.get("task_id") or "").strip()
    if not task_id:
        return
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is None:
            return
        active_id = str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
        if active_id and active_id != task_id:
            return
        old_cache = order_store._ACTIVE_ORDER.get("feishu_log_cache")
        old_lines = old_cache.get("lines") if isinstance(old_cache, dict) else []
        lines = _merge_feishu_log_cache_lines(old_lines, raw_logs)
        if isinstance(old_cache, dict) and lines == old_lines:
            return
        order_store._ACTIVE_ORDER["feishu_log_cache"] = {
            "task_id": task_id,
            "lines": lines,
            "updated_at": order_model._ts_to_iso(datetime.now(timezone.utc)),
        }
        order_store._save_active_order_unlocked()


def _public_order(order: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Hide internal listener/Feishu state from dashboard responses."""
    if order is None:
        return None
    result = deepcopy(order)
    result.pop("feishu_log_cache", None)
    result.pop("pending_confirm", None)
    result.pop("queued_orders", None)
    return result


def _prune_stale_item_states(
    order: Optional[Dict[str, object]], stale_task_ids: frozenset
) -> None:
    """Drop remembered item states produced by a previous order's task.

    Re-ordered SKUs share codes with the previous order; states remembered
    from that stale task would otherwise pin the new order's items to the old
    terminal states forever (the never-downgrade merge rule).
    """
    if order is None or not stale_task_ids:
        return
    task_id = str(order.get("task_id") or "").strip()
    with order_store._ACTIVE_ORDER_LOCK:

        if order_store._ACTIVE_ORDER is None:
            return
        active_id = str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
        if task_id and active_id and active_id != task_id:
            return
        memory = order_store._ACTIVE_ORDER.get("item_states")
        if not isinstance(memory, dict):
            return
        stale_keys = [
            key
            for key, value in memory.items()
            if isinstance(value, dict)
            and str(value.get("parent_task_id") or "").strip() in stale_task_ids
        ]
        if not stale_keys:
            return
        for key in stale_keys:
            memory.pop(key, None)
        order_store._save_active_order_unlocked()


def _apply_order_lifecycle(
    order: Optional[Dict[str, object]],
    parsed: Dict[str, object],
    broker: Dict[str, object],
    tasks: List[Dict[str, object]],
) -> Tuple[Dict[str, object], List[Dict[str, object]], str]:
    lifecycle = order_model._default_lifecycle()
    if order is not None and isinstance(order.get("lifecycle"), dict):
        lifecycle.update(order["lifecycle"])  # type: ignore[arg-type]

    human_kind = str(parsed.get("human_confirm_kind") or "")
    order_await = bool(parsed.get("order_await_active"))
    broker_status = str(broker.get("status") or "").strip()
    if broker.get("ok"):
        lifecycle["broker_status"] = broker_status
        lifecycle["broker_status_label"] = str(
            broker.get("status_label") or broker_status
        )

    # Live robot motion only (keeps order timer ticking). Await states freeze.
    has_robot_running = any(
        str(task.get("status") or "") in {"started", "processing"} for task in tasks
    )
    has_live_work = any(
        str(task.get("status") or "")
        in {"started", "processing", "await_confirm", "await_error"}
        for task in tasks
    )
    statuses = [str(task.get("status") or "pending") for task in tasks]
    has_failure = any(status == "failed" for status in statuses)
    unfinished = has_live_work or any(
        str(task.get("status") or "") == "pending" for task in tasks
    )
    broker_terminal = bool(broker.get("ok") and broker.get("terminal"))
    broker_ended = bool(broker.get("ok") and broker.get("ended"))
    item_needs_confirm = any(bool(task.get("needs_confirm")) for task in tasks)
    needs_confirm = item_needs_confirm or order_await
    # Persist the key wait with Broker status, before a concurrent create can advance.
    lifecycle["needs_confirm"] = needs_confirm
    has_error_wait = bool(
        any(status == "await_error" for status in statuses)
        or (needs_confirm and human_kind == "error")
        or (order_await and human_kind == "error")
    )
    # Failure with no in-flight SKU: robot usually stopped; remaining pending
    # will not run unless a later start line reopens the order.
    stopped_on_failure = bool(
        has_failure and not has_live_work and not needs_confirm
    )

    # 订单 lifecycle（ended / closed / end_reason）只由 Broker 状态决定；
    # 日志解析信号仅服务下方的展示层：label、aggregate、计时冻结与单品标记。
    now_iso = order_model._ts_to_iso(datetime.now(timezone.utc))
    now_dt = datetime.now(timezone.utc)

    # Broker 订单状态是 lifecycle 的唯一来源：结束 = Broker 终态/人工持有态。
    # manual_transferred is an operator-owned waiting state, not a terminal state:
    # keep it as the current order until manual-complete reaches the Broker.
    if broker_ended:
        lifecycle["ended"] = True
        lifecycle["end_source"] = "broker"
        if broker_status == "error":
            lifecycle["end_reason"] = "broker_error"
        elif broker_status in {"cancel", "manual_cancel", "manual_canceled"}:
            lifecycle["end_reason"] = "broker_cancel"
        elif broker_status == "awaiting_pack":
            lifecycle["end_reason"] = "broker_awaiting_pack"
        elif broker_status in order_model._BROKER_MANUAL_DONE:
            lifecycle["end_reason"] = "broker_manual_completed"
        elif broker_status in order_model._BROKER_MANUAL_HELD:
            lifecycle["end_reason"] = "broker_transferred"
        elif broker_status == "success":
            lifecycle["end_reason"] = "broker_success"
        if not lifecycle.get("ended_at"):
            lifecycle["ended_at"] = now_iso

    # Broker terminal is authoritative: ended + closed 的唯一来源。
    if broker_terminal:
        lifecycle["ended"] = True
        lifecycle["closed"] = True
        if not lifecycle.get("closed_at"):
            lifecycle["closed_at"] = now_iso

    # Order timer: first 开始处理 → first *active* human-gate speak
    # (needs_confirm). Do not freeze on historical human_seen from old logs,
    # and never latch 0s before this order has started.
    first_started_dt: Optional[datetime] = None
    for task in tasks:
        started_dt = order_model._parse_ts(str(task.get("started_at") or ""))
        if started_dt is None:
            continue
        if first_started_dt is None or started_dt < first_started_dt:
            first_started_dt = started_dt

    already_frozen = (
        str(lifecycle.get("timer_stop_reason") or "") in order_model._TIMER_STOP_REASONS
        and lifecycle.get("frozen_elapsed_seconds") is not None
    )
    if already_frozen:
        stop_dt = order_model._parse_ts(str(lifecycle.get("timer_stopped_at") or ""))
        try:
            frozen_value = float(lifecycle.get("frozen_elapsed_seconds"))
        except (TypeError, ValueError):
            frozen_value = 0.0
        stale_before_start = (
            first_started_dt is not None
            and stop_dt is not None
            and stop_dt < first_started_dt
        )
        invalid_zero = (
            frozen_value <= 0.0
            and first_started_dt is not None
            and (has_robot_running or not needs_confirm)
        )
        if stale_before_start or invalid_zero:
            lifecycle["timer_stopped_at"] = None
            lifecycle["frozen_elapsed_seconds"] = None
            lifecycle["timer_stop_reason"] = ""
            already_frozen = False

    # Drop incomplete freeze markers when no active human gate.
    if (
        not already_frozen
        and lifecycle.get("timer_stopped_at")
        and not needs_confirm
    ):
        lifecycle["timer_stopped_at"] = None
        lifecycle["frozen_elapsed_seconds"] = None
        lifecycle["timer_stop_reason"] = ""

    if needs_confirm and first_started_dt is not None and not already_frozen:
        stop_at = (
            parsed.get("await_at")
            or parsed.get("human_confirm_at")
            or now_iso
        )
        stop_dt = order_model._parse_ts(str(stop_at or "")) or now_dt
        if stop_dt < first_started_dt:
            stop_dt = first_started_dt
            stop_at = order_model._ts_to_iso(stop_dt) or now_iso
        frozen = max(0.0, (stop_dt - first_started_dt).total_seconds())
        lifecycle["timer_stopped_at"] = stop_at
        lifecycle["frozen_elapsed_seconds"] = frozen
        lifecycle["timer_stop_reason"] = "human_prompt"

    # Every actual order-ending status must freeze the order clock too. The
    # previous implementation only froze at a human prompt, so success/cancel/
    # error/manual-complete kept increasing forever in the browser.
    if lifecycle.get("ended") and first_started_dt is not None and not already_frozen:
        stop_at = lifecycle.get("ended_at") or now_iso
        stop_dt = order_model._parse_ts(str(stop_at or "")) or now_dt
        latest_task_end: Optional[datetime] = None
        for task in tasks:
            task_end = order_model._parse_ts(str(task.get("ended_at") or ""))
            if task_end is not None and (
                latest_task_end is None or task_end > latest_task_end
            ):
                latest_task_end = task_end
        if latest_task_end is not None:
            stop_dt = latest_task_end
            stop_at = order_model._ts_to_iso(stop_dt) or stop_at
        if stop_dt < first_started_dt:
            stop_dt = first_started_dt
            stop_at = order_model._ts_to_iso(stop_dt) or now_iso
        lifecycle["timer_stopped_at"] = stop_at
        lifecycle["frozen_elapsed_seconds"] = max(
            0.0, (stop_dt - first_started_dt).total_seconds()
        )
        lifecycle["timer_stop_reason"] = (
            "broker_ended" if broker_ended else "order_ended"
        )
        already_frozen = True
    # Once frozen on a valid human prompt or Broker terminal, keep until a new order.

    failure_reasons = {
        "human_error",
        "broker_error",
        "items_failed",
    }
    # Active robot picking takes priority over a stale closed/failed badge.
    if has_robot_running and not broker_ended:
        lifecycle["label"] = "工单进行中"
    elif has_error_wait:
        lifecycle["label"] = "待确认报错"
    elif item_needs_confirm or (needs_confirm and human_kind == "confirm" and unfinished):
        lifecycle["label"] = "待人工确认"
    elif lifecycle["closed"]:
        if lifecycle.get("end_reason") in failure_reasons or has_failure:
            lifecycle["label"] = "工单已结束（失败）"
        elif lifecycle.get("end_reason") == "broker_cancel":
            lifecycle["label"] = "工单已取消"
        elif lifecycle.get("end_reason") == "broker_transferred":
            lifecycle["label"] = "工单已转单关闭"
        elif lifecycle.get("end_reason") == "broker_manual_completed":
            lifecycle["label"] = "人工处理已完成"
        else:
            lifecycle["label"] = "工单已确认关闭"
    elif lifecycle["ended"] and needs_confirm:
        if lifecycle.get("end_reason") == "human_error":
            lifecycle["label"] = "工单已结束 · 待确认报错"
        else:
            lifecycle["label"] = "工单已结束 · 待打包/人工确认"
    elif lifecycle["ended"] or stopped_on_failure:
        if lifecycle.get("end_reason") in failure_reasons or has_failure:
            lifecycle["label"] = "工单失败 · 已停止"
        elif broker_status in order_model._BROKER_MANUAL_HELD:
            lifecycle["label"] = "已转人工 · 等待人工完成"
        else:
            lifecycle["label"] = "取货完成 · 待收尾"
    elif needs_confirm and human_kind == "error":
        lifecycle["label"] = "待确认报错"
    elif needs_confirm:
        lifecycle["label"] = "待人工确认"
    else:
        # 无工单且无任何子任务活动时应显示空闲，而不是“工单进行中”。
        lifecycle["label"] = "工单进行中" if (order is not None or tasks) else "空闲"

    # Only skip never-started items when the order is truly closed.
    # Do not skip while later SKUs may still arrive on the same task.
    if lifecycle["closed"] and not has_live_work:
        for task in tasks:
            status = str(task.get("status") or "pending")
            never_started = (
                status in {"pending", "skipped"}
                and not task.get("started_at")
                and not task.get("ended_at")
                and not task.get("duration_seconds")
                and not task.get("end_line")
            )
            if never_started:
                task["status"] = "skipped"
                task["status_label"] = order_model._STATUS_LABELS["skipped"]
                task["active"] = False
                task["needs_confirm"] = False
            elif status in {"started", "processing"} and needs_confirm:
                if not task.get("needs_confirm"):
                    task["active"] = False
            elif status in {"success", "failed"}:
                task["active"] = False
                task["needs_confirm"] = False
                task["status_label"] = order_model._STATUS_LABELS.get(status, status)
    else:
        # Revive items wrongly marked skipped while the order is still running.
        for task in tasks:
            if str(task.get("status") or "") == "skipped" and not task.get("end_line"):
                task["status"] = "pending"
                task["status_label"] = order_model._STATUS_LABELS["pending"]
                task["active"] = False
                task["needs_confirm"] = False

    # Broker only owns the order lifecycle. A matching robot log prompt remains
    # actionable even after Broker changes state (manual transfer/terminal included).
    if broker_ended:
        for task in tasks:
            task["active"] = False

    if order is not None:
        order = deepcopy(order)
        order["lifecycle"] = lifecycle
        if broker.get("ok") and broker.get("order_no") and not order.get("order_no"):
            order["order_no"] = broker.get("order_no")
        with order_store._ACTIVE_ORDER_LOCK:

            if order_store._ACTIVE_ORDER is not None and str(
                order_store._ACTIVE_ORDER.get("task_id") or ""
            ) == str(order.get("task_id") or ""):
                changed = order_store._ACTIVE_ORDER.get("lifecycle") != lifecycle
                if changed:
                    order_store._ACTIVE_ORDER["lifecycle"] = deepcopy(lifecycle)
                if order.get("order_no") and order_store._ACTIVE_ORDER.get("order_no") != order.get(
                    "order_no"
                ):
                    order_store._ACTIVE_ORDER["order_no"] = order.get("order_no")
                    changed = True
                if changed:
                    order_store._save_active_order_unlocked()

    aggregate = "idle"
    if needs_confirm:
        aggregate = "await_error" if human_kind == "error" else "await_confirm"
    elif lifecycle["closed"]:
        aggregate = (
            "failed"
            if (
                lifecycle.get("end_reason")
                in {"human_error", "broker_error", "items_failed", "broker_transferred"}
                or has_failure
            )
            else "success"
        )
    elif stopped_on_failure or (
        lifecycle["ended"]
        and (
            has_failure
            or lifecycle.get("end_reason")
            in {"human_error", "broker_error", "items_failed"}
        )
    ):
        aggregate = "failed"
    elif lifecycle["ended"]:
        aggregate = "order_ended"
    else:
        aggregate = _aggregate_from_tasks(tasks)
        if aggregate == "pending":
            aggregate = "idle"

    return lifecycle, tasks, aggregate


def _merge_order_items(
    order: Optional[Dict[str, object]],
    parsed: Dict[str, object],
    focus_task_id: str = "",
) -> List[Dict[str, object]]:
    states: Dict[str, Dict[str, object]] = parsed.get("item_states")  # type: ignore[assignment]
    if not isinstance(states, dict):
        states = {}
    remembered = _remembered_states(order)
    merged: List[Dict[str, object]] = []
    seen: set = set()
    order_task_id = ""
    if order is not None:
        order_task_id = str(order.get("task_id") or "").strip()
    focus_task_id = str(focus_task_id or "").strip()
    allowed_parents = {value for value in (order_task_id, focus_task_id) if value}
    order_items = []
    if order is not None and isinstance(order.get("items"), list):
        order_items = [raw for raw in order["items"] if isinstance(raw, dict)]  # type: ignore[index]

    # Start from registered/expanded order items, then include same-task log SKUs.
    if order_items:
        for raw in order_items:
            code = str(raw.get("code") or raw.get("barcode") or "").strip()
            if not code:
                continue
            seen.add(code)
            fresh = states.get(code) or log_parser._empty_item_state(code)
            state = _merge_item_state(remembered.get(code), fresh)
            row = deepcopy(state)
            row.update(
                {
                    "index": raw.get("index"),
                    "item_id": raw.get("item_id") or code,
                    "barcode": raw.get("barcode") or code,
                    "sku_id": raw.get("sku_id") or "",
                    "name": raw.get("name") or "",
                    "location_code": raw.get("location_code")
                    or state.get("location_code")
                    or "",
                    "quantity": raw.get("quantity") or 1,
                    "group_id": raw.get("group_id") or "",
                    "group_field": raw.get("group_field") or "",
                    "from_order": True,
                    "code": code,
                }
            )
            if order_task_id and not row.get("parent_task_id"):
                row["parent_task_id"] = order_task_id
            merged.append(row)

    for code, state in states.items():
        if code in seen:
            continue
        parent = str(state.get("parent_task_id") or "").strip()
        if allowed_parents and parent and parent not in allowed_parents:
            continue
        if order_items and parent and parent not in allowed_parents:
            continue
        if order_items and not parent and code not in remembered:
            continue
        seen.add(code)
        row = _merge_item_state(remembered.get(code), state)
        row.update(
            {
                "index": len(merged) + 1,
                "item_id": code,
                "barcode": code,
                "name": "",
                "quantity": 1,
                "from_order": bool(order_items),
                "code": code,
            }
        )
        merged.append(row)

    def sort_key(row: Dict[str, object]) -> Tuple[int, str]:
        index = row.get("index")
        try:
            index_num = int(index)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            index_num = 10_000
        return (index_num, str(row.get("code") or ""))

    merged.sort(key=sort_key)
    return merged


def _order_elapsed_seconds(
    order: Optional[Dict[str, object]],
    tasks: List[Dict[str, object]],
    lifecycle: Dict[str, object],
    polled_at: Optional[str],
) -> Optional[float]:
    """First 开始处理 → human gate or Broker terminal; then stays frozen."""
    frozen = lifecycle.get("frozen_elapsed_seconds")
    if (
        frozen is not None
        and str(lifecycle.get("timer_stop_reason") or "") in order_model._TIMER_STOP_REASONS
    ):
        try:
            return max(0.0, float(frozen))
        except (TypeError, ValueError):
            pass

    # Start counting only after first 开始处理 — not when the next order
    # is merely received / registered.
    start_candidates: List[datetime] = []
    for task in tasks:
        started = order_model._parse_ts(str(task.get("started_at") or ""))
        if started is not None:
            start_candidates.append(started)
    if not start_candidates:
        return None
    end_dt = order_model._parse_ts(str(polled_at or "")) or datetime.now(timezone.utc)
    elapsed = order_model._duration_seconds(min(start_candidates), end_dt)
    return None if elapsed is None else max(0.0, elapsed)


def _aggregate_from_tasks(tasks: List[Dict[str, object]]) -> str:
    if not tasks:
        return "idle"
    statuses = [str(item.get("status") or "pending") for item in tasks]
    if any(status == "await_error" for status in statuses):
        return "await_error"
    if any(status == "await_confirm" for status in statuses):
        return "await_confirm"
    if any(status in {"started", "processing"} for status in statuses):
        return "processing"
    if all(status == "success" for status in statuses):
        return "success"
    if any(status == "failed" for status in statuses):
        # Pending leftovers after a failure are abandoned, not "still processing".
        return "failed"
    if all(status == "pending" for status in statuses):
        return "pending"
    return "processing"


def _build_dashboard_snapshot(tail: int) -> Dict[str, object]:
    if tail < 50 or tail > 5000:
        raise LogServiceError("tail 必须在 50~5000 之间。", 400)
    settings = dashboard_settings.load_dashboard_settings()
    mode = str(settings.get("mode") or dashboard_settings._DEFAULT_DASHBOARD_MODE)
    broker_configured = order_api._is_broker_configured(mode)
    info = inspect_container(keyboard.ROBOT_SERVICE_NAME)
    auto_confirm = bool(settings.get("auto_confirm"))
    etm_status: Dict[str, object] = {
        "ok": mode != "prod",
        "reachable": False,
        "base_url": str(settings.get("etm_base_url") or DEFAULT_ETM_BASE_URL),
        "next_task_id": "",
        "cloud_ok": False,
        "error": "",
    }
    if not broker_configured:
        # 未配置 Broker 时不展示任何工单状态：持久化的 active order 是上一次
        # 有效配置时的残留，继续渲染会让现场误以为仍有工单。返回空快照，
        # 前端会隐藏工单面板并显示空的子任务/事件列表（与门店任务列表一致）。
        return {
            "service": keyboard.ROBOT_SERVICE_NAME,
            "service_running": bool(info.get("running")),
            "service_status": info.get("status"),
            "service_message": info.get("message") or "",
            "polled_at": order_model._ts_to_iso(datetime.now(timezone.utc)),
            "order": None,
            "order_queue": {
                "capacity": active_orders._ORDER_QUEUE_LIMIT,
                "total": 0,
                "queued_count": 0,
                "full": False,
                "queued": [],
            },
            "dashboard_mode": mode,
            "auto_confirm": auto_confirm,
            "etm": etm_status,
            "broker_configured": False,
            "status": "idle",
            "status_label": order_model._STATUS_LABELS["idle"],
            "needs_confirm": False,
            "dismissed_fingerprint": "",
            "await_kind": "",
            "task_id": "",
            "object_hint": "",
            "started_at": None,
            "await_at": None,
            "ended_at": None,
            "elapsed_to_await_seconds": None,
            "elapsed_seconds": None,
            "order_elapsed_seconds": None,
            "start_line": "",
            "await_line": "",
            "end_line": "",
            "events": [],
            "tasks": [],
            "active_code": "",
            "current_item": None,
            "order_lifecycle": {},
            "broker_order": {
                "ok": False,
                "status": "",
                "status_label": "",
                "ended": False,
                "terminal": False,
                "order_no": "",
                "error": "未配置 Broker",
                "source": "",
            },
            "progress": {
                "total": 0,
                "done": 0,
                "failed": 0,
                "skipped": 0,
                "active": 0,
            },
            "log_tail": tail,
            "error": "",
        }
    order = active_orders.get_active_order()
    if mode == "prod":
        order, etm_status = _sync_order_from_etm(order, settings)
    payload: Dict[str, object] = {
        "service": keyboard.ROBOT_SERVICE_NAME,
        "service_running": bool(info.get("running")),
        "service_status": info.get("status"),
        "service_message": info.get("message") or "",
        "polled_at": order_model._ts_to_iso(datetime.now(timezone.utc)),
        "order": _public_order(order),
        "order_queue": active_orders.order_queue_status(),
        "dashboard_mode": mode,
        "auto_confirm": auto_confirm,
        "etm": etm_status,
        "broker_configured": broker_configured,
    }
    if not info.get("running"):
        tasks = _merge_order_items(order, {"item_states": {}})
        # 服务未启动也要把 Broker 带回的名称/库位填上，否则卡片仍是空的。
        if broker_configured:
            parent = "" if order is None else str(order.get("task_id") or "")
            if parent:
                _apply_broker_item_details(tasks, order_api._fetch_broker_order(parent, mode))
        _persist_item_states(order, tasks)
        done = sum(1 for task in tasks if task.get("status") == "success")
        failed = sum(1 for task in tasks if task.get("status") == "failed")
        skipped = sum(1 for task in tasks if task.get("status") == "skipped")
        payload.update(
            {
                "status": "idle",
                "status_label": "服务未启动",
                "needs_confirm": False,
                "await_kind": "",
                "task_id": "" if order is None else order.get("task_id") or "",
                "object_hint": "",
                "started_at": None,
                "await_at": None,
                "ended_at": None,
                "elapsed_to_await_seconds": None,
                "elapsed_seconds": None,
                "start_line": "",
                "await_line": "",
                "end_line": "",
                "events": [],
                "tasks": tasks,
                "active_code": "",
                "current_item": None,
                "progress": {
                    "total": len(tasks),
                    "done": done,
                    "failed": failed,
                    "skipped": skipped,
                    "active": 0,
                },
                "error": str(info.get("message") or "服务未启动"),
                "log_available": False,
                "log_error": "",
            }
        )
        return payload

    # 日志取不到时不能让整个仪表盘 502：Broker 状态与子任务卡片并不依赖日志，
    # 仍应正常展示，只把「处理进度/当前子任务」标为不可用，避免把「抓不到日志」
    # 误示为「尚未开始处理商品」。
    log_available = True
    log_error = ""
    try:
        logs_payload = fetch_logs(keyboard.ROBOT_SERVICE_ID, tail, "")
        raw_logs = str(logs_payload.get("logs") or "")
    except LogServiceError as error:
        log_available = False
        log_error = str(error)
        raw_logs = ""
        LOGGER.warning("仪表盘读取机器人日志失败，仅展示 Broker 侧信息：%s", error)
    payload["log_available"] = log_available
    payload["log_error"] = log_error
    order = _resolve_active_order(order, raw_logs)
    order = _reconcile_broker_task_id(order, mode)
    order = _promote_latest_broker_order(order, mode)
    if mode == "prod":
        # After log discovery, try cloud enrich again for order_no / names.
        order, etm_status = _sync_order_from_etm(order, settings)
        payload["etm"] = etm_status
    # sku_id → 69码 别名：订单持久化的 ∪ 当前窗口新学的，随后贯穿发现/匹配/解析，
    # 并写回工单持久化——起始行滚出日志窗口后，结束行依然能归入正确子任务。
    code_aliases = log_parser._merged_code_aliases(order, raw_logs)
    latest_task_id, codes_by_task, last_seen_by_task = log_parser._discover_log_tasks(
        raw_logs, aliases=code_aliases
    )
    order_codes = order_model._order_item_codes(order)
    focus_task_id = log_parser._match_log_task_for_order(
        order, codes_by_task, latest_task_id, last_seen_by_task
    )
    stale_task_ids = log_parser._stale_log_tasks(order, last_seen_by_task)
    if stale_task_ids:
        _prune_stale_item_states(order, stale_task_ids)
    parsed = log_parser.parse_robot_log_text(
        raw_logs,
        focus_task_id=focus_task_id,
        extra_allowed_codes=order_codes,
        stale_task_ids=stale_task_ids,
        aliases=code_aliases,
    )
    if code_aliases:
        _persist_code_aliases(order, code_aliases)
    if order is None and focus_task_id:
        order = _resolve_active_order(None, raw_logs)
    _persist_feishu_log_cache(order, raw_logs)
    tasks = _merge_order_items(order, parsed, focus_task_id=focus_task_id)
    # Broker still keyed by registered order task_id when present.
    parent_task_id = ""
    if order is not None:
        parent_task_id = str(order.get("task_id") or "").strip()
    if not parent_task_id:
        parent_task_id = focus_task_id or str(parsed.get("task_id") or "")
    if broker_configured:
        broker = order_api._fetch_broker_order(parent_task_id, mode)
    else:
        broker = {"ok": False, "error": "未配置 Broker"}
    if (
        broker_configured
        and mode == "prod"
        and not broker.get("ok")
        and parent_task_id
    ):
        try:
            cloud_body = _etm_get_json(
                str(etm_status.get("base_url") or DEFAULT_ETM_BASE_URL),
                f"/api/v1/tasks/cloud/{quote(parent_task_id, safe='')}",
            )
            cloud = cloud_body.get("data")
            if isinstance(cloud, dict):
                broker = _broker_from_etm_cloud(cloud, parent_task_id)
                etm_status["cloud_ok"] = True
                payload["etm"] = etm_status
        except LogServiceError as error:
            etm_status["error"] = str(error)
            payload["etm"] = etm_status
    # A log-discovered order may only learn its stable order identity from the
    # first Broker detail response. Reconcile once more in that same snapshot,
    # then fetch status using the corrected Broker task id.
    if order is not None and broker.get("ok"):
        changed_identity = False
        with order_store._ACTIVE_ORDER_LOCK:

            if order_store._ACTIVE_ORDER is not None and str(
                order_store._ACTIVE_ORDER.get("task_id") or ""
            ) == str(order.get("task_id") or ""):
                for key in ("order_no", "platform_order_no"):
                    value = str(broker.get(key) or "").strip()
                    if value and not order_store._ACTIVE_ORDER.get(key):
                        order_store._ACTIVE_ORDER[key] = value
                        order[key] = value
                        changed_identity = True
                if changed_identity:
                    order_store._save_active_order_unlocked()
        reconciled = _reconcile_broker_task_id(order, mode)
        if reconciled is not None:
            order = reconciled
        corrected_parent = str(order.get("task_id") or "").strip()
        if corrected_parent and corrected_parent != parent_task_id:
            parent_task_id = corrected_parent
            broker = order_api._fetch_broker_order(parent_task_id, mode)
    # Broker 状态原值直出，不做本地覆盖。
    # 子任务的名称/库位以 Broker 带回的下单信息为准，必须在生命周期与计时
    # 处理之前回填，后续的 focus（大字区）从 tasks 里选，就能自动同源。
    _apply_broker_item_details(tasks, broker)
    lifecycle, tasks, aggregate = _apply_order_lifecycle(
        order, parsed, broker, tasks
    )
    polled_at_early = str(payload.get("polled_at") or "")
    if not lifecycle.get("ended"):
        log_parser._refresh_live_elapsed(tasks, polled_at_early)
    _persist_item_states(order, tasks)
    if order is not None:
        order = deepcopy(active_orders.get_active_order() or order)
        order["lifecycle"] = lifecycle
        if broker.get("ok") and broker.get("order_no") and not order.get("order_no"):
            order["order_no"] = broker.get("order_no")
        if (
            broker.get("ok")
            and broker.get("platform_order_no")
            and not order.get("platform_order_no")
        ):
            order["platform_order_no"] = broker.get("platform_order_no")
        if not order.get("order_source"):
            inferred = order_model._infer_order_source(
                broker.get("order_source") if broker.get("ok") else "",
                order.get("platform_order_no") or broker.get("platform_order_no"),
            )
            if inferred:
                order["order_source"] = inferred
        if order.get("order_source") or order.get("platform_order_no"):
            with order_store._ACTIVE_ORDER_LOCK:
                if order_store._ACTIVE_ORDER is not None and str(
                    order_store._ACTIVE_ORDER.get("task_id") or ""
                ) == str(order.get("task_id") or ""):
                    if order.get("order_source") and not order_store._ACTIVE_ORDER.get(
                        "order_source"
                    ):
                        order_store._ACTIVE_ORDER["order_source"] = order.get("order_source")
                    if order.get("platform_order_no") and not order_store._ACTIVE_ORDER.get(
                        "platform_order_no"
                    ):
                        order_store._ACTIVE_ORDER["platform_order_no"] = order.get(
                            "platform_order_no"
                        )
                    order_store._save_active_order_unlocked()

    focus_code = str(parsed.get("active_code") or "")
    focus = None
    if focus_code:
        # 日志只能给出条码或编号，不保证是哪一种；任一标识命中就当作处理中。
        for task in tasks:
            identities = {
                str(task.get(key) or "").strip()
                for key in _ITEM_MATCH_FIELDS
            }
            if focus_code in identities:
                focus = task
                break
    if focus is None:
        focus = next((task for task in tasks if task.get("needs_confirm")), None)
    if focus is None:
        focus = next((task for task in tasks if task.get("active")), None)
    if focus is None:
        focus = next(
            (task for task in tasks if str(task.get("status") or "") == "failed"),
            None,
        )
    if focus is None:
        focus = next(
            (
                task
                for task in reversed(tasks)
                if str(task.get("status") or "") in {"success", "skipped"}
            ),
            None,
        )

    done = sum(1 for task in tasks if task.get("status") == "success")
    failed = sum(1 for task in tasks if task.get("status") == "failed")
    skipped = sum(1 for task in tasks if task.get("status") == "skipped")
    active_count = sum(1 for task in tasks if task.get("active"))
    order_await = bool(parsed.get("order_await_active"))
    needs_confirm = any(task.get("needs_confirm") for task in tasks) or order_await
    if order_await and aggregate not in {"await_confirm", "await_error"}:
        aggregate = (
            "await_error"
            if str(parsed.get("await_kind") or "") == "error"
            else "await_confirm"
        )
    status_label = str(lifecycle.get("label") or order_model._STATUS_LABELS.get(aggregate, aggregate))
    await_kind = str(parsed.get("await_kind") or "")
    if not await_kind and focus is not None:
        await_kind = str(focus.get("await_kind") or "")
    await_line = str(parsed.get("await_line") or "")
    if not await_line and focus is not None:
        await_line = str(focus.get("await_line") or "")
    await_at = parsed.get("await_at")
    if await_at is None and focus is not None:
        await_at = focus.get("await_at")
    polled_at = str(payload.get("polled_at") or "")
    if not lifecycle.get("ended"):
        log_parser._refresh_live_elapsed(tasks, polled_at)
    order_elapsed = _order_elapsed_seconds(order, tasks, lifecycle, polled_at)
    if (
        order_elapsed is not None
        and order_elapsed > 0
        and str(lifecycle.get("timer_stop_reason") or "") in order_model._TIMER_STOP_REASONS
    ):
        previous_frozen = lifecycle.get("frozen_elapsed_seconds")
        try:
            previous_value = (
                float(previous_frozen) if previous_frozen is not None else 0.0
            )
        except (TypeError, ValueError):
            previous_value = 0.0
        if order_elapsed >= previous_value:
            lifecycle["frozen_elapsed_seconds"] = order_elapsed
        else:
            order_elapsed = previous_value
        expected_task_id = (
            str(order.get("task_id") or "").strip()
            if isinstance(order, dict)
            else str(parent_task_id or "").strip()
        )
        with order_store._ACTIVE_ORDER_LOCK:
            if order_store._ACTIVE_ORDER is not None and (
                not expected_task_id
                or str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
                == expected_task_id
            ):
                life = order_store._ACTIVE_ORDER.get("lifecycle")
                if not isinstance(life, dict):
                    life = {}
                    order_store._ACTIVE_ORDER["lifecycle"] = life
                changed = False
                for key in (
                    "frozen_elapsed_seconds",
                    "timer_stopped_at",
                    "timer_stop_reason",
                    "ended",
                    "closed",
                ):
                    value = lifecycle.get(key)
                    if life.get(key) != value:
                        life[key] = value
                        changed = True
                if changed:
                    order_store._save_active_order_unlocked()
    focus_elapsed = None if focus is None else focus.get("elapsed_seconds")
    dismissed_fingerprint = _dismissed_fingerprint_from_order(order)
    if not dismissed_fingerprint:
        with order_store._ACTIVE_ORDER_LOCK:
            order_store._ensure_active_order_loaded()
            dismissed_fingerprint = _dismissed_fingerprint_from_order(order_store._ACTIVE_ORDER)

    payload["order"] = _public_order(order)
    payload["order_queue"] = active_orders.order_queue_status()
    payload.update(
        {
            "status": aggregate,
            "status_label": status_label,
            "needs_confirm": needs_confirm,
            # 提示被真实确认/关闭（日志出现「继续」类行）才为真；前端据此区分
            # 「提示被处理」与「提示短暂滚出解析窗口」，避免误判锁死弹窗。
            "confirm_closed": bool(parsed.get("human_confirm_closed")),
            "dismissed_fingerprint": dismissed_fingerprint,
            "await_kind": await_kind,
            "task_id": parent_task_id,
            "object_hint": "" if focus is None else focus.get("code") or "",
            "started_at": None if focus is None else focus.get("started_at"),
            "await_at": await_at,
            "ended_at": lifecycle.get("ended_at")
            or (None if focus is None else focus.get("ended_at")),
            "elapsed_to_await_seconds": None
            if focus is None
            else focus.get("elapsed_to_await_seconds"),
            "elapsed_seconds": focus_elapsed,
            "order_elapsed_seconds": order_elapsed,
            "start_line": "" if focus is None else focus.get("start_line") or "",
            "await_line": await_line,
            "end_line": "" if focus is None else focus.get("end_line") or "",
            "events": parsed.get("events") or [],
            "tasks": tasks,
            "active_code": "" if focus is None else focus.get("code") or "",
            "current_item": focus,
            "order_lifecycle": lifecycle,
            "dashboard_mode": mode,
            "auto_confirm": auto_confirm,
            "etm": etm_status,
            "broker_order": {
                "ok": bool(broker.get("ok")),
                "task_id": broker.get("task_id") or "",
                "status": broker.get("status") or "",
                "status_label": broker.get("status_label") or "",
                "ended": bool(broker.get("ended")),
                "terminal": bool(broker.get("terminal")),
                "order_no": broker.get("order_no") or "",
                "error": broker.get("error") or "",
                "source": broker.get("source") or "",
            },
            "progress": {
                "total": len(tasks),
                "done": done,
                "failed": failed,
                "skipped": skipped,
                "active": active_count,
            },
            "log_tail": tail,
        }
    )
    # Submit on first human-gate speak (pack/confirm/error/cancel…), not on key.
    feishu_result: Optional[Dict[str, object]] = None
    if isinstance(order, dict) and should_submit_on_human_prompt(
        needs_confirm,
        bool(parsed.get("human_confirm_seen")),
        await_kind,
        order,
    ):
        working_order = deepcopy(order)
        working_order["await_kind"] = await_kind
        working_order["await_line"] = await_line
        if isinstance(lifecycle, dict) and lifecycle.get("broker_status"):
            working_order["broker_status"] = lifecycle.get("broker_status")
        feishu_result = maybe_submit_feishu_form(
            working_order,
            tasks,
            mode,
            settings,
            "human_prompt",
            _persist_feishu_submit_state,
            active_orders.get_active_order,
        )
        refreshed = active_orders.get_active_order()
        if isinstance(refreshed, dict):
            order = refreshed
    payload["order"] = _public_order(order)
    if feishu_result is not None:
        payload["feishu"] = feishu_result
    return payload


def _confirmation_fingerprint(snapshot: Mapping[str, object]) -> str:
    current = snapshot.get("current_item")
    current = current if isinstance(current, dict) else {}
    order = snapshot.get("order")
    order = order if isinstance(order, dict) else {}
    return "|".join(
        str(value or "")
        for value in (
            order.get("task_id") or snapshot.get("task_id"),
            snapshot.get("active_code"),
            current.get("status") or snapshot.get("status"),
            current.get("await_at") or snapshot.get("await_at"),
            current.get("await_line") or snapshot.get("await_line"),
        )
    )


def _reconcile_pending_confirmation(
    snapshot: Dict[str, object],
) -> Dict[str, object]:
    """Persist a prompt until logs explicitly show that robot execution resumed."""
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        if order_store._ACTIVE_ORDER is None:
            return snapshot
        active_task_id = str(order_store._ACTIVE_ORDER.get("task_id") or "").strip()
        snapshot_order = snapshot.get("order")
        snapshot_task_id = str(
            (
                snapshot_order.get("task_id")
                if isinstance(snapshot_order, dict)
                else ""
            )
            or snapshot.get("task_id")
            or ""
        ).strip()
        # A concurrent create invalidates this whole refresh. Do not copy an old
        # prompt onto the newly-created order while the cache generation retries.
        if active_task_id and snapshot_task_id and active_task_id != snapshot_task_id:
            return snapshot

        raw_pending = order_store._ACTIVE_ORDER.get("pending_confirm")
        pending = deepcopy(raw_pending) if isinstance(raw_pending, dict) else None
        if pending is not None and str(pending.get("task_id") or "").strip() not in {
            "",
            active_task_id,
        }:
            order_store._ACTIVE_ORDER.pop("pending_confirm", None)
            order_store._save_active_order_unlocked()
            pending = None

        if snapshot.get("confirm_closed"):
            if pending is not None:
                order_store._ACTIVE_ORDER.pop("pending_confirm", None)
                order_store._save_active_order_unlocked()
            snapshot["needs_confirm"] = False
            snapshot.pop("confirm_fingerprint", None)
            return snapshot

        if snapshot.get("needs_confirm"):
            current = snapshot.get("current_item")
            current = current if isinstance(current, dict) else {}
            status = str(snapshot.get("status") or "await_confirm")
            if status not in {"await_confirm", "await_error"}:
                status = (
                    "await_error"
                    if str(snapshot.get("await_kind") or "") == "error"
                    else "await_confirm"
                )
            detected = {
                "task_id": active_task_id or snapshot_task_id,
                "status": status,
                "kind": str(snapshot.get("await_kind") or current.get("await_kind") or ""),
                "code": str(
                    snapshot.get("active_code")
                    or current.get("code")
                    or snapshot.get("object_hint")
                    or ""
                ),
                "at": snapshot.get("await_at") or current.get("await_at"),
                "line": str(snapshot.get("await_line") or current.get("await_line") or ""),
                "fingerprint": _confirmation_fingerprint(snapshot),
            }
            logical_keys = ("task_id", "status", "kind", "code", "line")
            if isinstance(raw_pending, dict) and all(
                raw_pending.get(key) == detected.get(key) for key in logical_keys
            ):
                pending = deepcopy(raw_pending)
            else:
                pending = detected
            if raw_pending != pending:
                order_store._ACTIVE_ORDER["pending_confirm"] = deepcopy(pending)
                order_store._save_active_order_unlocked()

        if pending is None:
            return snapshot
        active_order = deepcopy(order_store._ACTIVE_ORDER)

    # A log line may temporarily disappear while Docker reconnects or the tail
    # rolls. Restore only the confirmation-facing fields; keep current service,
    # Broker, progress, and timing data from this fresh snapshot.
    snapshot["needs_confirm"] = True
    snapshot["confirm_closed"] = False
    snapshot["status"] = str(pending.get("status") or "await_confirm")
    snapshot["status_label"] = order_model._STATUS_LABELS.get(
        str(snapshot["status"]), str(snapshot["status"])
    )
    snapshot["await_kind"] = str(pending.get("kind") or "")
    snapshot["task_id"] = str(pending.get("task_id") or active_task_id)
    snapshot["active_code"] = str(pending.get("code") or "")
    snapshot["object_hint"] = str(pending.get("code") or "")
    snapshot["await_at"] = pending.get("at")
    snapshot["await_line"] = str(pending.get("line") or "")
    snapshot["confirm_fingerprint"] = str(pending.get("fingerprint") or "")
    snapshot["dismissed_fingerprint"] = _dismissed_fingerprint_from_order(active_order)
    if snapshot.get("order") is None:
        snapshot["order"] = _public_order(active_order)
    current = snapshot.get("current_item")
    if not isinstance(current, dict):
        current = {"code": snapshot["active_code"]}
        snapshot["current_item"] = current
    current["await_kind"] = snapshot["await_kind"]
    current["await_at"] = snapshot["await_at"]
    current["await_line"] = snapshot["await_line"]
    current["needs_confirm"] = True
    return snapshot


def _refresh_dashboard_snapshot(tail: int) -> Dict[str, object]:

    while True:
        with _DASHBOARD_REFRESH_LOCK:
            with dashboard_cache._DASHBOARD_CACHE_LOCK:
                generation = dashboard_cache._DASHBOARD_CACHE_GENERATION
            snapshot = _reconcile_pending_confirmation(
                _build_dashboard_snapshot(tail)
            )
            if active_orders.promote_queued_order_if_ready():
                continue
            with dashboard_cache._DASHBOARD_CACHE_LOCK:
                if generation != dashboard_cache._DASHBOARD_CACHE_GENERATION:
                    continue
                dashboard_cache._DASHBOARD_CACHE = deepcopy(snapshot)
                return deepcopy(snapshot)


def get_dashboard_snapshot(tail: int) -> Dict[str, object]:
    """Build a fresh serialized snapshot (used by manual refresh and tests)."""
    return _refresh_dashboard_snapshot(tail)


def get_dashboard_monitor_snapshot(
    tail: int, *, force: bool = False
) -> Dict[str, object]:
    """Read the resident listener cache; synchronously seed it on first request."""
    if tail < 50 or tail > 5000:
        raise LogServiceError("tail 必须在 50~5000 之间。", 400)
    if force:
        return _refresh_dashboard_snapshot(tail)
    with dashboard_cache._DASHBOARD_CACHE_LOCK:
        cached = deepcopy(dashboard_cache._DASHBOARD_CACHE)
    if cached is not None:
        return cached
    # The worker may already be building. The refresh lock makes concurrent
    # first requests wait for one builder instead of starting duplicate work.
    with _DASHBOARD_REFRESH_LOCK:
        with dashboard_cache._DASHBOARD_CACHE_LOCK:
            cached = deepcopy(dashboard_cache._DASHBOARD_CACHE)
        if cached is not None:
            return cached
    return _refresh_dashboard_snapshot(tail)


def _dashboard_monitor_delay(snapshot: Optional[Mapping[str, object]]) -> float:
    if not snapshot or snapshot.get("needs_confirm"):
        return _DASHBOARD_MONITOR_ACTIVE_SECONDS
    broker = snapshot.get("broker_order")
    broker_status = (
        str(broker.get("status") or "") if isinstance(broker, dict) else ""
    )
    if broker_status in {"pending", "dispatched", "running", "awaiting_pack"}:
        return _DASHBOARD_MONITOR_ACTIVE_SECONDS
    if str(snapshot.get("status") or "") in {
        "started",
        "processing",
        "await_confirm",
        "await_error",
    }:
        return _DASHBOARD_MONITOR_ACTIVE_SECONDS
    return _DASHBOARD_MONITOR_IDLE_SECONDS


def _dashboard_monitor_loop(stop_event: Event) -> None:
    last_error = ""
    while not stop_event.is_set():
        snapshot: Optional[Dict[str, object]] = None
        try:
            snapshot = _refresh_dashboard_snapshot(_DASHBOARD_MONITOR_TAIL)
            if last_error:
                LOGGER.info("后台仪表盘监听已恢复。")
                last_error = ""
        except Exception as error:  # keep the resident listener alive
            message = str(error) or error.__class__.__name__
            if message != last_error:
                LOGGER.warning("后台仪表盘监听失败，将自动重试：%s", message)
                last_error = message
            with dashboard_cache._DASHBOARD_CACHE_LOCK:
                snapshot = deepcopy(dashboard_cache._DASHBOARD_CACHE)
        stop_event.wait(_dashboard_monitor_delay(snapshot))


def start_dashboard_monitor() -> None:
    """Start the process-wide dashboard/log listener once."""
    global _DASHBOARD_MONITOR_STOP, _DASHBOARD_MONITOR_THREAD
    with dashboard_cache._DASHBOARD_CACHE_LOCK:
        thread = _DASHBOARD_MONITOR_THREAD
        if thread is not None and thread.is_alive():
            return
        stop_event = Event()
        thread = Thread(
            target=_dashboard_monitor_loop,
            args=(stop_event,),
            name="ksq-dashboard-monitor",
            daemon=True,
        )
        _DASHBOARD_MONITOR_STOP = stop_event
        _DASHBOARD_MONITOR_THREAD = thread
        try:
            thread.start()
        except Exception:
            _DASHBOARD_MONITOR_STOP = None
            _DASHBOARD_MONITOR_THREAD = None
            raise


def stop_dashboard_monitor(timeout: float = 5.0) -> None:
    """Stop the resident listener without holding its state lock during join."""
    global _DASHBOARD_MONITOR_STOP, _DASHBOARD_MONITOR_THREAD
    with dashboard_cache._DASHBOARD_CACHE_LOCK:
        stop_event = _DASHBOARD_MONITOR_STOP
        thread = _DASHBOARD_MONITOR_THREAD
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.ident is not None:
        thread.join(max(0.0, timeout))
    with dashboard_cache._DASHBOARD_CACHE_LOCK:
        if _DASHBOARD_MONITOR_THREAD is thread and (
            thread is None or not thread.is_alive()
        ):
            _DASHBOARD_MONITOR_THREAD = None
            _DASHBOARD_MONITOR_STOP = None


def save_dashboard_settings(
    payload: Dict[str, object], restart_robot: bool
) -> Dict[str, object]:
    # Serialize read-modify-write of the settings file; the container recreate
    # below is deliberately left outside the lock since it can take seconds.
    settings, env_written, previous_mode = dashboard_settings.update_settings(payload)
    mode_changed = previous_mode != str(settings["mode"])
    if mode_changed:
        active_orders.clear_active_order()
    restart_result: Optional[Dict[str, object]] = None
    if restart_robot:
        # Recreate is required for env_file changes; docker restart keeps old env.
        restart_result = keyboard._recreate_robot_for_keyboard_env()
    mode_label = "生产" if settings["mode"] == "prod" else "测试"
    public_settings = {
        "keyboard_device": settings["keyboard_device"],
        "mode": settings["mode"],
        "etm_base_url": settings["etm_base_url"],
        "auto_confirm": bool(settings.get("auto_confirm")),
        "feishu": dashboard_settings._public_feishu_settings(
            settings["feishu"] if isinstance(settings.get("feishu"), dict) else {}
        ),
    }
    dashboard_cache._invalidate_dashboard_snapshot_cache()
    return {
        "ok": True,
        "settings": public_settings,
        "env_written": env_written,
        "env_path": str(dashboard_settings.ROBOT_KEYBOARD_ENV_FILE),
        "restart": restart_result,
        "mode_changed": mode_changed,
        "message": (
            f"已保存（仪表板模式：{mode_label}）。"
            + (
                " 已清空当前工单会话。"
                if mode_changed
                else ""
            )
            + (
                " 已请求重建机器人容器以使监听环境变量生效。"
                if restart_robot
                else ""
            )
        ),
    }


def _persist_feishu_submit_state(order: Dict[str, object]) -> None:
    task_id = str(order.get("task_id") or "").strip()
    state = order.get("feishu_submit")
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        if order_store._ACTIVE_ORDER is None:
            return
        if task_id and str(order_store._ACTIVE_ORDER.get("task_id") or "") != task_id:
            return
        order_store._ACTIVE_ORDER["feishu_submit"] = deepcopy(state) if isinstance(state, dict) else state
        order_store._save_active_order_unlocked()


def preview_feishu_submission() -> Dict[str, object]:
    settings = dashboard_settings.load_dashboard_settings()
    order = active_orders.get_active_order()
    return preview_feishu_form(
        order,
        [],
        str(settings.get("mode") or dashboard_settings._DEFAULT_DASHBOARD_MODE),
        settings,
    )


def submit_feishu_manual() -> Dict[str, object]:
    settings = dashboard_settings.load_dashboard_settings()
    order = active_orders.get_active_order()
    if order is None:
        raise ValueError("当前没有工单，无法提交飞书表单。")
    working = deepcopy(order)
    working.pop("feishu_submit", None)
    with order_store._ACTIVE_ORDER_LOCK:
        order_store._ensure_active_order_loaded()
        if order_store._ACTIVE_ORDER is not None and str(order_store._ACTIVE_ORDER.get("task_id") or "") == str(
            working.get("task_id") or ""
        ):
            order_store._ACTIVE_ORDER.pop("feishu_submit", None)
            order_store._save_active_order_unlocked()
    from ksq.feishu.submit import clear_feishu_dedupe_key

    clear_feishu_dedupe_key(working)
    return maybe_submit_feishu_form(
        working,
        [],
        str(settings.get("mode") or dashboard_settings._DEFAULT_DASHBOARD_MODE),
        settings,
        "manual",
        _persist_feishu_submit_state,
        active_orders.get_active_order,
    )


def confirm_and_maybe_submit_feishu() -> Dict[str, object]:
    """Inject confirm key. Feishu is normally submitted on speak; this is fallback."""
    settings = dashboard_settings.load_dashboard_settings()
    pre_order = active_orders.get_active_order()
    result = keyboard.inject_confirm_key()
    working_order = deepcopy(pre_order) if isinstance(pre_order, dict) else None
    if should_submit_on_confirm(working_order, ""):
        feishu_result = maybe_submit_feishu_form(
            working_order,
            [],
            str(settings.get("mode") or dashboard_settings._DEFAULT_DASHBOARD_MODE),
            settings,
            "confirm_fallback",
            _persist_feishu_submit_state,
            active_orders.get_active_order,
        )
    else:
        feishu_result = {
            "ok": False,
            "skipped": True,
            "reason": "already_handled_or_no_prompt",
        }
    result["feishu"] = feishu_result
    return result
