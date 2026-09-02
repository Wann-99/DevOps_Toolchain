"""Parse robot logs into deterministic task/SKU facts for Feishu."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]"
)
_TS_RE = re.compile(
    r"^(?:\[(?P<bracket_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\]\s*"
    r"|(?P<iso_ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+)"
    r"(?P<body>.*)$"
)
_TASK_ITEM_RE = re.compile(
    r"MedicinePickUpTaskItem\(code=([^,\s]+),\s*task_id=([^,\s]+),\s*seq_id=([^)]+)\)"
)
_START_RE = re.compile(
    r"start process object\s*\{[^}]*'(?:code|sku_id)':\s*'([^']+)'", re.I
)
_BARCODE_RE = re.compile(r"'barcode':\s*'([^']+)'", re.I)
_SUBTASK_RE = re.compile(
    r"SubTaskDetail\(.*?barcode=['\"]([^'\"]+)['\"].*?"
    r"sku_id=['\"]([^'\"]+)['\"].*?end_tools=\[([^\]]*)\]",
    re.I,
)
_CODE_RE = re.compile(r"\b(?:code|sku_id)\s*[:=]\s*['\"]?([\w.-]+)", re.I)
_TASK_CONTEXT_CODE_RE = re.compile(
    r"\btask_id:\s*[^;\s]*-(sku-[^;\s]+)-\d+\s*;", re.I
)
_TOOL_RE = re.compile(r"\btool=([\w.-]+)", re.I)
_PROCESS_END_RE = re.compile(r"\bitem\s+([^\s]+)\s+process\s+end\s+time", re.I)
_ITEM_RE = re.compile(r"\bitem\s+(\S+)\s+process\s+(start|end)", re.I)
_CASE_RE = re.compile(r"\b(?:case_id|test_case_id)\s*[:=]\s*['\"]?([\w.-]+)", re.I)

STAGES = ("recognize", "pick", "scan", "outbound", "place")
_SUCCESS = {
    "recognize": ("recognize success", "recognition success", "识别成功", "percept success"),
    "pick": ("pick_up_object success", "pick success", "抓取成功", "吸取成功"),
    "scan": ("scan object pipeline success", "scan success", "扫码成功", "scan result success"),
    "outbound": ("outbound success", "take out success", "出库成功"),
    "place": ("place object pipeline success", "place success", "放置成功", "packing task success"),
}
_FAILURE = {
    "recognize": (
        "not found in percept_pusher results",
        "find object and shelf failed",
        "object is marked as unavailable",
        "recognize failed",
        "识别失败",
    ),
    "pick": ("pick_up_object failed", "pick failed", "抓取失败", "吸取失败"),
    "scan": (
        "scan object pipeline failed",
        "check scan object result failed",
        "scan failed",
        "扫码失败",
    ),
    "outbound": ("outbound failed", "take out failed", "出库失败"),
    "place": ("packing task failed", "place failed", "放置失败"),
}


def clean_log_lines(raw_logs: object) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for raw in _ANSI_RE.sub("", str(raw_logs or "")).splitlines():
        text = raw.strip()
        if not text:
            continue
        match = _TS_RE.match(text)
        stamp: Optional[datetime] = None
        body = text
        if match is not None:
            body = match.group("body")
            try:
                bracket_ts = match.group("bracket_ts")
                if bracket_ts:
                    stamp = datetime.strptime(
                        bracket_ts, "%Y-%m-%d %H:%M:%S,%f"
                    ).replace(tzinfo=timezone(timedelta(hours=8)))
                else:
                    stamp = datetime.fromisoformat(
                        match.group("iso_ts").replace("Z", "+00:00")
                    )
            except ValueError:
                stamp = None
        rows.append({"body": body, "time": stamp, "raw": text})
    return rows


def _expected_codes(expected_skus: Iterable[object]) -> set:
    codes = set()
    for item in expected_skus:
        if isinstance(item, Mapping):
            for key in ("code", "sku_id", "barcode"):
                value = str(item.get(key) or "").strip()
                if value:
                    codes.add(value)
        else:
            value = str(item or "").strip()
            if value:
                codes.add(value)
    return codes


def _barcode_aliases(rows: Iterable[Mapping[str, object]]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for row in rows:
        body = str(row.get("body") or "")
        start = _START_RE.search(body)
        barcode = _BARCODE_RE.search(body)
        if start and barcode:
            code = start.group(1).strip()
            value = barcode.group(1).strip()
            if code and value:
                aliases[code] = value
        detail = _SUBTASK_RE.search(body)
        if detail:
            aliases[detail.group(2).strip()] = detail.group(1).strip()
    return aliases


def scope_robot_log(
    raw_logs: object,
    task_id: object = "",
    expected_skus: Iterable[object] = (),
) -> str:
    """Select the current task segment so historical events cannot leak into it."""
    rows = clean_log_lines(raw_logs)
    if not rows:
        return ""
    wanted_task = str(task_id or "").strip()
    wanted_codes = _expected_codes(expected_skus)
    aliases = _barcode_aliases(rows)
    wanted_codes.update(aliases.get(code, code) for code in tuple(wanted_codes))
    markers = []
    for index, row in enumerate(rows):
        match = _TASK_ITEM_RE.search(str(row.get("body") or ""))
        if match:
            raw_code = match.group(1).strip()
            markers.append((index, aliases.get(raw_code, raw_code), match.group(2).strip()))

    selected_task = ""
    exact = [marker for marker in markers if wanted_task and marker[2] == wanted_task]
    if exact:
        selected_task = wanted_task
    elif wanted_codes:
        candidates: Dict[str, tuple] = {}
        for index, code, parent in markers:
            if code not in wanted_codes:
                continue
            overlap, _last = candidates.get(parent, (set(), -1))
            candidates[parent] = (set(overlap) | {code}, index)
        if candidates:
            selected_task = max(
                candidates,
                key=lambda parent: (len(candidates[parent][0]), candidates[parent][1]),
            )
    if selected_task:
        selected = [index for index, _code, parent in markers if parent == selected_task]
        start = min(selected)
        # Robot emits the TaskItem line near the end of a SKU. Include the
        # earlier tool/start/scan lines for the same parent task as well.
        task_occurrences = [
            index
            for index, row in enumerate(rows[:start])
            if selected_task in str(row.get("body") or "")
        ]
        if task_occurrences:
            start = min(task_occurrences)
        for index in range(start - 1, max(-1, start - 6), -1):
            body = str(rows[index].get("body") or "")
            if _CASE_RE.search(body):
                start = index
                break
        last = max(selected)
        end = next(
            (index for index, _code, parent in markers if index > last and parent != selected_task),
            len(rows),
        )
        return "\n".join(str(row["raw"]) for row in rows[start:end])

    if wanted_codes:
        matching_starts = []
        for index, row in enumerate(rows):
            body = str(row.get("body") or "")
            start = _START_RE.search(body)
            barcode = _BARCODE_RE.search(body)
            values = {
                start.group(1).strip() if start else "",
                barcode.group(1).strip() if barcode else "",
            }
            if wanted_codes & values:
                matching_starts.append(index)
        if matching_starts:
            start = matching_starts[-1]
            return "\n".join(str(row["raw"]) for row in rows[start:])
    return "\n".join(str(row["raw"]) for row in rows)


def _iso(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return text


def _stamp_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _new_sku(code: str, barcode: str = "") -> Dict[str, object]:
    return {
        "code": code,
        "barcode": barcode,
        "tool": "",
        "executed": False,
        "recognize": None,
        "pick": None,
        "scan": None,
        "outbound": None,
        "place": None,
    }


def _ensure(
    items: Dict[str, Dict[str, object]], code: str, barcode: str = ""
) -> Dict[str, object]:
    item = items.setdefault(code, _new_sku(code, barcode))
    if barcode:
        item["barcode"] = barcode
    return item


def _matching_stage(body: str, patterns: Mapping[str, Iterable[str]]) -> str:
    lower = body.lower()
    for stage, needles in patterns.items():
        if any(needle.lower() in lower for needle in needles):
            return stage
    return ""


def _merge_task_state(item: Dict[str, object], task: Mapping[str, object]) -> None:
    status = str(task.get("status") or "").strip().lower()
    if task.get("started_at"):
        item["started_at"] = _iso(task.get("started_at"))
    if task.get("ended_at"):
        item["ended_at"] = _iso(task.get("ended_at"))
    if status in {"success", "failed", "await_error", "processing", "started", "await_confirm"}:
        item["executed"] = True
    if status == "success":
        for stage in STAGES:
            if item.get(stage) is None:
                item[stage] = True
        return
    if status not in {"failed", "await_error"}:
        return
    evidence = str(task.get("end_line") or task.get("await_line") or "")
    failed_stage = _matching_stage(evidence, _FAILURE)
    if not failed_stage:
        return
    for stage in STAGES:
        if stage == failed_stage:
            item[stage] = False
            break
        if item.get(stage) is None:
            item[stage] = True


def _merge_expected_barcode_aliases(
    items: Dict[str, Dict[str, object]],
    aliases: Mapping[str, str],
    expected: Mapping[str, Mapping[str, object]],
) -> None:
    """Join a log internal sku_id to an order keyed by its 69-code."""
    for source, target in aliases.items():
        if source == target or source not in items or target not in expected:
            continue
        destination = items.setdefault(target, _new_sku(target, target))
        origin = items[source]
        for key, value in origin.items():
            if key in {"code", "barcode"} or value in (None, "", [], {}):
                continue
            if destination.get(key) in (None, "", [], {}):
                destination[key] = value
        if not destination.get("barcode"):
            destination["barcode"] = target
        del items[source]


def parse_robot_log(
    raw_logs: object,
    task_id: object = "",
    expected_skus: Iterable[object] = (),
    status_hint: object = "",
    case_id: object = "",
) -> Dict[str, object]:
    """Return only facts proven by logs or the dashboard's parsed task state."""
    expected_skus = list(expected_skus)
    rows = clean_log_lines(scope_robot_log(raw_logs, task_id, expected_skus))
    aliases = _barcode_aliases(rows)
    # Keep the stable internal SKU id as `code`; barcode is a separate field.
    # Aliases are used for log scoping, not as a replacement identifier.
    canonical = lambda value: str(value or "").strip()
    items: Dict[str, Dict[str, object]] = {}
    task_rows: Dict[str, Mapping[str, object]] = {}
    task_ids: List[str] = []
    parsed_case_id = ""
    current_code = ""
    saw_failure = False
    saw_success = False
    start_times: List[datetime] = []
    end_times: List[datetime] = []

    for expected in expected_skus:
        if isinstance(expected, Mapping):
            raw_code = str(
                expected.get("code")
                or expected.get("sku_id")
                or expected.get("barcode")
                or ""
            ).strip()
            barcode = str(expected.get("barcode") or "").strip()
            code = canonical(raw_code)
            if code:
                item = _ensure(items, code, barcode)
                expected_tool = str(expected.get("tool") or "").strip()
                if not expected_tool:
                    end_tools = expected.get("end_tools")
                    if isinstance(end_tools, (list, tuple)) and end_tools:
                        expected_tool = str(end_tools[0] or "").strip()
                if expected_tool:
                    item["tool"] = expected_tool
                task_rows[code] = expected
        else:
            code = str(expected or "").strip()
            if code:
                _ensure(items, code)

    for row in rows:
        body = str(row["body"])
        stamp = row.get("time")
        case_match = _CASE_RE.search(body)
        if case_match:
            parsed_case_id = case_match.group(1).strip()
        task_match = _TASK_ITEM_RE.search(body)
        if task_match:
            current_code = canonical(task_match.group(1))
            parent = task_match.group(2).strip()
            if parent and parent not in task_ids:
                task_ids.append(parent)
            _ensure(items, current_code)

        context_match = _TASK_CONTEXT_CODE_RE.search(body)
        if context_match:
            current_code = context_match.group(1).strip()
            item = _ensure(items, current_code)
            if (
                isinstance(stamp, datetime)
                and re.search(r"current_event:\s*start\s+(?:pick|process)", body, re.I)
                and not item.get("started_at")
            ):
                item["started_at"] = _stamp_iso(stamp)
                start_times.append(stamp)

        code_match = _CODE_RE.search(body)
        if code_match and not re.search(r"\bprior_code\s*=", body, re.I):
            current_code = code_match.group(1).strip()
            _ensure(items, current_code)

        detail = _SUBTASK_RE.search(body)
        if detail:
            current_code = detail.group(2).strip()
            item = _ensure(items, current_code, detail.group(1).strip())
            tools = [value.strip(" '\"") for value in detail.group(3).split(",")]
            if tools and tools[0]:
                item["tool"] = tools[0]

        tool_match = _TOOL_RE.search(body)
        if tool_match and current_code:
            _ensure(items, current_code)["tool"] = tool_match.group(1).strip()

        start_match = _START_RE.search(body)
        if start_match:
            current_code = canonical(start_match.group(1))
            barcode_match = _BARCODE_RE.search(body)
            item = _ensure(
                items,
                current_code,
                barcode_match.group(1).strip() if barcode_match else "",
            )
            item["executed"] = True
            if isinstance(stamp, datetime):
                start_times.append(stamp)

        item_match = _ITEM_RE.search(body)
        if item_match:
            current_code = canonical(item_match.group(1))
            item = _ensure(items, current_code)
            if item_match.group(2).lower() == "start":
                item["executed"] = True
                if isinstance(stamp, datetime):
                    start_times.append(stamp)
            elif isinstance(stamp, datetime):
                end_times.append(stamp)

        end_match = _PROCESS_END_RE.search(body)
        if end_match:
            current_code = end_match.group(1).strip()
            item = _ensure(items, current_code)
            item["executed"] = True
            if isinstance(stamp, datetime):
                item["ended_at"] = _stamp_iso(stamp)
                end_times.append(stamp)

        success_stage = _matching_stage(body, _SUCCESS)
        if success_stage and current_code:
            _ensure(items, current_code)[success_stage] = True
        if re.search(r"\bscan_object\s+final\s+codes|\bscan\s+object\s+\{", body, re.I):
            if current_code:
                _ensure(items, current_code)["scan"] = True
        failed_stage = _matching_stage(body, _FAILURE)
        if failed_stage:
            saw_failure = True
            if current_code:
                item = _ensure(items, current_code)
                item["executed"] = True
                item[failed_stage] = False
            if isinstance(stamp, datetime):
                end_times.append(stamp)
        lower = body.lower()
        if re.search(r"\b(?:task|order)\s+(?:failed|error)\b|任务失败", lower):
            saw_failure = True
        if re.search(
            r"(?:process|task|order)\s+(?:success|succeeded|completed)|"
            r"TaskItem:.*succeeded|任务完成",
            lower,
        ):
            saw_success = True
            if isinstance(stamp, datetime):
                end_times.append(stamp)

    _merge_expected_barcode_aliases(items, aliases, task_rows)
    for code, task in task_rows.items():
        _merge_task_state(_ensure(items, code), task)

    hinted = str(status_hint or "").strip().upper()
    if hinted in {"FAILED", "SUCCESS"}:
        task_status = hinted
    elif saw_failure or any(
        item.get(stage) is False for item in items.values() for stage in STAGES
    ):
        task_status = "FAILED"
    elif saw_success or (
        items
        and all(
            str(task_rows.get(code, {}).get("status") or "").lower() == "success"
            for code in items
        )
    ):
        task_status = "SUCCESS"
    else:
        task_status = "UNKNOWN"

    sku_rows = list(items.values())
    starts = [
        _iso(item.get("started_at")) for item in sku_rows if item.get("started_at")
    ]
    ends = [_iso(item.get("ended_at")) for item in sku_rows if item.get("ended_at")]
    ends.extend(
        _stamp_iso(stamp)
        for stamp in end_times
    )
    result: Dict[str, object] = {
        "case_id": str(case_id or "").strip() or parsed_case_id,
        "task_id": str(task_id or "").strip() or (task_ids[-1] if task_ids else ""),
        "start_time": min(starts)
        if starts
        else (
            _stamp_iso(min(start_times))
            if start_times
            else ""
        ),
        "end_time": max(ends)
        if ends
        else (
            _stamp_iso(max(end_times))
            if end_times
            else ""
        ),
        "task_status": task_status,
        "skus": sku_rows,
    }
    for index, item in enumerate(sku_rows, start=1):
        result["sku%d" % index] = item
    return result


if __name__ == "__main__":  # pragma: no cover
    parsed = parse_robot_log(
        "2026-08-20T10:00:00Z start process object {'code': 'sku-a', 'barcode': '690001'}\n"
        "2026-08-20T10:00:01Z scan object pipeline success\n"
        "2026-08-20T10:00:02Z task success\n"
    )
    assert parsed["task_status"] == "SUCCESS"
    assert parsed["sku1"]["barcode"] == "690001"
    print("log_parser self-check ok")
