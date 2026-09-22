"""Pure robot-log parsing and item timing; no HTTP, device or file access."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Tuple
import re

from ksq.order import model as order_model


# A log tail is useful for recovering a process that restarted mid-order, but
# an old tail must never become a new queue lock when the active-order file is
# missing.  Robot workflows are normally measured in minutes; a half-hour
# window also tolerates a slow/manual step without reviving yesterday's task.
_LOG_ORDER_RECOVERY_MAX_AGE_SECONDS = 30 * 60


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


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


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
        "status_label": order_model._STATUS_LABELS["pending"],
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
            "at": order_model._ts_to_iso(ts),
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
        start_dt = order_model._parse_ts(str(item.get("started_at") or ""))
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
        item["started_at"] = order_model._ts_to_iso(start_dt)
    if await_dt is not None:
        item["await_at"] = order_model._ts_to_iso(await_dt)
    if end_dt is not None:
        item["ended_at"] = order_model._ts_to_iso(end_dt)
    item["elapsed_to_await_seconds"] = order_model._duration_seconds(start_dt, await_dt)
    if start_dt is not None and timing_end is not None:
        item["elapsed_seconds"] = order_model._duration_seconds(start_dt, timing_end)
    if item.get("duration_seconds") is None and start_dt is not None and end_dt is not None:
        item["duration_seconds"] = order_model._duration_seconds(start_dt, end_dt)
    item["status_label"] = order_model._STATUS_LABELS.get(status, status)
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
    now = order_model._parse_ts(str(polled_at or "")) or datetime.now(timezone.utc)
    for task in tasks:
        status = str(task.get("status") or "pending")
        if status not in _LIVE_ITEM_STATUSES:
            continue
        start_dt = order_model._parse_ts(str(task.get("started_at") or ""))
        if start_dt is None:
            continue
        task["elapsed_seconds"] = order_model._duration_seconds(start_dt, now)
        task["active"] = True
        task["needs_confirm"] = status in {"await_confirm", "await_error"}
        task["status_label"] = order_model._STATUS_LABELS.get(status, status)


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
        line_ts = order_model._parse_ts(match.group("ts")) if match is not None else None
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
    registered_at = order_model._parse_ts(str(order.get("registered_at") or ""))
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
    order_codes = order_model._order_item_codes(order)
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
            ts = order_model._parse_ts(match.group("ts"))
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
                        "at": order_model._ts_to_iso(ts),
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
                            "at": order_model._ts_to_iso(ts),
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
                        "at": order_model._ts_to_iso(ts),
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
                            "at": order_model._ts_to_iso(ts),
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
                        "at": order_model._ts_to_iso(ts),
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
                        "at": order_model._ts_to_iso(ts),
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
                    "at": order_model._ts_to_iso(ts),
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
                    "at": order_model._ts_to_iso(ts),
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
        await_at = order_model._ts_to_iso(order_await_at) or await_at

    return {
        "status": aggregate if aggregate != "pending" else "idle",
        "status_label": order_model._STATUS_LABELS.get(
            aggregate if aggregate != "pending" else "idle",
            aggregate,
        ),
        "needs_confirm": needs_confirm,
        "await_kind": await_kind,
        "order_await_active": bool(order_await_active),
        "human_confirm_seen": human_confirm_seen,
        "human_confirm_kind": human_confirm_kind,
        "human_confirm_closed": human_confirm_closed and not needs_confirm,
        "human_confirm_at": order_model._ts_to_iso(
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
