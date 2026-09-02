"""Submit the selected robot-test form once per work order."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Mapping, Optional, Sequence

from ksq.feishu.client import FeishuApiError, create_bitable_record
from ksq.feishu.pipeline import build_submission
from ksq.feishu.rules import normalize_rule_id
from ksq.web.logs_api import LogServiceError, fetch_logs


_SUBMIT_LOCK = Lock()
_SUBMITTED_KEYS = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _order_dedupe_key(order: Mapping[str, object]) -> str:
    order_no = str(order.get("order_no") or "").strip()
    task_id = str(order.get("task_id") or "").strip()
    return "order:%s" % order_no if order_no else "task:%s" % task_id if task_id else ""


def _feishu_settings(settings: Mapping[str, object]) -> Dict[str, object]:
    raw = settings.get("feishu")
    if not isinstance(raw, Mapping):
        raw = {}
    forms: List[Dict[str, str]] = []
    seen = set()
    for entry in raw.get("forms") if isinstance(raw.get("forms"), list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        form_id = str(entry.get("id") or "").strip() or name
        app_token = str(entry.get("app_token") or "").strip()
        table_id = str(entry.get("table_id") or "").strip()
        if not name or not form_id or not app_token or not table_id or form_id in seen:
            continue
        seen.add(form_id)
        forms.append(
            {
                "id": form_id,
                "name": name,
                "app_token": app_token,
                "table_id": table_id,
                "rule": normalize_rule_id(entry.get("rule")),
            }
        )
    selected = str(raw.get("selected_form") or "").strip()
    if selected not in seen:
        selected = forms[0]["id"] if forms else ""
    return {
        "enabled": bool(raw.get("enabled")),
        "app_id": str(raw.get("app_id") or "").strip(),
        "app_secret": str(raw.get("app_secret") or "").strip(),
        "forms": forms,
        "selected_form": selected,
        "ai": dict(raw.get("ai")) if isinstance(raw.get("ai"), Mapping) else {},
    }


def _selected_form(feishu: Mapping[str, object]) -> Optional[Dict[str, str]]:
    selected = str(feishu.get("selected_form") or "").strip()
    for entry in feishu.get("forms") if isinstance(feishu.get("forms"), list) else []:
        if not isinstance(entry, Mapping) or str(entry.get("id") or "") != selected:
            continue
        return {
            "id": selected,
            "name": str(entry.get("name") or selected),
            "app_id": str(feishu.get("app_id") or ""),
            "app_secret": str(feishu.get("app_secret") or ""),
            "app_token": str(entry.get("app_token") or ""),
            "table_id": str(entry.get("table_id") or ""),
            "rule": normalize_rule_id(entry.get("rule")),
        }
    return None


def _tasks_from_order(order: Mapping[str, object]) -> List[Dict[str, object]]:
    items = order.get("items")
    states = order.get("item_states")
    tasks: List[Dict[str, object]] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("code") or raw.get("sku_id") or raw.get("barcode") or "").strip()
        task = dict(raw)
        if code and isinstance(states, Mapping) and isinstance(states.get(code), Mapping):
            task.update(states[code])  # type: ignore[index]
        if code:
            task["code"] = code
        tasks.append(task)
    if tasks:
        return tasks
    for code, state in states.items() if isinstance(states, Mapping) else []:
        if isinstance(state, Mapping):
            task = dict(state)
            task["code"] = str(code)
            tasks.append(task)
    return tasks


def _robot_logs(order: Optional[Mapping[str, object]] = None) -> tuple[str, str]:
    cached = order.get("feishu_log_cache") if isinstance(order, Mapping) else None
    lines = cached.get("lines") if isinstance(cached, Mapping) else None
    if isinstance(lines, list) and lines:
        return "\n".join(str(line) for line in lines), ""
    try:
        payload = fetch_logs("0", 1200, "")
        return str(payload.get("logs") or ""), ""
    except LogServiceError as error:
        return "", str(error)


def preview_feishu_form(
    order: Optional[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
    dashboard_mode: str,
    settings: Mapping[str, object],
) -> Dict[str, object]:
    del dashboard_mode
    feishu = _feishu_settings(settings)
    form = _selected_form(feishu)
    if form is None:
        raise FeishuApiError("请先配置并选择一个飞书表单。", 400, {})
    working_order = dict(order or {})
    working_tasks = list(tasks) if tasks else _tasks_from_order(working_order)
    raw_logs, log_error = _robot_logs(working_order)
    result = build_submission(
        working_order,
        working_tasks,
        raw_logs,
        form,
        feishu["ai"] if isinstance(feishu.get("ai"), Mapping) else {},
        upload_screenshot=False,
    )
    meta = dict(result["meta"])
    if log_error:
        meta["log_error"] = log_error
    return {
        "ok": True,
        "enabled": bool(feishu.get("enabled")),
        "fields": result["fields"],
        "meta": meta,
        "config": {
            "form_id": form["id"],
            "form_name": form["name"],
            "rule": form["rule"],
            "app_token": form["app_token"],
            "table_id": form["table_id"],
        },
    }


def _order_outcome_ready(
    await_kind: object, order: Optional[Mapping[str, object]]
) -> bool:
    kind = str(await_kind or "").strip().lower()
    if kind in {"pack", "error"}:
        return True
    if order is None:
        return False
    lifecycle = order.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    blob = "%s %s" % (
        str(lifecycle.get("end_reason") or "").lower(),
        str(lifecycle.get("broker_status") or "").lower(),
    )
    return any(
        token in blob
        for token in (
            "human_pack",
            "human_error",
            "broker_success",
            "broker_error",
            "broker_cancel",
            "awaiting_pack",
            "items_done",
            "items_failed",
            "claimed",
            "transferred",
            "cancel",
        )
    )


def should_submit_on_human_prompt(
    needs_confirm: object,
    human_confirm_seen: object,
    await_kind: object,
    order: Optional[Mapping[str, object]],
) -> bool:
    del needs_confirm, human_confirm_seen
    return _order_outcome_ready(await_kind, order)


def should_submit_on_confirm(
    order: Optional[Mapping[str, object]], await_kind: object
) -> bool:
    return _order_outcome_ready(await_kind, order)


def should_submit_on_closed(order: Optional[Mapping[str, object]]) -> bool:
    return _order_outcome_ready("", order)


def _parse_iso(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pending_stale(state: Mapping[str, object], seconds: int = 90) -> bool:
    attempted = _parse_iso(state.get("at"))
    return bool(state.get("pending")) and not state.get("submitted") and (
        attempted is None
        or (datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)).total_seconds()
        >= seconds
    )


def _already_submitted(order: Mapping[str, object]) -> bool:
    state = order.get("feishu_submit")
    return isinstance(state, Mapping) and (
        bool(state.get("submitted"))
        or (bool(state.get("pending")) and not _pending_stale(state))
    )


def _recent_failed(order: Mapping[str, object], seconds: int = 120) -> bool:
    state = order.get("feishu_submit")
    if not isinstance(state, Mapping) or state.get("ok"):
        return False
    attempted = _parse_iso(state.get("at"))
    return attempted is not None and (
        datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)
    ).total_seconds() < seconds


def clear_feishu_dedupe_key(order: Mapping[str, object]) -> None:
    key = _order_dedupe_key(order)
    if key:
        _SUBMITTED_KEYS.discard(key)


def _save_state(
    order: Dict[str, object], state: Dict[str, object], callback: object
) -> None:
    order["feishu_submit"] = state
    if callable(callback):
        callback(order)


def maybe_submit_feishu_form(
    order: Optional[Dict[str, object]],
    tasks: Sequence[Mapping[str, object]],
    dashboard_mode: str,
    settings: Mapping[str, object],
    trigger: str,
    persist_callback: object,
    load_active_callback: object,
) -> Dict[str, object]:
    del dashboard_mode
    if order is None:
        return {"ok": False, "skipped": True, "reason": "no_order"}
    feishu = _feishu_settings(settings)
    # The switch controls automatic submissions only. The settings page's
    # manual submit action is an explicit request and must remain available.
    if not feishu.get("enabled") and trigger != "manual":
        return {"ok": False, "skipped": True, "reason": "disabled"}

    with _SUBMIT_LOCK:
        key = _order_dedupe_key(order)
        if not key:
            return {"ok": False, "skipped": True, "reason": "missing_order_identity"}
        if callable(load_active_callback):
            active = load_active_callback()
            if isinstance(active, dict) and _order_dedupe_key(active) == key:
                if isinstance(active.get("feishu_submit"), dict):
                    order["feishu_submit"] = deepcopy(active["feishu_submit"])
                if _already_submitted(active):
                    _SUBMITTED_KEYS.add(key)
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "already_submitted",
                        "previous": deepcopy(active.get("feishu_submit")),
                    }
        state = order.get("feishu_submit")
        if isinstance(state, Mapping) and _pending_stale(state):
            _SUBMITTED_KEYS.discard(key)
        if key in _SUBMITTED_KEYS or _already_submitted(order):
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_submitted",
                "previous": deepcopy(order.get("feishu_submit")),
            }
        if trigger not in {"confirm", "manual", "confirm_fallback"} and _recent_failed(order):
            return {
                "ok": False,
                "skipped": True,
                "reason": "recent_failure_cooldown",
                "previous": deepcopy(order.get("feishu_submit")),
            }

        form = _selected_form(feishu)
        if form is None:
            return {
                "ok": False,
                "skipped": False,
                "error": "请先配置并选择一个飞书表单。",
                "status_code": 400,
            }
        _SUBMITTED_KEYS.add(key)
        _save_state(
            order,
            {
                "submitted": False,
                "ok": False,
                "pending": True,
                "trigger": trigger,
                "at": _now_iso(),
            },
            persist_callback,
        )
        try:
            raw_logs, log_error = _robot_logs(order)
            pipeline = build_submission(
                order,
                list(tasks) if tasks else _tasks_from_order(order),
                raw_logs,
                form,
                feishu["ai"] if isinstance(feishu.get("ai"), Mapping) else {},
                upload_screenshot=True,
            )
            fields = pipeline["fields"]
            meta = dict(pipeline["meta"])
            if log_error:
                meta["log_error"] = log_error
            result = create_bitable_record(
                form["app_id"],
                form["app_secret"],
                form["app_token"],
                form["table_id"],
                fields,
            )
        except FeishuApiError as error:
            _SUBMITTED_KEYS.discard(key)
            failed = {
                "submitted": False,
                "ok": False,
                "pending": False,
                "trigger": trigger,
                "at": _now_iso(),
                "error": str(error),
                "status_code": error.status_code,
                "body": error.body,
            }
            _save_state(order, failed, persist_callback)
            return {
                "ok": False,
                "skipped": False,
                "error": str(error),
                "status_code": error.status_code,
                "body": error.body,
            }
        except Exception as error:  # noqa: BLE001 - submission must not crash dashboard polling
            _SUBMITTED_KEYS.discard(key)
            failed = {
                "submitted": False,
                "ok": False,
                "pending": False,
                "trigger": trigger,
                "at": _now_iso(),
                "error": str(error),
            }
            _save_state(order, failed, persist_callback)
            return {"ok": False, "skipped": False, "error": str(error)}

        submitted = {
            "submitted": True,
            "ok": True,
            "pending": False,
            "trigger": trigger,
            "at": _now_iso(),
            "record_id": result.get("record_id") or "",
            "form_id": form["id"],
            "fields": fields,
            "meta": meta,
        }
        _save_state(order, submitted, persist_callback)
        return {
            "ok": True,
            "skipped": False,
            "record_id": result.get("record_id") or "",
            "fields": fields,
            "meta": meta,
            "trigger": trigger,
        }
