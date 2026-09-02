"""Dashboard: parse robot workspace logs and inject confirm key events."""

from __future__ import annotations

import json
import re
import subprocess
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Dict, List, Mapping, Optional, Tuple

import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlparse

from ksq.constants import (
    DASHBOARD_ACTIVE_ORDER_FILE,
    DASHBOARD_SETTINGS_FILE,
    DEFAULT_ETM_BASE_URL,
    ORDER_CONFIG_FILE,
    ORDER_CONFIG_PROD_FILE,
    ROBOT_KEYBOARD_ENV_FILE,
)
from ksq.safe_io import safe_write_text
from ksq.runtime_logging import get_logger
from ksq.feishu.rules import normalize_rule_id, public_rules
from ksq.feishu.submit import (
    maybe_submit_feishu_form,
    preview_feishu_form,
    should_submit_on_confirm,
    should_submit_on_human_prompt,
)
from ksq.web.logs_api import (
    LogServiceError,
    fetch_logs,
    inspect_container,
    restart_services,
)

ROBOT_SERVICE_ID = "0"
ROBOT_SERVICE_NAME = "robot_workspace_move_test"
_DEFAULT_KEYBOARD_DEVICE = "/dev/input/event1"
_DEFAULT_DASHBOARD_MODE = "test"
_RECOVERED_ORDER_TIMEZONE = timezone(timedelta(hours=8))
_KEYBOARD_DEVICE_RE = re.compile(r"^/dev/input/event\d+$")
_DASHBOARD_MODES = frozenset({"test", "prod"})
_FEISHU_LOG_CACHE_MAX_LINES = 20000
# A log tail is useful for recovering a process that restarted mid-order, but
# an old tail must never become a new queue lock when the active-order file is
# missing.  Robot workflows are normally measured in minutes; a half-hour
# window also tolerates a slow/manual step without reviving yesterday's task.
_LOG_ORDER_RECOVERY_MAX_AGE_SECONDS = 30 * 60
LOGGER = get_logger("dashboard")

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]"
)
_TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<body>.*)$"
)
_ITEM_TASK_RE = re.compile(
    r"MedicinePickUpTaskItem\(code=([^,\s]+),\s*task_id=([^,\s]+),\s*seq_id=([^)]+)\)"
)
_SEQ_ID_RE = re.compile(
    r"(?P<seq>\d+(?:\.\d+)?-"
    r"(?P<parent>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<code>\d+)-(?P<idx>\d+))",
    re.I,
)
# 新版机器人日志的 start process object 字典：{'code': sku_id, 'barcode': 69码, ...}
# 字段顺序与旧版不同，正则不假设相邻字段；barcode 单独提取（旧版没有该键）。
_START_OBJECT_RE = re.compile(
    r"start process object\s*\{[^}]*'code':\s*'([^']+)'[^}]*'location_code':\s*'([^']*)'"
)
_START_OBJECT_BARCODE_RE = re.compile(
    r"start process object\s*\{[^}]*'barcode':\s*'([^']+)'"
)
# 新版日志 item 行的编号是 sku_id（非纯数字），统一按非空白匹配。
_ITEM_START_RE = re.compile(r"\bitem\s+(\S+)\s+process start time", re.I)
_ITEM_END_RE = re.compile(r"\bitem\s+(\S+)\s+process end time", re.I)
_ITEM_DURATION_RE = re.compile(
    r"\bitem\s+(\S+)\s+process duration:\s*([0-9.]+)", re.I
)


def _barcode_aliases(lines: List[str], base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """sku_id → 69码 映射，来自 start process object 行（新版日志同时携带两者）。

    base 用于并入订单上已持久化的别名：起始行滚出日志窗口后，结束行仍能正确翻译。
    """
    aliases: Dict[str, str] = dict(base or {})
    for line in lines:
        match = _START_OBJECT_RE.search(line)
        if match is None:
            continue
        code = match.group(1).strip()
        barcode_match = _START_OBJECT_BARCODE_RE.search(line)
        barcode = barcode_match.group(1).strip() if barcode_match else ""
        if code and barcode and code != barcode:
            aliases[code] = barcode
    return aliases
_PLACE_SUCCESS_RE = re.compile(r"place object pipeline success", re.I)

# Align with PNP case_config speak texts (config.py): these prompts mean
# "human gate reached" — freeze order timer and submit Feishu here, not on key.
_CONFIRM_PATTERNS = (
    "请确认药品是否正确",
    "如正确请按回车键确认",
    "请按键盘确认",
    "放置流程失败，请人工协助",
    "请取走药品，进行打包",
    "取走药品后请人工打包",
    "等待键盘输入",
    "wait_for_key",
    "程序已暂停，等待按下目标按键",
    "等待按下目标按键",
)
_KEY_WAIT_RE = re.compile(
    r"(?:等待|请|暂停).*?(?:按下|按|输入).*?(?:目标|指定|任意)?(?:按键|键盘|回车|键)"
    r"|(?:wait(?:ing)?(?:\s+for)?|press).*?(?:target|specified|any|enter).*?key",
    re.I,
)
_ERROR_CONFIRM_PATTERNS = (
    "报错，请求人工处理",
    "工单已被取消",
    "工单已被人工抢占",
    "数据录入问题",
)
_PACK_CONFIRM_PATTERNS = (
    "请取走药品，进行打包",
    "取走药品后请人工打包",
    "程序已暂停，等待按下目标按键",
    "等待按下目标按键",
)
_TIMER_STOP_REASONS = frozenset(
    {"human_prompt", "confirm", "broker_ended", "order_ended"}
)
_RESUME_PATTERNS = (
    "确认成功，继续操作",
    "人工操作完成，继续",
    "无需确认，继续操作",
)
_FAIL_PATTERNS = (
    "packing task failed",
    "pick_up_object failed",
    "object is marked as unavailable",
    "find object and shelf failed",
    "not found in percept_pusher results",
    "scan object pipeline failed",
    "check scan object result failed",
)
_START_SPEAK = "开始处理商品"

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

_ACTIVE_ORDER_LOCK = Lock()
_SETTINGS_LOCK = Lock()
_SETTINGS_BACKUP_KEEP_DAYS = 2
_ACTIVE_ORDER: Optional[Dict[str, object]] = None
_ACTIVE_ORDER_LOADED = False
# Last persistence error, surfaced so a failing write is not silently invisible.
_ACTIVE_ORDER_SAVE_ERROR: Optional[str] = None
# Only one order may exist at a time.  Older state files may still contain
# ``queued_orders``; those entries are ignored by the single-order flow.
_ORDER_QUEUE_LIMIT = 1
_BROKER_TASK_IDENTITY_CACHE_TTL_SECONDS = 3.0
_BROKER_TASK_IDENTITY_CACHE_LOCK = Lock()
_BROKER_TASK_IDENTITY_CACHE: Dict[str, Tuple[float, str]] = {}
_DASHBOARD_MONITOR_TAIL = 2500
_DASHBOARD_MONITOR_ACTIVE_SECONDS = 1.0
_DASHBOARD_MONITOR_IDLE_SECONDS = 4.0
_DASHBOARD_REFRESH_LOCK = Lock()
_DASHBOARD_CACHE_LOCK = Lock()
_DASHBOARD_CACHE: Optional[Dict[str, object]] = None
_DASHBOARD_CACHE_GENERATION = 0
_DASHBOARD_MONITOR_THREAD: Optional[Thread] = None
_DASHBOARD_MONITOR_STOP: Optional[Event] = None


def _invalidate_dashboard_snapshot_cache() -> None:
    global _DASHBOARD_CACHE, _DASHBOARD_CACHE_GENERATION
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE = None
        _DASHBOARD_CACHE_GENERATION += 1


def active_order_save_error() -> Optional[str]:
    return _ACTIVE_ORDER_SAVE_ERROR


def _save_active_order_unlocked() -> None:
    """Persist current active order. Caller must hold _ACTIVE_ORDER_LOCK."""
    global _ACTIVE_ORDER_SAVE_ERROR
    path = DASHBOARD_ACTIVE_ORDER_FILE
    try:
        if _ACTIVE_ORDER is None:
            if path.is_file():
                # Bind-mounted single files cannot be unlinked from inside the
                # container; truncating to an empty object clears state too.
                try:
                    path.unlink()
                except OSError:
                    safe_write_text(path, "{}\n", backup=False)
            _ACTIVE_ORDER_SAVE_ERROR = None
            return
        safe_write_text(
            path,
            json.dumps(_ACTIVE_ORDER, ensure_ascii=False, indent=2) + "\n",
            backup=False,
        )
        _ACTIVE_ORDER_SAVE_ERROR = None
    except OSError as error:
        # In-memory state stays authoritative, but record why disk is stale.
        _ACTIVE_ORDER_SAVE_ERROR = str(error)
        LOGGER.error("活动订单持久化失败：%s", error, exc_info=True)
        return


def _ensure_active_order_loaded() -> None:
    """Load active order from disk once per process if memory is empty."""
    global _ACTIVE_ORDER, _ACTIVE_ORDER_LOADED
    if _ACTIVE_ORDER_LOADED:
        return
    _ACTIVE_ORDER_LOADED = True
    if _ACTIVE_ORDER is not None:
        return
    path = DASHBOARD_ACTIVE_ORDER_FILE
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    current = payload if (
        payload.get("task_id") or payload.get("items") or payload.get("lifecycle")
    ) else None
    # v1.3 and earlier could persist a second order in queued_orders.  The
    # single-order flow no longer queues, but a queued Broker order may be the
    # newest real order while the old active record is only a log projection.
    legacy_queue = payload.get("queued_orders")
    queued = (
        [item for item in legacy_queue if isinstance(item, dict)]
        if isinstance(legacy_queue, list)
        else []
    )
    current_lifecycle = current.get("lifecycle") if isinstance(current, dict) else None
    current_closed = bool(
        isinstance(current_lifecycle, dict)
        and current_lifecycle.get("closed")
    )
    if queued and (current is None or current_closed):
        _ACTIVE_ORDER = deepcopy(queued[0])
        # Drop the legacy queue so it cannot reappear after a later restart.
        _save_active_order_unlocked()
        return
    if current is not None:
        _ACTIVE_ORDER = current


def _dismissed_fingerprint_from_order(order: Optional[Dict[str, object]]) -> str:
    if not isinstance(order, dict):
        return ""
    ui = order.get("ui")
    if not isinstance(ui, dict):
        return ""
    return str(ui.get("dismissed_fingerprint") or "").strip()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


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


def _match_any(text: str, patterns: Tuple[str, ...]) -> Optional[str]:
    lower = text.lower()
    for pattern in patterns:
        if pattern.lower() in lower:
            return pattern
    return None


def _empty_item_state(code: str) -> Dict[str, object]:
    return {
        "code": code,
        "seq_id": "",
        "parent_task_id": "",
        "location_code": "",
        "status": "pending",
        "status_label": _STATUS_LABELS["pending"],
        "started_at": None,
        "await_at": None,
        "ended_at": None,
        "elapsed_to_await_seconds": None,
        "elapsed_seconds": None,
        "duration_seconds": None,
        "await_kind": "",
        "await_line": "",
        "start_line": "",
        "end_line": "",
        "events": [],
        "active": False,
    }


def _ensure_item(
    items: Dict[str, Dict[str, object]], code: str
) -> Dict[str, object]:
    key = str(code or "").strip()
    if not key:
        raise ValueError("商品编码不能为空。")
    if key not in items:
        items[key] = _empty_item_state(key)
    return items[key]


def _append_event(
    item: Dict[str, object], kind: str, ts: Optional[datetime], text: str
) -> None:
    events: List[Dict[str, object]] = item["events"]  # type: ignore[assignment]
    events.append(
        {
            "kind": kind,
            "at": _ts_to_iso(ts),
            "text": text[:240],
            "code": item.get("code") or "",
        }
    )
    if len(events) > 30:
        del events[:-30]


_LIVE_ITEM_STATUSES = frozenset(
    {"started", "processing", "await_confirm", "await_error"}
)


def _finalize_item_timing(item: Dict[str, object], now: datetime) -> None:
    start_dt = item.get("_started_dt")
    await_dt = item.get("_await_dt")
    end_dt = item.get("_ended_dt")
    if not isinstance(start_dt, datetime):
        start_dt = _parse_ts(str(item.get("started_at") or ""))
    if not isinstance(await_dt, datetime):
        await_dt = None
    if not isinstance(end_dt, datetime):
        end_dt = None
    status = str(item.get("status") or "pending")
    timing_end = await_dt or end_dt
    if status in _LIVE_ITEM_STATUSES:
        timing_end = now
    # Do not wipe timestamps when the start line has rolled out of the log window.
    if start_dt is not None:
        item["started_at"] = _ts_to_iso(start_dt)
    if await_dt is not None:
        item["await_at"] = _ts_to_iso(await_dt)
    if end_dt is not None:
        item["ended_at"] = _ts_to_iso(end_dt)
    item["elapsed_to_await_seconds"] = _duration_seconds(start_dt, await_dt)
    if start_dt is not None and timing_end is not None:
        item["elapsed_seconds"] = _duration_seconds(start_dt, timing_end)
    if item.get("duration_seconds") is None and start_dt is not None and end_dt is not None:
        item["duration_seconds"] = _duration_seconds(start_dt, end_dt)
    item["status_label"] = _STATUS_LABELS.get(status, status)
    item.pop("_started_dt", None)
    item.pop("_await_dt", None)
    item.pop("_ended_dt", None)


def _refresh_live_elapsed(
    tasks: List[Dict[str, object]], polled_at: Optional[str]
) -> None:
    """Recompute in-flight item timers from started_at.

    Needed when docker log tail drops the start line: merge keeps remembered
    status/started_at but would otherwise leave elapsed_seconds frozen.
    """
    now = _parse_ts(str(polled_at or "")) or datetime.now(timezone.utc)
    for task in tasks:
        status = str(task.get("status") or "pending")
        if status not in _LIVE_ITEM_STATUSES:
            continue
        start_dt = _parse_ts(str(task.get("started_at") or ""))
        if start_dt is None:
            continue
        task["elapsed_seconds"] = _duration_seconds(start_dt, now)
        task["active"] = True
        task["needs_confirm"] = status in {"await_confirm", "await_error"}
        task["status_label"] = _STATUS_LABELS.get(status, status)


def _discover_log_tasks(
    raw_logs: str,
    aliases: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, List[str]], Dict[str, datetime]]:
    """Return (latest_task_id, task_id -> codes, task_id -> last timestamp)."""
    text = _strip_ansi(raw_logs)
    if aliases is None:
        aliases = _barcode_aliases(text.splitlines())
    latest_task_id = ""
    codes_by_task: Dict[str, List[str]] = {}
    seen_by_task: Dict[str, set] = {}
    last_seen_by_task: Dict[str, datetime] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _TS_RE.match(line)
        body = match.group("body") if match is not None else line
        line_ts = _parse_ts(match.group("ts")) if match is not None else None
        item_task = _ITEM_TASK_RE.search(body or "")
        parent = ""
        code = ""
        if item_task is not None:
            code = item_task.group(1).strip()
            parent = item_task.group(2).strip()
        else:
            seq_match = _SEQ_ID_RE.search(body or "")
            if seq_match is None:
                continue
            code = seq_match.group("code").strip()
            parent = seq_match.group("parent").strip()
        code = aliases.get(code, code)
        if not parent or not code:
            continue
        latest_task_id = parent
        if line_ts is not None:
            if line_ts.tzinfo is None:
                line_ts = line_ts.replace(tzinfo=timezone.utc)
            previous_seen = last_seen_by_task.get(parent)
            if previous_seen is None or line_ts > previous_seen:
                last_seen_by_task[parent] = line_ts
        bucket = codes_by_task.setdefault(parent, [])
        seen = seen_by_task.setdefault(parent, set())
        if code not in seen:
            seen.add(code)
            bucket.append(code)
    return latest_task_id, codes_by_task, last_seen_by_task


def _stale_log_tasks(
    order: Optional[Dict[str, object]],
    last_seen_by_task: Optional[Dict[str, datetime]],
) -> frozenset:
    """Log tasks whose activity stopped before the current order was registered.

    Re-ordering the same SKUs (e.g. 测试下单「已下单」再次下单) creates a new
    Broker task while the previous order's robot task lines are still in the
    log tail.  Those stale tasks must not serve as the new order's log scope.
    Tasks without timestamp information keep legacy behaviour (never stale).
    """
    if order is None or not last_seen_by_task:
        return frozenset()
    registered_at = _parse_ts(str(order.get("registered_at") or ""))
    if registered_at is None:
        return frozenset()
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    active_ids = {
        str(order.get(key) or "").strip()
        for key in ("task_id", "robot_task_id")
        if str(order.get(key) or "").strip()
    }
    stale = set()
    for task_id, seen in last_seen_by_task.items():
        if not task_id or task_id in active_ids or seen is None:
            continue
        seen_dt = (
            seen if seen.tzinfo is not None else seen.replace(tzinfo=timezone.utc)
        )
        if seen_dt < registered_at:
            stale.add(task_id)
    return frozenset(stale)


def _has_recent_log_activity(
    last_seen_by_task: Optional[Mapping[str, datetime]],
    now: Optional[datetime] = None,
) -> bool:
    """Whether a timestamped log tail can safely recover an active order."""
    if not last_seen_by_task:
        return False
    timestamps = [value for value in last_seen_by_task.values() if isinstance(value, datetime)]
    if not timestamps:
        # Untimestamped historical logs cannot prove that a task is current.
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    latest = max(
        value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        for value in timestamps
    )
    age = (current - latest).total_seconds()
    return age <= _LOG_ORDER_RECOVERY_MAX_AGE_SECONDS


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
    return set_active_order(
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
    existing = _order_item_codes(order)
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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is not None and str(_ACTIVE_ORDER.get("task_id") or "") == str(
            order.get("task_id") or ""
        ):
            _ACTIVE_ORDER["items"] = deepcopy(items)
            _ACTIVE_ORDER["item_count"] = len(items)
    return updated


def _merged_code_aliases(
    order: Optional[Dict[str, object]], raw_logs: str
) -> Dict[str, str]:
    """sku_id → 69码 别名：订单上已持久化的 ∪ 当前日志窗口新学到的。"""
    persisted: Dict[str, str] = {}
    if isinstance(order, dict):
        for item in order.get("items") or []:
            if not isinstance(item, dict):
                continue
            sku_id = str(item.get("sku_id") or "").strip()
            barcode = str(item.get("barcode") or "").strip()
            if sku_id and barcode:
                persisted[sku_id] = barcode
        raw = order.get("code_aliases")
        if isinstance(raw, dict):
            persisted.update({str(k): str(v) for k, v in raw.items()})
    merged = dict(persisted)
    merged.update(_barcode_aliases(_strip_ansi(raw_logs).splitlines()))
    return merged


def _persist_code_aliases(order: Optional[Dict[str, object]], aliases: Dict[str, str]) -> None:
    """把别名写回当前工单持久化，滚出日志窗口后仍可用。"""
    if not isinstance(order, dict) or not aliases:
        return
    task_id = str(order.get("task_id") or "").strip()
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is None:
            return
        if task_id and str(_ACTIVE_ORDER.get("task_id") or "") != task_id:
            return
        existing = _ACTIVE_ORDER.get("code_aliases")
        if isinstance(existing, dict) and all(
            existing.get(key) == value for key, value in aliases.items()
        ):
            return
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(aliases)
        _ACTIVE_ORDER["code_aliases"] = merged
        _save_active_order_unlocked()


def _resolve_active_order(
    order: Optional[Dict[str, object]], raw_logs: str
) -> Optional[Dict[str, object]]:
    """
    Keep showing the current order until the next order starts, then refresh.
    Same task_id may gain more SKUs over time — those are expanded separately.
    """
    latest_task_id, codes_by_task, last_seen_by_task = _discover_log_tasks(raw_logs)
    if order is None:
        if not latest_task_id or not _has_recent_log_activity(last_seen_by_task):
            return None
        return _build_order_from_log_task(
            latest_task_id, codes_by_task.get(latest_task_id, [])
        )

    active_task_id = str(order.get("task_id") or "").strip()
    lifecycle = order.get("lifecycle") if isinstance(order.get("lifecycle"), dict) else {}
    source = str(order.get("source") or "").strip()
    ended = bool(lifecycle.get("ended") or lifecycle.get("closed"))
    matched = _match_log_task_for_order(
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
        order_no = order_no or _order_no_from_task_dict(node)
        platform_no = platform_no or _platform_order_no_from_task_dict(node)
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
            parsed = _parse_ts(str(node.get(field) or ""))
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                # Broker list times are explicitly requested in Asia/Shanghai.
                parsed = parsed.replace(tzinfo=_RECOVERED_ORDER_TIMEZONE)
            return str(_ts_to_iso(parsed) or "")
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
            from ksq.web import order_api

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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is not None:
            active_id = str(_ACTIVE_ORDER.get("task_id") or "").strip()
            active_order_no = str(_ACTIVE_ORDER.get("order_no") or "").strip()
            if active_id == current_id or active_order_no == order_no:
                _ACTIVE_ORDER["task_id"] = broker_id
                _ACTIVE_ORDER["robot_task_id"] = current_id
                _ACTIVE_ORDER["source"] = "order"
                _save_active_order_unlocked()
                updated = deepcopy(_ACTIVE_ORDER)
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
    current_id = str(order.get("task_id") or "").strip() if isinstance(order, dict) else ""
    current_broker: Dict[str, object] = {}
    if current_id:
        current_broker = _fetch_broker_order(current_id, mode)
        if current_broker.get("ok") and not current_broker.get("terminal"):
            return order
        if (
            not current_broker.get("ok")
            and str(order.get("source") or "").strip() != "log"
        ):
            return order
    try:
        from ksq.web import order_api

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
            | _BROKER_MANUAL_HELD
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
        candidate = _build_active_order(
            {
                "task_id": task_id,
                "order_no": promoted_order_no,
                "platform_order_no": promoted_platform_no,
                "items": items,
                "source": "order",
            },
            registered_at=_recovered_order_registered_at(task),
        )
        with _ACTIVE_ORDER_LOCK:
            global _ACTIVE_ORDER
            active_id = (
                ""
                if _ACTIVE_ORDER is None
                else str(_ACTIVE_ORDER.get("task_id") or "").strip()
            )
            # A create request may replace the active order while Broker list is
            # in flight. Never let that older refresh overwrite the new order.
            if active_id != current_id:
                return deepcopy(_ACTIVE_ORDER) if _ACTIVE_ORDER is not None else None
            _ACTIVE_ORDER = candidate
            _save_active_order_unlocked()
        return candidate
    return order


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


def _match_log_task_for_order(
    order: Optional[Dict[str, object]],
    codes_by_task: Dict[str, List[str]],
    latest_task_id: str,
    last_seen_by_task: Optional[Dict[str, datetime]] = None,
) -> str:
    """Map UI/broker task_id to the robot log task_id that actually handles the SKUs.

    Tasks that went silent before this order was registered belong to a
    previous order (same SKUs re-ordered) and are never matched.
    """
    if order is None:
        return latest_task_id
    active = str(order.get("task_id") or "").strip()
    order_codes = _order_item_codes(order)
    stale = _stale_log_tasks(order, last_seen_by_task)
    if latest_task_id and order_codes and latest_task_id not in stale:
        latest_codes = set(codes_by_task.get(latest_task_id, []))
        if order_codes & latest_codes:
            return latest_task_id
    if active and active in codes_by_task:
        return active
    best = ""
    best_n = 0
    for task_id, codes in codes_by_task.items():
        if task_id in stale:
            continue
        overlap = len(order_codes & set(codes))
        if overlap > best_n:
            best_n = overlap
            best = task_id
    if best:
        return best
    return active or latest_task_id


def parse_robot_log_text(
    raw_logs: str,
    focus_task_id: str = "",
    extra_allowed_codes: Optional[set] = None,
    stale_task_ids: Optional[frozenset] = None,
    aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    text = _strip_ansi(raw_logs)
    lines = [line for line in text.splitlines() if line.strip()]
    # 新版机器人日志的商品编号是 sku_id；start process object 行同时给出 69码，
    # 先把 sku_id → 69码 映射建好，后续所有编号统一翻译成订单使用的 69码。
    aliases = _barcode_aliases(lines) if aliases is None else aliases
    focus_task_id = str(focus_task_id or "").strip()
    _latest_task_id, codes_by_task, _last_seen = _discover_log_tasks(
        raw_logs, aliases=aliases
    )
    allowed_codes = set(codes_by_task.get(focus_task_id, [])) if focus_task_id else set()
    if extra_allowed_codes:
        allowed_codes.update(str(code).strip() for code in extra_allowed_codes if str(code).strip())
    stale_tasks = frozenset(stale_task_ids or ())
    items: Dict[str, Dict[str, object]] = {}
    events: List[Dict[str, object]] = []
    parent_task_id = focus_task_id
    current_code = ""
    scope_task_id = ""
    saw_any_task = False
    in_focus_scope = not bool(focus_task_id)
    # Codes seen on stale (previous-order) / fresh task marker lines.  Lines
    # about a code that only appears under a stale task belong to the previous
    # order and must not update the new order's state.
    stale_marker_codes: set = set()
    fresh_marker_codes: set = set()
    order_events: List[Dict[str, object]] = []
    human_confirm_seen = False
    human_confirm_kind = ""
    human_confirm_closed = False
    human_confirm_at: Optional[datetime] = None
    order_await_active = False
    order_await_kind = ""
    order_await_line = ""
    order_await_at: Optional[datetime] = None

    def _clear_order_await() -> None:
        nonlocal order_await_active, order_await_kind, order_await_line, order_await_at
        order_await_active = False
        order_await_kind = ""
        order_await_line = ""
        order_await_at = None

    def _in_scope(task_id: str) -> bool:
        if not focus_task_id:
            return True
        return bool(task_id) and task_id == focus_task_id

    def _accept_code(code: str) -> bool:
        if not code:
            return False
        if code in allowed_codes:
            if code in stale_marker_codes and code not in fresh_marker_codes:
                return False
            return True
        if not focus_task_id:
            return True
        # No task markers left in the log window: assume lines belong to focus order.
        if not saw_any_task:
            return True
        return in_focus_scope

    for line in lines:
        match = _TS_RE.match(line)
        if match is None:
            body = line
            ts = None
        else:
            ts = _parse_ts(match.group("ts"))
            body = match.group("body") or ""

        item_task = _ITEM_TASK_RE.search(body)
        if item_task is not None:
            code = aliases.get(item_task.group(1).strip(), item_task.group(1).strip())
            parent = item_task.group(2).strip()
            seq_id = item_task.group(3).strip()
            saw_any_task = True
            scope_task_id = parent or scope_task_id
            marker_stale = bool(parent) and parent in stale_tasks
            in_focus_scope = _in_scope(parent) or (
                code in allowed_codes and not marker_stale
            )
            if not in_focus_scope:
                if marker_stale and code:
                    stale_marker_codes.add(code)
                current_code = ""
                continue
            fresh_marker_codes.add(code)
            parent_task_id = parent or parent_task_id
            item = _ensure_item(items, code)
            item["parent_task_id"] = parent
            item["seq_id"] = seq_id
            current_code = code

        seq_match = _SEQ_ID_RE.search(body)
        if seq_match is not None:
            code = aliases.get(seq_match.group("code"), seq_match.group("code"))
            parent = seq_match.group("parent")
            seq_id = seq_match.group("seq")
            saw_any_task = True
            scope_task_id = parent or scope_task_id
            marker_stale = bool(parent) and parent in stale_tasks
            in_focus_scope = _in_scope(parent) or (
                code in allowed_codes and not marker_stale
            )
            if not in_focus_scope:
                if marker_stale and code:
                    stale_marker_codes.add(code)
                current_code = ""
                continue
            fresh_marker_codes.add(code)
            parent_task_id = parent or parent_task_id
            item = _ensure_item(items, code)
            item["parent_task_id"] = parent
            item["seq_id"] = seq_id
            current_code = code

        start_object = _START_OBJECT_RE.search(body)
        if start_object is not None:
            code = aliases.get(start_object.group(1).strip(), start_object.group(1).strip())
            if not _accept_code(code):
                current_code = ""
                continue
            location = start_object.group(2).strip()
            item = _ensure_item(items, code)
            item["location_code"] = location
            # Do not rewind a finished item if an older start line is still in the tail.
            if item.get("status") not in {"success", "failed"}:
                # A new SKU started: drop stale order-level pack wait from prior item.
                _clear_order_await()
                if human_confirm_kind == "pack":
                    human_confirm_kind = ""
                    human_confirm_closed = False
                item["status"] = "started"
                item["_started_dt"] = ts or item.get("_started_dt") or datetime.now(
                    timezone.utc
                )
                item["_await_dt"] = None
                item["_ended_dt"] = None
                item["await_kind"] = ""
                item["await_line"] = ""
                item["end_line"] = ""
                item["start_line"] = body.strip()[:240]
                _append_event(item, "started", ts, item["start_line"])  # type: ignore[arg-type]
                order_events.append(
                    {
                        "kind": "started",
                        "at": _ts_to_iso(ts),
                        "text": item["start_line"],
                        "code": code,
                    }
                )
            current_code = code
            continue

        item_start = _ITEM_START_RE.search(body)
        if item_start is not None:
            code = aliases.get(item_start.group(1).strip(), item_start.group(1).strip())
            if not _accept_code(code):
                current_code = ""
                continue
            item = _ensure_item(items, code)
            if item.get("status") == "pending":
                item["status"] = "started"
            if item.get("status") not in {"success", "failed"}:
                _clear_order_await()
                if human_confirm_kind == "pack":
                    human_confirm_kind = ""
                    human_confirm_closed = False
                item["_started_dt"] = ts or item.get("_started_dt") or datetime.now(
                    timezone.utc
                )
                _append_event(item, "started", ts, body.strip()[:240])
            current_code = code
            continue

        if _START_SPEAK in body:
            code = current_code
            if code and _accept_code(code):
                item = _ensure_item(items, code)
                if item.get("status") not in {"success", "failed"}:
                    _clear_order_await()
                    if human_confirm_kind == "pack":
                        human_confirm_kind = ""
                        human_confirm_closed = False
                    if item.get("status") == "pending":
                        item["status"] = "started"
                    if item.get("_started_dt") is None:
                        item["_started_dt"] = ts or datetime.now(timezone.utc)
                    item["start_line"] = body.strip()[:240]
                    item["status"] = "started"
                    _append_event(item, "started", ts, item["start_line"])  # type: ignore[arg-type]
                    order_events.append(
                        {
                            "kind": "started",
                            "at": _ts_to_iso(ts),
                            "text": item["start_line"],
                            "code": code,
                        }
                    )
            continue

        resume_hit = _match_any(body, _RESUME_PATTERNS)
        if resume_hit:
            resume_code = current_code
            if not resume_code:
                for code, candidate in reversed(list(items.items())):
                    if candidate.get("status") in {"await_confirm", "await_error"}:
                        resume_code = code
                        break
            # Order-level packing / key-wait confirm (no active item await).
            if order_await_active or (human_confirm_seen and not resume_code):
                order_await_active = False
                order_await_kind = ""
                order_await_line = ""
                human_confirm_closed = True
                human_confirm_kind = human_confirm_kind or "pack"
                text = body.strip()[:240]
                order_events.append(
                    {
                        "kind": "success",
                        "at": _ts_to_iso(ts),
                        "text": text,
                        "code": resume_code or "",
                    }
                )
                if resume_code:
                    current_code = resume_code
                continue
            if resume_code:
                item = _ensure_item(items, resume_code)
                previous = str(item.get("status") or "")
                if previous in {"await_confirm", "await_error"} or human_confirm_seen:
                    # Entity/virtual key after order-end human confirm closes the order.
                    if previous == "await_error" or item.get("await_kind") == "error" or human_confirm_kind == "error":
                        item["status"] = "failed"
                        item["_ended_dt"] = ts or datetime.now(timezone.utc)
                        event_kind = "failed"
                        human_confirm_kind = "error"
                    else:
                        item["status"] = "success"
                        item["_ended_dt"] = ts or datetime.now(timezone.utc)
                        event_kind = "success"
                        human_confirm_kind = human_confirm_kind or "confirm"
                    item["await_kind"] = ""
                    item["await_line"] = ""
                    human_confirm_closed = True
                    order_await_active = False
                    order_await_kind = ""
                    order_await_line = ""
                    text = body.strip()[:240]
                    _append_event(item, event_kind, ts, text)
                    order_events.append(
                        {
                            "kind": event_kind,
                            "at": _ts_to_iso(ts),
                            "text": text,
                            "code": resume_code,
                        }
                    )
                    current_code = resume_code
                elif previous in {"started", "processing"}:
                    item["status"] = "processing"
                    text = body.strip()[:240]
                    _append_event(item, "processing", ts, text)
                    current_code = resume_code
                continue

        key_wait_hit = _KEY_WAIT_RE.search(body)
        confirm_hit = _match_any(body, _CONFIRM_PATTERNS) or key_wait_hit
        error_hit = _match_any(body, _ERROR_CONFIRM_PATTERNS)
        pack_hit = _match_any(body, _PACK_CONFIRM_PATTERNS) or key_wait_hit
        if confirm_hit or error_hit:
            if stale_marker_codes and not fresh_marker_codes:
                # Only a previous order's execution is visible in the tail; its
                # confirm/pack prompts must not surface on the new order.
                continue
            # Human confirm / key-wait must always surface, even when broker task_id
            # differs from the robot log task_id (manual transfer, etc.).
            text = body.strip()[:240]
            item = None
            if current_code and _accept_code(current_code):
                item = _ensure_item(items, current_code)
            # Item-level confirm only while the item is still in progress.
            if (
                item is not None
                and item.get("status") not in {"success", "failed", "skipped"}
            ):
                if item.get("_await_dt") is None:
                    item["_await_dt"] = ts or datetime.now(timezone.utc)
                item["await_line"] = text
                previous = str(item.get("status") or "")
                if error_hit or previous == "await_error":
                    item["await_kind"] = "error"
                    item["status"] = "await_error"
                    human_confirm_kind = "error"
                else:
                    item["await_kind"] = "confirm"
                    item["status"] = "await_confirm"
                    human_confirm_kind = "confirm"
                human_confirm_seen = True
                human_confirm_closed = False
                human_confirm_at = item.get("_await_dt")  # type: ignore[assignment]
                order_await_active = False
                order_await_kind = ""
                order_await_line = ""
                _append_event(item, str(item["status"]), ts, text)
                order_events.append(
                    {
                        "kind": item["status"],
                        "at": _ts_to_iso(ts),
                        "text": text,
                        "code": current_code,
                    }
                )
            else:
                # Packing / key-wait / error after item finished: order-level confirm.
                if error_hit:
                    order_await_kind = "error"
                    human_confirm_kind = "error"
                    event_kind = "await_error"
                elif order_await_kind == "error" and pack_hit:
                    # Keep error severity when key-wait line follows error speak.
                    human_confirm_kind = "error"
                    event_kind = "await_error"
                else:
                    order_await_kind = "pack" if pack_hit else "confirm"
                    human_confirm_kind = order_await_kind
                    event_kind = "await_confirm"
                order_await_active = True
                order_await_line = text
                if order_await_at is None:
                    order_await_at = ts or datetime.now(timezone.utc)
                human_confirm_seen = True
                human_confirm_closed = False
                if human_confirm_at is None:
                    human_confirm_at = order_await_at
                order_events.append(
                    {
                        "kind": event_kind,
                        "at": _ts_to_iso(ts),
                        "text": text,
                        "code": current_code or "",
                    }
                )
            continue

        item_end = _ITEM_END_RE.search(body)
        if item_end is not None:
            code = aliases.get(item_end.group(1).strip(), item_end.group(1).strip())
            if not _accept_code(code):
                continue
            item = _ensure_item(items, code)
            item["_ended_dt"] = ts or datetime.now(timezone.utc)
            item["end_line"] = body.strip()[:240]
            if item.get("status") not in {"failed"}:
                item["status"] = "success"
            _append_event(item, "success", ts, str(item["end_line"]))
            order_events.append(
                {
                    "kind": "success",
                    "at": _ts_to_iso(ts),
                    "text": item["end_line"],
                    "code": code,
                }
            )
            if current_code == code:
                current_code = ""
            continue

        duration_match = _ITEM_DURATION_RE.search(body)
        if duration_match is not None:
            code = aliases.get(duration_match.group(1).strip(), duration_match.group(1).strip())
            if not _accept_code(code):
                continue
            item = _ensure_item(items, code)
            try:
                item["duration_seconds"] = float(duration_match.group(2))
            except ValueError:
                pass
            continue

        if _PLACE_SUCCESS_RE.search(body) and current_code and _accept_code(current_code):
            item = _ensure_item(items, current_code)
            if item.get("status") not in {"failed", "success"}:
                item["status"] = "processing"
                _append_event(item, "processing", ts, body.strip()[:240])
            continue

        fail_hit = _match_any(body, _FAIL_PATTERNS)
        if fail_hit and current_code and _accept_code(current_code):
            item = _ensure_item(items, current_code)
            item["status"] = "failed"
            item["_ended_dt"] = ts or datetime.now(timezone.utc)
            item["end_line"] = body.strip()[:240]
            _append_event(item, "failed", ts, str(item["end_line"]))
            order_events.append(
                {
                    "kind": "failed",
                    "at": _ts_to_iso(ts),
                    "text": item["end_line"],
                    "code": current_code,
                }
            )
            continue

        if current_code and _accept_code(current_code):
            item = _ensure_item(items, current_code)
            if item.get("status") == "started":
                item["status"] = "processing"

    now = datetime.now(timezone.utc)
    for item in items.values():
        status = str(item.get("status") or "pending")
        if status == "started":
            # Keep started if only speak happened; else processing is set above.
            pass
        _finalize_item_timing(item, now)
        item["active"] = status in {
            "started",
            "processing",
            "await_confirm",
            "await_error",
        }
        item["needs_confirm"] = status in {"await_confirm", "await_error"}
        item["events"] = list(item["events"])[-12:]  # type: ignore[index]

    active_items = [
        item
        for item in items.values()
        if item.get("active")
    ]
    active_item = active_items[-1] if active_items else None
    if active_item is None and items:
        # Prefer latest non-pending
        ranked = sorted(
            items.values(),
            key=lambda row: str(row.get("started_at") or row.get("ended_at") or ""),
        )
        for row in reversed(ranked):
            if row.get("status") != "pending":
                active_item = row
                break

    statuses = [str(item.get("status") or "pending") for item in items.values()]
    if order_await_active and order_await_kind == "error":
        aggregate = "await_error"
    elif order_await_active:
        aggregate = "await_confirm"
    elif any(status in {"await_error"} for status in statuses):
        aggregate = "await_error"
    elif any(status in {"await_confirm"} for status in statuses):
        aggregate = "await_confirm"
    elif any(status in {"started", "processing"} for status in statuses):
        aggregate = "processing"
    elif statuses and all(status == "success" for status in statuses):
        aggregate = "success"
    elif any(status == "failed" for status in statuses):
        aggregate = "failed"
    elif statuses and all(status == "pending" for status in statuses):
        aggregate = "pending"
    else:
        aggregate = "idle" if not statuses else "processing"

    item_needs_confirm = any(item.get("needs_confirm") for item in items.values())
    needs_confirm = item_needs_confirm or bool(order_await_active)
    await_item = next(
        (
            item
            for item in items.values()
            if item.get("status") in {"await_confirm", "await_error"}
        ),
        None,
    )
    focus = await_item or active_item
    if await_item is not None:
        human_confirm_kind = str(await_item.get("await_kind") or human_confirm_kind or "confirm")
        human_confirm_seen = True
        human_confirm_closed = False
    if order_await_active:
        human_confirm_seen = True
        human_confirm_closed = False
        if not human_confirm_kind:
            human_confirm_kind = order_await_kind or "pack"

    await_line = ""
    if await_item is not None:
        await_line = str(await_item.get("await_line") or "")
    if order_await_active and order_await_line:
        await_line = order_await_line
    await_kind = ""
    if await_item is not None:
        await_kind = str(await_item.get("await_kind") or "")
    if order_await_active:
        await_kind = order_await_kind or await_kind or "pack"
    await_at = None if await_item is None else await_item.get("await_at")
    if order_await_active:
        await_at = _ts_to_iso(order_await_at) or await_at

    return {
        "status": aggregate if aggregate != "pending" else "idle",
        "status_label": _STATUS_LABELS.get(
            aggregate if aggregate != "pending" else "idle",
            aggregate,
        ),
        "needs_confirm": needs_confirm,
        "await_kind": await_kind,
        "order_await_active": bool(order_await_active),
        "human_confirm_seen": human_confirm_seen,
        "human_confirm_kind": human_confirm_kind,
        "human_confirm_closed": human_confirm_closed and not needs_confirm,
        "human_confirm_at": _ts_to_iso(
            human_confirm_at if isinstance(human_confirm_at, datetime) else None
        ),
        "task_id": parent_task_id
        or ("" if focus is None else str(focus.get("parent_task_id") or "")),
        "object_hint": "" if focus is None else str(focus.get("code") or ""),
        "started_at": None if focus is None else focus.get("started_at"),
        "await_at": await_at if order_await_active else (
            None if focus is None else focus.get("await_at")
        ),
        "ended_at": None if focus is None else focus.get("ended_at"),
        "elapsed_to_await_seconds": None
        if focus is None
        else focus.get("elapsed_to_await_seconds"),
        "elapsed_seconds": None if focus is None else focus.get("elapsed_seconds"),
        "start_line": "" if focus is None else focus.get("start_line") or "",
        "await_line": await_line,
        "end_line": "" if focus is None else focus.get("end_line") or "",
        "events": order_events[-30:],
        "item_states": items,
        "active_code": "" if focus is None else str(focus.get("code") or ""),
        "current_item": None if focus is None else deepcopy(focus),
    }


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


def set_active_order(
    payload: Dict[str, object], *, registered_at: object = ""
) -> Dict[str, object]:
    order = _build_active_order(payload, registered_at=registered_at)
    global _ACTIVE_ORDER, _ACTIVE_ORDER_LOADED
    with _ACTIVE_ORDER_LOCK:
        _ACTIVE_ORDER_LOADED = True
        _ACTIVE_ORDER = order
        _save_active_order_unlocked()
    _invalidate_dashboard_snapshot_cache()
    return deepcopy(order)


def _order_is_queue_terminal(order: Optional[Dict[str, object]]) -> bool:
    if not isinstance(order, dict):
        return False
    lifecycle = order.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return str(lifecycle.get("broker_status") or "").strip() in _BROKER_ORDER_TERMINAL


def order_queue_status() -> Dict[str, object]:
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        current = _ACTIVE_ORDER
        return {
            "capacity": _ORDER_QUEUE_LIMIT,
            "total": 1 if isinstance(current, dict) else 0,
            "queued_count": 0,
            "full": isinstance(current, dict) and not _order_is_queue_terminal(current),
            "queued": [],
        }


def ensure_order_queue_capacity() -> None:
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is None:
            return
        if _order_is_queue_terminal(_ACTIVE_ORDER):
            return
        raise ValueError("上一单尚未完成，请等待上一单完成后再下单。")


def register_created_order(
    task_id: str, request_body: Dict[str, object], source: str
) -> Dict[str, object]:
    """Register a Broker-created order as the only active order."""
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
    candidate = _build_active_order(payload)
    global _ACTIVE_ORDER, _ACTIVE_ORDER_LOADED
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        _ACTIVE_ORDER_LOADED = True
        active_id = "" if _ACTIVE_ORDER is None else str(_ACTIVE_ORDER.get("task_id") or "")
        if active_id and active_id == task_id:
            result = deepcopy(_ACTIVE_ORDER)
            result["queue_position"] = 0
            return result
        if _ACTIVE_ORDER is not None and not _order_is_queue_terminal(_ACTIVE_ORDER):
            raise ValueError("上一单尚未完成，请等待上一单完成后再下单。")
        _ACTIVE_ORDER = candidate
        _save_active_order_unlocked()
    _invalidate_dashboard_snapshot_cache()
    result = deepcopy(candidate)
    # Keep the legacy fields so older clients can consume the response while
    # the server no longer creates a waiting order.
    result["queue_position"] = 0
    result["queued"] = False
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
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is None:
            return None
        return deepcopy(_ACTIVE_ORDER)


def dismiss_await(fingerprint: object) -> Dict[str, object]:
    """Shared across devices: mark confirm modal as dismissed for this await."""
    value = str(fingerprint or "").strip()
    if not value:
        raise ValueError("fingerprint 不能为空。")
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is None:
            raise ValueError("当前没有活动工单。")
        ui = _ACTIVE_ORDER.get("ui")
        if not isinstance(ui, dict):
            ui = {}
            _ACTIVE_ORDER["ui"] = ui
        ui["dismissed_fingerprint"] = value
        _save_active_order_unlocked()
    _invalidate_dashboard_snapshot_cache()
    return {"ok": True, "dismissed_fingerprint": value}


_TERMINAL_ITEM_STATUSES = frozenset({"success", "failed", "skipped"})


def active_order_blocking_keys() -> List[str]:
    """SKU keys tied to the current order (multi-device guard)."""
    current = get_active_order()
    if current is None:
        return []
    orders = [current]
    keys: List[str] = []
    seen: set = set()
    for order in orders:
        life = order.get("lifecycle")
        if isinstance(life, dict) and _order_is_queue_terminal(order):
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
            if status in _TERMINAL_ITEM_STATUSES:
                continue
            for value in (code, item_id, str(raw.get("barcode") or "").strip()):
                if value and value not in seen:
                    seen.add(value)
                    keys.append(value)
    return keys


def active_order_requires_manual_completion() -> bool:
    """Whether a previous key prompt is still unresolved for the active order.

    仅单品级日志信号（item_states 中的待确认/报错）参与；订单 lifecycle 已
    完全由 Broker 状态驱动，不再引用日志结束标记。
    """
    order = get_active_order()
    if not isinstance(order, dict) or _order_is_queue_terminal(order):
        return False
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


def active_order_blocks_new_order() -> bool:
    """Whether any non-terminal active order must finish before a new one."""
    order = get_active_order()
    return isinstance(order, dict) and not _order_is_queue_terminal(order)


def resolve_dashboard_mode(mode: object) -> str:
    value = str(mode or "").strip().lower()
    if value in {"test", "prod"}:
        return value
    settings_mode = str(load_dashboard_settings().get("mode") or _DEFAULT_DASHBOARD_MODE)
    return "prod" if settings_mode == "prod" else "test"


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


# Broker 任务详情每次快照都实时拉一次（云端 HTTP 调用），是轮询延迟的主要来源。
# 同一任务 2 秒内视为新鲜：播报/确认这类日志驱动的交互得以即时弹出，工单状态
# 芯片最多滞后 2 秒，可接受。
_BROKER_ORDER_CACHE_TTL_SECONDS = 2.0
_BROKER_ORDER_CACHE: Dict[str, Tuple[float, Dict[str, object]]] = {}
_BROKER_ORDER_CACHE_LOCK = Lock()


def invalidate_broker_order_cache(task_id: str) -> None:
    """写操作成功后丢弃 Broker 详情缓存，下一次轮询立即重拉。

    传入具体 task_id 时按任务精准失效；传空串时全量清空（按 order_no
    直发的操作无法反查 task_id，只能整体作废）。
    """
    task_id = str(task_id or "").strip()
    with _BROKER_ORDER_CACHE_LOCK:
        if not task_id:
            _BROKER_ORDER_CACHE.clear()
            return
        stale = [key for key in _BROKER_ORDER_CACHE if key.endswith("|" + task_id)]
        for key in stale:
            _BROKER_ORDER_CACHE.pop(key, None)


def _fetch_broker_order(task_id: str, mode: str = "test") -> Dict[str, object]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "无 task_id"}
    cache_key = "%s|%s" % (mode, task_id)
    now = time.monotonic()
    with _BROKER_ORDER_CACHE_LOCK:
        cached = _BROKER_ORDER_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _BROKER_ORDER_CACHE_TTL_SECONDS:
            return deepcopy(cached[1])
    try:
        from ksq.order.broker import OrderBrokerError
        from ksq.web import order_api

        config_file = (
            ORDER_CONFIG_PROD_FILE if mode == "prod" else ORDER_CONFIG_FILE
        )
        if mode == "prod" and not config_file.is_file():
            return {
                "ok": False,
                "error": "未找到生产 Broker 配置 order_config.prod.json",
            }
        status_code, data = order_api.get_task_detail(task_id, config_file)
        task = _unwrap_broker_task(data)
        if task is None:
            return {
                "ok": False,
                "http_status": status_code,
                "error": "任务详情格式无效",
            }
        broker_status = str(task.get("status") or "").strip()
        platform_order_no = _platform_order_no_from_task_dict(task)
        order_source = _infer_order_source(
            task.get("order_source"), platform_order_no
        )
        if not order_source:
            params = task.get("params")
            if isinstance(params, dict):
                order_source = _infer_order_source(
                    params.get("order_source"), platform_order_no
                )
        result = {
            "ok": True,
            "http_status": status_code,
            "task_id": str(task.get("task_id") or task_id).strip(),
            "order_no": _order_no_from_task_dict(task),
            "platform_order_no": platform_order_no,
            "order_source": order_source,
            "status": broker_status,
            "status_label": _BROKER_STATUS_LABELS.get(
                broker_status, broker_status or "未知"
            ),
            "ended": broker_status in _BROKER_ORDER_ENDED,
            "terminal": broker_status in _BROKER_ORDER_TERMINAL,
            "create_time": str(
                task.get("create_time") or task.get("order_time") or ""
            ),
            "raw": task,
            "source": "broker",
        }
        with _BROKER_ORDER_CACHE_LOCK:
            _BROKER_ORDER_CACHE[cache_key] = (time.monotonic(), deepcopy(result))
        return result
    except OrderBrokerError as error:
        return {
            "ok": False,
            "error": str(error),
            "http_status": error.status_code,
        }
    except (ValueError, FileNotFoundError, KeyError, TypeError) as error:
        return {"ok": False, "error": str(error)}


def _is_broker_configured(mode: str) -> bool:
    """Return True when order_config has valid Broker credentials."""
    from ksq.order.config import load_order_config, validate_order_config

    config_file = ORDER_CONFIG_PROD_FILE if mode == "prod" else ORDER_CONFIG_FILE
    try:
        config = load_order_config(config_file)
        validate_order_config(config)
        return True
    except (ValueError, FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _normalize_dashboard_mode(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in _DASHBOARD_MODES:
        raise ValueError("mode 仅支持 test 或 prod。")
    return value


def _normalize_etm_base_url(raw: object) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_ETM_BASE_URL
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("etm_base_url 必须以 http:// 或 https:// 开头。")
    return value


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
        "order_no": _order_no_from_task_dict(cloud),
        "platform_order_no": _platform_order_no_from_task_dict(cloud),
        "status": broker_status,
        "status_label": _BROKER_STATUS_LABELS.get(
            broker_status, broker_status or "未知"
        ),
        "ended": broker_status in _BROKER_ORDER_ENDED,
        "terminal": broker_status in _BROKER_ORDER_TERMINAL,
        "create_time": str(cloud.get("create_time") or ""),
        "raw": cloud,
        "source": "etm",
    }


def _sync_order_from_etm(
    order: Optional[Dict[str, object]], settings: Dict[str, object]
) -> Tuple[Optional[Dict[str, object]], Dict[str, object]]:
    """Production mode: prefer Edge Task Manager next/cloud for order identity."""
    base = _normalize_etm_base_url(settings.get("etm_base_url"))
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
    order_no = _order_no_from_task_dict(cloud)
    platform_no = _platform_order_no_from_task_dict(cloud)
    items = _items_from_ob_params(cloud.get("params"))
    current_id = "" if order is None else str(order.get("task_id") or "").strip()
    if next_task_id and next_task_id != current_id:
        order = set_active_order(
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
        order = set_active_order(
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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is not None:
            if order_no and not _ACTIVE_ORDER.get("order_no"):
                _ACTIVE_ORDER["order_no"] = order_no
            if platform_no and not _ACTIVE_ORDER.get("platform_order_no"):
                _ACTIVE_ORDER["platform_order_no"] = platform_no
            if items:
                existing = _order_item_codes(_ACTIVE_ORDER)
                merged_items = list(_ACTIVE_ORDER.get("items") or [])  # type: ignore[arg-type]
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
                _ACTIVE_ORDER["items"] = merged_items
                _ACTIVE_ORDER["item_count"] = len(merged_items)
            order = deepcopy(_ACTIVE_ORDER)
    return order, etm


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
        merged["status_label"] = _STATUS_LABELS.get(old_status, old_status)
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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is None:
            return
        active_id = str(_ACTIVE_ORDER.get("task_id") or "").strip()
        if task_id and active_id and active_id != task_id:
            return
        memory = _ACTIVE_ORDER.setdefault("item_states", {})
        state_changed = False
        if not isinstance(memory, dict):
            memory = {}
            _ACTIVE_ORDER["item_states"] = memory
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
            for raw in (_ACTIVE_ORDER.get("items") or [])
            if isinstance(raw, dict)
        }
        # 日志的 item 行编号可能是 sku_id（见 _ITEM_START_RE 处注释），而已有条目是按
        # 69码 建的。code_aliases 是订单已记录的 sku_id -> 69码，先翻译再判重，
        # 否则同一药品会以两种标识各存一条，虚增子任务数、让进度永远差一格。
        # 注意：69码 与 sku_id 是两个不同字段，这里只做翻译，不把两者归入同一集合。
        raw_aliases = _ACTIVE_ORDER.get("code_aliases")
        aliases = raw_aliases if isinstance(raw_aliases, dict) else {}
        items = list(_ACTIVE_ORDER.get("items") or [])
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
            _ACTIVE_ORDER["items"] = items
            _ACTIVE_ORDER["item_count"] = len(items)
        if state_changed or items_changed:
            _save_active_order_unlocked()


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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is None:
            return
        active_id = str(_ACTIVE_ORDER.get("task_id") or "").strip()
        if active_id and active_id != task_id:
            return
        old_cache = _ACTIVE_ORDER.get("feishu_log_cache")
        old_lines = old_cache.get("lines") if isinstance(old_cache, dict) else []
        lines = _merge_feishu_log_cache_lines(old_lines, raw_logs)
        if isinstance(old_cache, dict) and lines == old_lines:
            return
        _ACTIVE_ORDER["feishu_log_cache"] = {
            "task_id": task_id,
            "lines": lines,
            "updated_at": _ts_to_iso(datetime.now(timezone.utc)),
        }
        _save_active_order_unlocked()


def _public_order(order: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Hide internal listener/Feishu state from dashboard responses."""
    if order is None:
        return None
    result = deepcopy(order)
    result.pop("feishu_log_cache", None)
    result.pop("pending_confirm", None)
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
    with _ACTIVE_ORDER_LOCK:
        global _ACTIVE_ORDER
        if _ACTIVE_ORDER is None:
            return
        active_id = str(_ACTIVE_ORDER.get("task_id") or "").strip()
        if task_id and active_id and active_id != task_id:
            return
        memory = _ACTIVE_ORDER.get("item_states")
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
        _save_active_order_unlocked()


def _apply_order_lifecycle(
    order: Optional[Dict[str, object]],
    parsed: Dict[str, object],
    broker: Dict[str, object],
    tasks: List[Dict[str, object]],
) -> Tuple[Dict[str, object], List[Dict[str, object]], str]:
    lifecycle = _default_lifecycle()
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
    now_iso = _ts_to_iso(datetime.now(timezone.utc))
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
        elif broker_status in _BROKER_MANUAL_DONE:
            lifecycle["end_reason"] = "broker_manual_completed"
        elif broker_status in _BROKER_MANUAL_HELD:
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
        started_dt = _parse_ts(str(task.get("started_at") or ""))
        if started_dt is None:
            continue
        if first_started_dt is None or started_dt < first_started_dt:
            first_started_dt = started_dt

    already_frozen = (
        str(lifecycle.get("timer_stop_reason") or "") in _TIMER_STOP_REASONS
        and lifecycle.get("frozen_elapsed_seconds") is not None
    )
    if already_frozen:
        stop_dt = _parse_ts(str(lifecycle.get("timer_stopped_at") or ""))
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
        stop_dt = _parse_ts(str(stop_at or "")) or now_dt
        if stop_dt < first_started_dt:
            stop_dt = first_started_dt
            stop_at = _ts_to_iso(stop_dt) or now_iso
        frozen = max(0.0, (stop_dt - first_started_dt).total_seconds())
        lifecycle["timer_stopped_at"] = stop_at
        lifecycle["frozen_elapsed_seconds"] = frozen
        lifecycle["timer_stop_reason"] = "human_prompt"

    # Every actual order-ending status must freeze the order clock too. The
    # previous implementation only froze at a human prompt, so success/cancel/
    # error/manual-complete kept increasing forever in the browser.
    if lifecycle.get("ended") and first_started_dt is not None and not already_frozen:
        stop_at = lifecycle.get("ended_at") or now_iso
        stop_dt = _parse_ts(str(stop_at or "")) or now_dt
        latest_task_end: Optional[datetime] = None
        for task in tasks:
            task_end = _parse_ts(str(task.get("ended_at") or ""))
            if task_end is not None and (
                latest_task_end is None or task_end > latest_task_end
            ):
                latest_task_end = task_end
        if latest_task_end is not None:
            stop_dt = latest_task_end
            stop_at = _ts_to_iso(stop_dt) or stop_at
        if stop_dt < first_started_dt:
            stop_dt = first_started_dt
            stop_at = _ts_to_iso(stop_dt) or now_iso
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
        elif broker_status in _BROKER_MANUAL_HELD:
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
                task["status_label"] = _STATUS_LABELS["skipped"]
                task["active"] = False
                task["needs_confirm"] = False
            elif status in {"started", "processing"} and needs_confirm:
                if not task.get("needs_confirm"):
                    task["active"] = False
            elif status in {"success", "failed"}:
                task["active"] = False
                task["needs_confirm"] = False
                task["status_label"] = _STATUS_LABELS.get(status, status)
    else:
        # Revive items wrongly marked skipped while the order is still running.
        for task in tasks:
            if str(task.get("status") or "") == "skipped" and not task.get("end_line"):
                task["status"] = "pending"
                task["status_label"] = _STATUS_LABELS["pending"]
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
        with _ACTIVE_ORDER_LOCK:
            global _ACTIVE_ORDER
            if _ACTIVE_ORDER is not None and str(
                _ACTIVE_ORDER.get("task_id") or ""
            ) == str(order.get("task_id") or ""):
                changed = _ACTIVE_ORDER.get("lifecycle") != lifecycle
                if changed:
                    _ACTIVE_ORDER["lifecycle"] = deepcopy(lifecycle)
                if order.get("order_no") and _ACTIVE_ORDER.get("order_no") != order.get(
                    "order_no"
                ):
                    _ACTIVE_ORDER["order_no"] = order.get("order_no")
                    changed = True
                if changed:
                    _save_active_order_unlocked()

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
            fresh = states.get(code) or _empty_item_state(code)
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
        and str(lifecycle.get("timer_stop_reason") or "") in _TIMER_STOP_REASONS
    ):
        try:
            return max(0.0, float(frozen))
        except (TypeError, ValueError):
            pass

    # Start counting only after first 开始处理 — not when the next order
    # is merely received / registered.
    start_candidates: List[datetime] = []
    for task in tasks:
        started = _parse_ts(str(task.get("started_at") or ""))
        if started is not None:
            start_candidates.append(started)
    if not start_candidates:
        return None
    end_dt = _parse_ts(str(polled_at or "")) or datetime.now(timezone.utc)
    elapsed = _duration_seconds(min(start_candidates), end_dt)
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
    settings = load_dashboard_settings()
    mode = str(settings.get("mode") or _DEFAULT_DASHBOARD_MODE)
    broker_configured = _is_broker_configured(mode)
    info = inspect_container(ROBOT_SERVICE_NAME)
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
            "service": ROBOT_SERVICE_NAME,
            "service_running": bool(info.get("running")),
            "service_status": info.get("status"),
            "service_message": info.get("message") or "",
            "polled_at": _ts_to_iso(datetime.now(timezone.utc)),
            "order": None,
            "order_queue": {
                "capacity": _ORDER_QUEUE_LIMIT,
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
            "status_label": _STATUS_LABELS["idle"],
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
    order = get_active_order()
    if mode == "prod":
        order, etm_status = _sync_order_from_etm(order, settings)
    payload: Dict[str, object] = {
        "service": ROBOT_SERVICE_NAME,
        "service_running": bool(info.get("running")),
        "service_status": info.get("status"),
        "service_message": info.get("message") or "",
        "polled_at": _ts_to_iso(datetime.now(timezone.utc)),
        "order": _public_order(order),
        "order_queue": order_queue_status(),
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
                _apply_broker_item_details(tasks, _fetch_broker_order(parent, mode))
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
        logs_payload = fetch_logs(ROBOT_SERVICE_ID, tail, "")
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
    code_aliases = _merged_code_aliases(order, raw_logs)
    latest_task_id, codes_by_task, last_seen_by_task = _discover_log_tasks(
        raw_logs, aliases=code_aliases
    )
    order_codes = _order_item_codes(order)
    focus_task_id = _match_log_task_for_order(
        order, codes_by_task, latest_task_id, last_seen_by_task
    )
    stale_task_ids = _stale_log_tasks(order, last_seen_by_task)
    if stale_task_ids:
        _prune_stale_item_states(order, stale_task_ids)
    parsed = parse_robot_log_text(
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
        broker = _fetch_broker_order(parent_task_id, mode)
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
        with _ACTIVE_ORDER_LOCK:
            global _ACTIVE_ORDER
            if _ACTIVE_ORDER is not None and str(
                _ACTIVE_ORDER.get("task_id") or ""
            ) == str(order.get("task_id") or ""):
                for key in ("order_no", "platform_order_no"):
                    value = str(broker.get(key) or "").strip()
                    if value and not _ACTIVE_ORDER.get(key):
                        _ACTIVE_ORDER[key] = value
                        order[key] = value
                        changed_identity = True
                if changed_identity:
                    _save_active_order_unlocked()
        reconciled = _reconcile_broker_task_id(order, mode)
        if reconciled is not None:
            order = reconciled
        corrected_parent = str(order.get("task_id") or "").strip()
        if corrected_parent and corrected_parent != parent_task_id:
            parent_task_id = corrected_parent
            broker = _fetch_broker_order(parent_task_id, mode)
    # Broker 状态原值直出，不做本地覆盖。
    # 子任务的名称/库位以 Broker 带回的下单信息为准，必须在生命周期与计时
    # 处理之前回填，后续的 focus（大字区）从 tasks 里选，就能自动同源。
    _apply_broker_item_details(tasks, broker)
    lifecycle, tasks, aggregate = _apply_order_lifecycle(
        order, parsed, broker, tasks
    )
    polled_at_early = str(payload.get("polled_at") or "")
    if not lifecycle.get("ended"):
        _refresh_live_elapsed(tasks, polled_at_early)
    _persist_item_states(order, tasks)
    if order is not None:
        order = deepcopy(get_active_order() or order)
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
            inferred = _infer_order_source(
                broker.get("order_source") if broker.get("ok") else "",
                order.get("platform_order_no") or broker.get("platform_order_no"),
            )
            if inferred:
                order["order_source"] = inferred
        if order.get("order_source") or order.get("platform_order_no"):
            with _ACTIVE_ORDER_LOCK:
                if _ACTIVE_ORDER is not None and str(
                    _ACTIVE_ORDER.get("task_id") or ""
                ) == str(order.get("task_id") or ""):
                    if order.get("order_source") and not _ACTIVE_ORDER.get(
                        "order_source"
                    ):
                        _ACTIVE_ORDER["order_source"] = order.get("order_source")
                    if order.get("platform_order_no") and not _ACTIVE_ORDER.get(
                        "platform_order_no"
                    ):
                        _ACTIVE_ORDER["platform_order_no"] = order.get(
                            "platform_order_no"
                        )
                    _save_active_order_unlocked()

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
    status_label = str(lifecycle.get("label") or _STATUS_LABELS.get(aggregate, aggregate))
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
        _refresh_live_elapsed(tasks, polled_at)
    order_elapsed = _order_elapsed_seconds(order, tasks, lifecycle, polled_at)
    if (
        order_elapsed is not None
        and order_elapsed > 0
        and str(lifecycle.get("timer_stop_reason") or "") in _TIMER_STOP_REASONS
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
        with _ACTIVE_ORDER_LOCK:
            if _ACTIVE_ORDER is not None and (
                not expected_task_id
                or str(_ACTIVE_ORDER.get("task_id") or "").strip()
                == expected_task_id
            ):
                life = _ACTIVE_ORDER.get("lifecycle")
                if not isinstance(life, dict):
                    life = {}
                    _ACTIVE_ORDER["lifecycle"] = life
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
                    _save_active_order_unlocked()
    focus_elapsed = None if focus is None else focus.get("elapsed_seconds")
    dismissed_fingerprint = _dismissed_fingerprint_from_order(order)
    if not dismissed_fingerprint:
        with _ACTIVE_ORDER_LOCK:
            _ensure_active_order_loaded()
            dismissed_fingerprint = _dismissed_fingerprint_from_order(_ACTIVE_ORDER)

    payload["order"] = _public_order(order)
    payload["order_queue"] = order_queue_status()
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
            get_active_order,
        )
        refreshed = get_active_order()
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
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is None:
            return snapshot
        active_task_id = str(_ACTIVE_ORDER.get("task_id") or "").strip()
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

        raw_pending = _ACTIVE_ORDER.get("pending_confirm")
        pending = deepcopy(raw_pending) if isinstance(raw_pending, dict) else None
        if pending is not None and str(pending.get("task_id") or "").strip() not in {
            "",
            active_task_id,
        }:
            _ACTIVE_ORDER.pop("pending_confirm", None)
            _save_active_order_unlocked()
            pending = None

        if snapshot.get("confirm_closed"):
            if pending is not None:
                _ACTIVE_ORDER.pop("pending_confirm", None)
                _save_active_order_unlocked()
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
                _ACTIVE_ORDER["pending_confirm"] = deepcopy(pending)
                _save_active_order_unlocked()

        if pending is None:
            return snapshot
        active_order = deepcopy(_ACTIVE_ORDER)

    # A log line may temporarily disappear while Docker reconnects or the tail
    # rolls. Restore only the confirmation-facing fields; keep current service,
    # Broker, progress, and timing data from this fresh snapshot.
    snapshot["needs_confirm"] = True
    snapshot["confirm_closed"] = False
    snapshot["status"] = str(pending.get("status") or "await_confirm")
    snapshot["status_label"] = _STATUS_LABELS.get(
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
    global _DASHBOARD_CACHE
    while True:
        with _DASHBOARD_REFRESH_LOCK:
            with _DASHBOARD_CACHE_LOCK:
                generation = _DASHBOARD_CACHE_GENERATION
            snapshot = _reconcile_pending_confirmation(
                _build_dashboard_snapshot(tail)
            )
            with _DASHBOARD_CACHE_LOCK:
                if generation != _DASHBOARD_CACHE_GENERATION:
                    continue
                _DASHBOARD_CACHE = deepcopy(snapshot)
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
    with _DASHBOARD_CACHE_LOCK:
        cached = deepcopy(_DASHBOARD_CACHE)
    if cached is not None:
        return cached
    # The worker may already be building. The refresh lock makes concurrent
    # first requests wait for one builder instead of starting duplicate work.
    with _DASHBOARD_REFRESH_LOCK:
        with _DASHBOARD_CACHE_LOCK:
            cached = deepcopy(_DASHBOARD_CACHE)
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
            with _DASHBOARD_CACHE_LOCK:
                snapshot = deepcopy(_DASHBOARD_CACHE)
        stop_event.wait(_dashboard_monitor_delay(snapshot))


def start_dashboard_monitor() -> None:
    """Start the process-wide dashboard/log listener once."""
    global _DASHBOARD_MONITOR_STOP, _DASHBOARD_MONITOR_THREAD
    with _DASHBOARD_CACHE_LOCK:
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
    with _DASHBOARD_CACHE_LOCK:
        stop_event = _DASHBOARD_MONITOR_STOP
        thread = _DASHBOARD_MONITOR_THREAD
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.ident is not None:
        thread.join(max(0.0, timeout))
    with _DASHBOARD_CACHE_LOCK:
        if _DASHBOARD_MONITOR_THREAD is thread and (
            thread is None or not thread.is_alive()
        ):
            _DASHBOARD_MONITOR_THREAD = None
            _DASHBOARD_MONITOR_STOP = None


_LIST_DEVICES_SCRIPT = r"""
import json
import re
from pathlib import Path

text = Path("/proc/bus/input/devices").read_text(errors="replace")
blocks = [b for b in text.strip().split("\n\n") if b.strip()]
devices = []
for block in blocks:
    name = ""
    handlers = ""
    phys = ""
    for line in block.splitlines():
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1].strip()
        elif line.startswith("P: Phys="):
            phys = line.split("=", 1)[1].strip()
    match = re.search(r"\bevent(\d+)\b", handlers)
    if match is None:
        continue
    path = f"/dev/input/event{match.group(1)}"
    is_kbd = "kbd" in handlers.split()
    devices.append(
        {
            "path": path,
            "name": name or path,
            "handlers": handlers,
            "phys": phys,
            "is_keyboard": is_kbd,
        }
    )
devices.sort(key=lambda row: int(re.search(r"(\d+)$", row["path"]).group(1)))
print(json.dumps({"ok": True, "devices": devices}, ensure_ascii=False))
"""

_INJECT_SCRIPT = r"""
import json
import os
import struct
import time

EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0
KEY_1 = 2
device = (os.environ.get("KSQ_KEYBOARD_DEVICE") or "").strip()


def emit_fd(fd, ev_type, code, value):
    now = time.time()
    sec = int(now)
    usec = int((now - sec) * 1_000_000)
    os.write(fd, struct.pack("llHHi", sec, usec, ev_type, code, value))


errors = []
if device:
    try:
        from evdev import InputDevice, ecodes

        dev = InputDevice(device)
        dev.write(ecodes.EV_KEY, ecodes.KEY_1, 1)
        dev.syn()
        time.sleep(0.05)
        dev.write(ecodes.EV_KEY, ecodes.KEY_1, 0)
        dev.syn()
        print(json.dumps({"ok": True, "method": "evdev_device", "device": device}))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as error:
        errors.append(f"evdev_device:{error}")
    try:
        fd = os.open(device, os.O_WRONLY)
        try:
            emit_fd(fd, EV_KEY, KEY_1, 1)
            emit_fd(fd, EV_SYN, SYN_REPORT, 0)
            time.sleep(0.05)
            emit_fd(fd, EV_KEY, KEY_1, 0)
            emit_fd(fd, EV_SYN, SYN_REPORT, 0)
        finally:
            os.close(fd)
        print(json.dumps({"ok": True, "method": "event_write", "device": device}))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as error:
        errors.append(f"event_write:{error}")

try:
    from evdev import UInput, ecodes

    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_1]}, name="ksq-virtual-keyboard")
    time.sleep(0.2)
    ui.write(ecodes.EV_KEY, ecodes.KEY_1, 1)
    ui.syn()
    time.sleep(0.05)
    ui.write(ecodes.EV_KEY, ecodes.KEY_1, 0)
    ui.syn()
    ui.close()
    print(
        json.dumps(
            {
                "ok": True,
                "method": "uinput_fallback",
                "device": device,
                "errors": errors,
            }
        )
    )
except Exception as error:
    errors.append(f"uinput:{error}")
    print(json.dumps({"ok": False, "errors": errors}))
    raise SystemExit(1)
"""


def _normalize_keyboard_device(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return _DEFAULT_KEYBOARD_DEVICE
    if not _KEYBOARD_DEVICE_RE.match(value):
        raise ValueError(
            "keyboard_device 格式无效，应为 /dev/input/eventN。"
        )
    return value


def _normalize_bool(raw: object, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{field} 必须是布尔值。")
    return raw


def _default_feishu_settings() -> Dict[str, object]:
    return {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "forms": [],
        "selected_form": "",
        "ai": {
            "enabled": False,
            "endpoint": "",
            "api_key": "",
            "model": "gpt-4o-mini",
            "max_tokens": 180,
        },
    }


def _normalize_feishu_ai(
    raw: object, previous: object = None, strict: bool = False
) -> Dict[str, object]:
    current = {
        "enabled": False,
        "endpoint": "",
        "api_key": "",
        "model": "gpt-4o-mini",
        "max_tokens": 180,
    }
    if isinstance(previous, dict):
        if isinstance(previous.get("enabled"), bool):
            current["enabled"] = previous["enabled"]
        for key in ("endpoint", "model"):
            if key in previous:
                current[key] = str(previous.get(key) or "").strip()
        if str(previous.get("api_key") or "").strip():
            current["api_key"] = str(previous.get("api_key") or "").strip()
        try:
            current["max_tokens"] = max(
                64, min(1000, int(previous.get("max_tokens") or 180))
            )
        except (TypeError, ValueError):
            pass
    if not isinstance(raw, dict):
        if strict and raw is not None:
            raise ValueError("feishu.ai 必须是对象。")
        return current
    if "enabled" in raw:
        if strict:
            current["enabled"] = _normalize_bool(
                raw.get("enabled"), "feishu.ai.enabled"
            )
        elif isinstance(raw.get("enabled"), bool):
            current["enabled"] = raw["enabled"]
    for key in ("endpoint", "model"):
        if key in raw:
            current[key] = str(raw.get(key) or "").strip()
    if "api_key" in raw:
        value = str(raw.get("api_key") or "").strip()
        if value:
            current["api_key"] = value
    if "max_tokens" in raw:
        try:
            current["max_tokens"] = max(64, min(1000, int(raw.get("max_tokens") or 180)))
        except (TypeError, ValueError):
            pass
    if not str(current.get("model") or "").strip():
        current["model"] = "gpt-4o-mini"
    return current


def _parse_feishu_link(raw: object) -> Tuple[str, str]:
    """Extract the Bitable app/table identifiers from a pasted Feishu URL."""
    value = str(raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        base_index = parts.index("base")
    except ValueError:
        base_index = -1
    app_token = parts[base_index + 1].strip() if base_index >= 0 and len(parts) > base_index + 1 else ""
    table_id = (parse_qs(parsed.query, keep_blank_values=True).get("table") or [""])[0].strip()
    if not app_token or not table_id:
        raise ValueError("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。")
    return app_token, table_id


def _canonical_feishu_link(app_token: str, table_id: str) -> str:
    return "https://feishu.cn/base/%s?table=%s" % (
        quote(app_token, safe=""),
        quote(table_id, safe=""),
    )


def _normalize_feishu_forms(raw: object, strict: bool = False) -> List[Dict[str, str]]:
    """Configured forms: a pasted Feishu link plus one registered payload rule."""
    forms: List[Dict[str, str]] = []
    seen = set()
    seen_names = set()
    if strict and not isinstance(raw, list):
        raise ValueError("飞书 forms 必须是数组。")
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            if strict:
                raise ValueError("飞书表单配置格式无效。")
            continue
        link = str(entry.get("url") or "").strip()
        app_token = ""
        table_id = ""
        if link:
            try:
                app_token, table_id = _parse_feishu_link(link)
            except ValueError:
                if strict:
                    raise
                continue
        else:
            # Keep old settings readable; newly saved settings use url.
            app_token = str(entry.get("app_token") or "").strip()
            table_id = str(entry.get("table_id") or "").strip()
            if app_token and table_id:
                link = _canonical_feishu_link(app_token, table_id)
        name = str(entry.get("name") or "").strip()
        form_id = str(entry.get("id") or "").strip() or name
        if not form_id or not name or not app_token or not table_id:
            if strict:
                raise ValueError("请完整填写表单名称和飞书多维表格链接。")
            continue
        if form_id in seen or name in seen_names:
            if strict:
                raise ValueError("飞书表单名称或 ID 重复：%s" % name)
            continue
        seen.add(form_id)
        seen_names.add(name)
        forms.append(
            {
                "id": form_id,
                "name": name,
                "url": link,
                "app_token": app_token,
                "table_id": table_id,
                "rule": normalize_rule_id(entry.get("rule")),
            }
        )
    return forms


def _normalize_feishu_settings(
    raw: object, previous: Optional[Dict[str, object]], strict: bool = False
) -> Dict[str, object]:
    current = _default_feishu_settings()
    if isinstance(previous, dict):
        if isinstance(previous.get("enabled"), bool):
            current["enabled"] = previous["enabled"]
        current["app_id"] = str(previous.get("app_id") or "").strip()
        current["app_secret"] = str(previous.get("app_secret") or "").strip()
        current["forms"] = _normalize_feishu_forms(previous.get("forms"))
        current["selected_form"] = str(previous.get("selected_form") or "").strip()
        current["ai"] = _normalize_feishu_ai(previous.get("ai"))
    if not isinstance(raw, dict):
        if strict and raw is not None:
            raise ValueError("feishu 必须是对象。")
        current["forms"] = _normalize_feishu_forms(current.get("forms"))
        current["ai"] = _normalize_feishu_ai(None, current.get("ai"))
        return current
    if "enabled" in raw:
        if strict:
            current["enabled"] = _normalize_bool(raw.get("enabled"), "feishu.enabled")
        elif isinstance(raw.get("enabled"), bool):
            current["enabled"] = raw["enabled"]
    if "app_id" in raw:
        current["app_id"] = str(raw.get("app_id") or "").strip()
    if "app_secret" in raw:
        secret = str(raw.get("app_secret") or "").strip()
        if secret:
            current["app_secret"] = secret
    if "forms" in raw:
        forms = _normalize_feishu_forms(raw.get("forms"), strict)
    else:
        forms = _normalize_feishu_forms(current.get("forms"))
    current["forms"] = forms

    if "selected_form" in raw:
        current["selected_form"] = str(raw.get("selected_form") or "").strip()
    if strict and "ai" in raw and raw.get("ai") is not None and not isinstance(raw.get("ai"), dict):
        raise ValueError("feishu.ai 必须是对象。")
    current["ai"] = _normalize_feishu_ai(
        raw.get("ai"), current.get("ai"), strict=strict
    )
    known = {str(form.get("id") or "") for form in current["forms"]}
    if str(current.get("selected_form") or "") not in known:
        current["selected_form"] = str(current["forms"][0]["id"]) if current["forms"] else ""
    if strict and current["enabled"]:
        if not str(current.get("app_id") or "").strip() or not str(current.get("app_secret") or "").strip():
            raise ValueError("启用飞书表单前请配置 App ID 和 App Secret。")
        if not current["forms"]:
            raise ValueError("启用飞书表单前请新增并选择一个表单。")
    return current


def _public_feishu_settings(feishu: Mapping[str, object]) -> Dict[str, object]:
    ai = feishu.get("ai") if isinstance(feishu.get("ai"), Mapping) else {}
    return {
        "enabled": bool(feishu.get("enabled")),
        "app_id": str(feishu.get("app_id") or ""),
        "has_app_secret": bool(str(feishu.get("app_secret") or "").strip()),
        "forms": deepcopy(feishu.get("forms") or []),
        "selected_form": str(feishu.get("selected_form") or ""),
        "form_rules": public_rules(),
        "ai": {
            "enabled": bool(ai.get("enabled")),
            "endpoint": str(ai.get("endpoint") or ""),
            "model": str(ai.get("model") or "gpt-4o-mini"),
            "max_tokens": int(ai.get("max_tokens") or 180),
            "has_api_key": bool(str(ai.get("api_key") or "").strip()),
        },
    }


def load_dashboard_settings() -> Dict[str, object]:
    settings: Dict[str, object] = {
        "keyboard_device": _DEFAULT_KEYBOARD_DEVICE,
        "mode": _DEFAULT_DASHBOARD_MODE,
        "etm_base_url": DEFAULT_ETM_BASE_URL,
        "auto_confirm": False,
        "feishu": _default_feishu_settings(),
    }
    path = DASHBOARD_SETTINGS_FILE
    payload: object = {}
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            # A damaged settings file must not take down every dashboard/status
            # endpoint.  Defaults keep the UI usable while the log identifies
            # the repair target.
            LOGGER.warning("读取仪表板配置失败，使用默认值 path=%s error=%s", path, error)
        if isinstance(payload, dict):
            try:
                settings["keyboard_device"] = _normalize_keyboard_device(
                    payload.get("keyboard_device")
                )
            except ValueError:
                settings["keyboard_device"] = _DEFAULT_KEYBOARD_DEVICE
            try:
                settings["mode"] = _normalize_dashboard_mode(
                    payload.get("mode") or _DEFAULT_DASHBOARD_MODE
                )
            except ValueError:
                settings["mode"] = _DEFAULT_DASHBOARD_MODE
            try:
                settings["etm_base_url"] = _normalize_etm_base_url(
                    payload.get("etm_base_url")
                )
            except ValueError:
                settings["etm_base_url"] = DEFAULT_ETM_BASE_URL
            if isinstance(payload.get("auto_confirm"), bool):
                settings["auto_confirm"] = payload["auto_confirm"]
            settings["feishu"] = _normalize_feishu_settings(
                payload.get("feishu"), None
            )
    # Prefer mounted robot env file when present.
    env_path = ROBOT_KEYBOARD_ENV_FILE
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                if key.strip() == "PNP_KEYBOARD_DEVICE":
                    settings["keyboard_device"] = _normalize_keyboard_device(
                        value.strip().strip('"').strip("'")
                    )
                    break
        except (OSError, ValueError):
            pass
    return settings


def _write_robot_keyboard_env(device: str) -> bool:
    env_path = ROBOT_KEYBOARD_ENV_FILE
    try:
        safe_write_text(
            env_path,
            f"PNP_KEYBOARD_DEVICE={device}\n",
            keep_days=_SETTINGS_BACKUP_KEEP_DAYS,
        )
        return True
    except OSError:
        return False


def clear_active_order() -> None:
    global _ACTIVE_ORDER, _ACTIVE_ORDER_LOADED
    with _ACTIVE_ORDER_LOCK:
        _ACTIVE_ORDER_LOADED = True
        _ACTIVE_ORDER = None
        _save_active_order_unlocked()
    _invalidate_dashboard_snapshot_cache()


def save_dashboard_settings(
    payload: Dict[str, object], restart_robot: bool
) -> Dict[str, object]:
    # Serialize read-modify-write of the settings file; the container recreate
    # below is deliberately left outside the lock since it can take seconds.
    with _SETTINGS_LOCK:
        current = load_dashboard_settings()
        previous_mode = str(current.get("mode") or _DEFAULT_DASHBOARD_MODE)
        if "keyboard_device" in payload:
            current["keyboard_device"] = _normalize_keyboard_device(
                payload.get("keyboard_device")
            )
        if "mode" in payload:
            current["mode"] = _normalize_dashboard_mode(payload.get("mode"))
        if "etm_base_url" in payload:
            current["etm_base_url"] = _normalize_etm_base_url(
                payload.get("etm_base_url")
            )
        if "auto_confirm" in payload:
            current["auto_confirm"] = _normalize_bool(
                payload.get("auto_confirm"), "auto_confirm"
            )
        if "feishu" in payload:
            previous_feishu = current.get("feishu")
            if not isinstance(previous_feishu, dict):
                previous_feishu = _default_feishu_settings()
            current["feishu"] = _normalize_feishu_settings(
                payload.get("feishu"), previous_feishu, strict=True
            )
        feishu_settings = current.get("feishu")
        if not isinstance(feishu_settings, dict):
            feishu_settings = _default_feishu_settings()
        settings = {
            "keyboard_device": current["keyboard_device"],
            "mode": current["mode"],
            "etm_base_url": current["etm_base_url"],
            "auto_confirm": bool(current.get("auto_confirm")),
            "feishu": feishu_settings,
        }
        # Preserve the internal version marker used by state_reset.py so
        # that a user-initiated settings save does not wipe it and cause a
        # spurious reset on the next restart.
        try:
            if DASHBOARD_SETTINGS_FILE.is_file():
                _existing_settings = json.loads(
                    DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8")
                )
                if isinstance(_existing_settings, dict) and (
                    "_app_version_marker" in _existing_settings
                ):
                    settings["_app_version_marker"] = _existing_settings[
                        "_app_version_marker"
                    ]
        except (OSError, json.JSONDecodeError):
            pass
        safe_write_text(
            DASHBOARD_SETTINGS_FILE,
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            keep_days=_SETTINGS_BACKUP_KEEP_DAYS,
        )
        env_written = _write_robot_keyboard_env(str(settings["keyboard_device"]))
    mode_changed = previous_mode != str(settings["mode"])
    if mode_changed:
        clear_active_order()
    restart_result: Optional[Dict[str, object]] = None
    if restart_robot:
        # Recreate is required for env_file changes; docker restart keeps old env.
        restart_result = _recreate_robot_for_keyboard_env()
    mode_label = "生产" if settings["mode"] == "prod" else "测试"
    public_settings = {
        "keyboard_device": settings["keyboard_device"],
        "mode": settings["mode"],
        "etm_base_url": settings["etm_base_url"],
        "auto_confirm": bool(settings.get("auto_confirm")),
        "feishu": _public_feishu_settings(
            settings["feishu"] if isinstance(settings.get("feishu"), dict) else {}
        ),
    }
    _invalidate_dashboard_snapshot_cache()
    return {
        "ok": True,
        "settings": public_settings,
        "env_written": env_written,
        "env_path": str(ROBOT_KEYBOARD_ENV_FILE),
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


def _recreate_robot_for_keyboard_env() -> Dict[str, object]:
    """Force-recreate robot container so env_file PNP_KEYBOARD_DEVICE reloads."""
    info = inspect_container(ROBOT_SERVICE_NAME)
    if not info.get("exists"):
        raise LogServiceError(
            str(info.get("message") or f"服务不存在：{ROBOT_SERVICE_NAME}"),
            503,
        )
    code, stdout, stderr = _run_docker_raw(
        [
            "inspect",
            "-f",
            "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}"
            "|{{index .Config.Labels \"com.docker.compose.project.config_files\"}}"
            "|{{index .Config.Labels \"com.docker.compose.project\"}}",
            ROBOT_SERVICE_NAME,
        ],
        10,
    )
    if code != 0:
        # Fallback: plain restart (env may not refresh).
        return restart_services([ROBOT_SERVICE_NAME])
    parts = (stdout or "").strip().split("|")
    working_dir = parts[0].strip() if parts else ""
    config_files = parts[1].strip() if len(parts) > 1 else ""
    project = parts[2].strip() if len(parts) > 2 else ""
    if not working_dir:
        return restart_services([ROBOT_SERVICE_NAME])
    compose_args = ["compose"]
    if project:
        compose_args.extend(["-p", project])
    if config_files:
        for item in config_files.split(","):
            path = item.strip()
            if path:
                compose_args.extend(["-f", path])
    else:
        compose_args.extend(["--project-directory", working_dir])
    compose_args.extend(
        [
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "robot_workspace_move_test",
        ]
    )
    # Run from host working dir via docker compose (daemon resolves host paths).
    try:
        completed = subprocess.run(
            ["docker", *compose_args],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            cwd=working_dir if working_dir else None,
        )
    except FileNotFoundError as error:
        raise LogServiceError("未找到 docker 命令。", 503) from error
    except subprocess.TimeoutExpired as error:
        raise LogServiceError("重建机器人容器超时。", 504) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise LogServiceError(f"重建机器人容器失败：{detail}", 502)
    return {
        "ok": True,
        "action": "force-recreate",
        "name": ROBOT_SERVICE_NAME,
        "message": "已按新键盘设备环境变量重建机器人容器",
    }


def _run_docker_raw(args: List[str], timeout_seconds: int) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "docker not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def list_keyboard_devices() -> Dict[str, object]:
    settings = load_dashboard_settings()
    info = inspect_container(ROBOT_SERVICE_NAME)
    devices: List[Dict[str, object]] = []
    list_error = ""
    if info.get("running"):
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "exec",
                    ROBOT_SERVICE_NAME,
                    "python3",
                    "-c",
                    _LIST_DEVICES_SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode == 0:
                parsed = json.loads((completed.stdout or "").strip().splitlines()[-1])
                if isinstance(parsed, dict) and isinstance(parsed.get("devices"), list):
                    devices = [
                        row
                        for row in parsed["devices"]
                        if isinstance(row, dict)
                    ]
            else:
                list_error = (completed.stderr or completed.stdout or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            list_error = str(error)
    else:
        list_error = str(info.get("message") or "机器人服务未启动")
    public_settings = {
        "keyboard_device": settings.get("keyboard_device"),
        "mode": settings.get("mode"),
        "etm_base_url": settings.get("etm_base_url"),
        "auto_confirm": bool(settings.get("auto_confirm")),
        "feishu": _public_feishu_settings(
            settings["feishu"] if isinstance(settings.get("feishu"), dict) else {}
        ),
    }
    return {
        "ok": True,
        "settings": public_settings,
        "devices": devices,
        "service_running": bool(info.get("running")),
        "list_error": list_error,
        "default_device": _DEFAULT_KEYBOARD_DEVICE,
    }


def _persist_feishu_submit_state(order: Dict[str, object]) -> None:
    task_id = str(order.get("task_id") or "").strip()
    state = order.get("feishu_submit")
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is None:
            return
        if task_id and str(_ACTIVE_ORDER.get("task_id") or "") != task_id:
            return
        _ACTIVE_ORDER["feishu_submit"] = deepcopy(state) if isinstance(state, dict) else state
        _save_active_order_unlocked()


def preview_feishu_submission() -> Dict[str, object]:
    settings = load_dashboard_settings()
    order = get_active_order()
    return preview_feishu_form(
        order,
        [],
        str(settings.get("mode") or _DEFAULT_DASHBOARD_MODE),
        settings,
    )


def submit_feishu_manual() -> Dict[str, object]:
    settings = load_dashboard_settings()
    order = get_active_order()
    if order is None:
        raise ValueError("当前没有工单，无法提交飞书表单。")
    working = deepcopy(order)
    working.pop("feishu_submit", None)
    with _ACTIVE_ORDER_LOCK:
        _ensure_active_order_loaded()
        if _ACTIVE_ORDER is not None and str(_ACTIVE_ORDER.get("task_id") or "") == str(
            working.get("task_id") or ""
        ):
            _ACTIVE_ORDER.pop("feishu_submit", None)
            _save_active_order_unlocked()
    from ksq.feishu.submit import clear_feishu_dedupe_key

    clear_feishu_dedupe_key(working)
    return maybe_submit_feishu_form(
        working,
        [],
        str(settings.get("mode") or _DEFAULT_DASHBOARD_MODE),
        settings,
        "manual",
        _persist_feishu_submit_state,
        get_active_order,
    )


def confirm_and_maybe_submit_feishu() -> Dict[str, object]:
    """Inject confirm key. Feishu is normally submitted on speak; this is fallback."""
    settings = load_dashboard_settings()
    pre_order = get_active_order()
    result = inject_confirm_key()
    working_order = deepcopy(pre_order) if isinstance(pre_order, dict) else None
    if should_submit_on_confirm(working_order, ""):
        feishu_result = maybe_submit_feishu_form(
            working_order,
            [],
            str(settings.get("mode") or _DEFAULT_DASHBOARD_MODE),
            settings,
            "confirm_fallback",
            _persist_feishu_submit_state,
            get_active_order,
        )
    else:
        feishu_result = {
            "ok": False,
            "skipped": True,
            "reason": "already_handled_or_no_prompt",
        }
    result["feishu"] = feishu_result
    return result


def inject_confirm_key() -> Dict[str, object]:
    info = inspect_container(ROBOT_SERVICE_NAME)
    if not info.get("running"):
        raise LogServiceError(
            str(info.get("message") or f"服务未启动：{ROBOT_SERVICE_NAME}"),
            503,
        )
    device = str(load_dashboard_settings().get("keyboard_device") or _DEFAULT_KEYBOARD_DEVICE)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                f"KSQ_KEYBOARD_DEVICE={device}",
                ROBOT_SERVICE_NAME,
                "python3",
                "-c",
                _INJECT_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as error:
        raise LogServiceError(
            "未找到 docker 命令，无法注入虚拟键盘。",
            503,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise LogServiceError("虚拟键盘注入超时。", 504) from error

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"exit={completed.returncode}"
        raise LogServiceError(f"虚拟键盘注入失败：{detail}", 502)

    method = "unknown"
    used_device = device
    try:
        parsed = json.loads(stdout.splitlines()[-1])
        if isinstance(parsed, dict):
            method = str(parsed.get("method") or "unknown")
            used_device = str(parsed.get("device") or device)
    except (json.JSONDecodeError, IndexError, TypeError):
        method = "unknown"

    return {
        "ok": True,
        "key": "1",
        "method": method,
        "device": used_device,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "message": f"已向 {used_device} 注入确认按键",
    }
