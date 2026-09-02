"""Build the compact abnormal-event input sent to AI."""

from __future__ import annotations

import re
from typing import Dict, List, Mapping

from ksq.feishu.log_parser import clean_log_lines


MAX_EVENTS = 20
_ABNORMAL_RE = re.compile(
    r"timeout|timed out|exception|traceback|\berror\b|\bfailed\b|failure|"
    r"\bwarning\b|\bwarn\b|retry|recover(?:ed)?|重试|恢复|超时|异常|失败|错误|警告",
    re.I,
)
_RETRY_RE = re.compile(r"retry|re-try|再次|重试", re.I)
_RECOVER_RE = re.compile(r"recover(?:ed)?|success|succeed|继续|恢复|完成", re.I)
_FAILURE_RE = re.compile(r"failed|failure|error|exception|失败|错误|异常", re.I)
_EXPECTED_TELEMETRY_RE = re.compile(
    r"current-target\s+error|reached\s+joint\s+near\s+target|total_index\s*>\s*\d+",
    re.I,
)
_TYPE_RULES = (
    ("scan", ("scan", "scanner", "扫码")),
    ("recognize", ("percept", "recogn", "识别", "视觉")),
    ("pick", ("pick_up", "pick", "抓取", "吸取", "真空")),
    ("place", ("place", "packing", "放置")),
    ("navigation", ("navigation", "navigate", "导航", "定位")),
    ("communication", ("http", "grpc", "rpc", "通信", "连接", "broker")),
    ("hardware", ("robotd", "机械臂", "硬件", "motor", "camera", "collision")),
)


def _event_type(text: str) -> str:
    lower = text.lower()
    for event_type, needles in _TYPE_RULES:
        if any(needle.lower() in lower for needle in needles):
            return event_type
    return "unknown"


def _same_type_success(event_type: str, text: str) -> bool:
    return _event_type(text) == event_type and bool(_RECOVER_RE.search(text)) and not bool(
        _FAILURE_RE.search(text)
    )


def build_events(raw_logs: object, context_lines: int = 12) -> List[Dict[str, object]]:
    """Extract at most 20 structured events; never return full log context."""
    rows = clean_log_lines(raw_logs)
    events: List[Dict[str, object]] = []
    seen = set()
    for index, row in enumerate(rows):
        body = str(row.get("body") or "")
        if not _ABNORMAL_RE.search(body):
            continue
        # These INFO lines describe motion convergence/search limits; they are
        # expected telemetry, not a failed test event.
        if _EXPECTED_TELEMETRY_RE.search(body):
            continue
        event_type = _event_type(body)
        message = re.sub(r"\s+", " ", body).strip()[:240]
        key = (event_type, message)
        if key in seen:
            continue
        seen.add(key)
        nearby = [
            str(candidate.get("body") or "")
            for candidate in rows[index + 1 : index + context_lines + 1]
        ]
        retry = bool(_RETRY_RE.search(message)) or any(_RETRY_RE.search(line) for line in nearby)
        recovered = retry and any(_same_type_success(event_type, line) for line in nearby)
        stamp = row.get("time")
        events.append(
            {
                "type": event_type,
                "message": message,
                "retry": retry,
                "recovered": recovered,
                "at": stamp.isoformat().replace("+00:00", "Z") if stamp else "",
            }
        )
    return events[-MAX_EVENTS:]


def has_abnormal_events(events: object) -> bool:
    return isinstance(events, list) and bool(events)


extract_abnormal_events = build_events


if __name__ == "__main__":  # pragma: no cover
    demo = build_events(
        "Scanner timeout\nretry scan\nscan object pipeline success"
    )
    assert demo[0]["retry"] and demo[0]["recovered"]
    print("event_builder self-check ok")
