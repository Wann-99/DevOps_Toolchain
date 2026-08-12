"""Submit work-order form rows to Feishu with per-order dedupe."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Mapping, Optional, Sequence

from ksq.feishu.client import (
    FeishuApiError,
    create_bitable_record,
    list_bitable_select_options,
    upload_bitable_media,
)
from ksq.feishu.failure_evidence import collect_failure_evidence
from ksq.feishu.form_payload import (
    DEFAULT_FIELD_NAMES,
    DEFAULT_SITE,
    OUTCOME_FAILED,
    build_feishu_form_fields,
)
from ksq.web.logs_api import LogServiceError, fetch_logs

_SUBMIT_LOCK = Lock()
# Process-local dedupe by order/task id (covers concurrent poll races).
_SUBMITTED_KEYS = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _order_dedupe_key(order: Mapping[str, object]) -> str:
    order_no = str(order.get("order_no") or "").strip()
    task_id = str(order.get("task_id") or "").strip()
    if order_no:
        return "order:%s" % order_no
    if task_id:
        return "task:%s" % task_id
    return ""


def _feishu_settings(settings: Mapping[str, object]) -> Dict[str, object]:
    raw = settings.get("feishu")
    if not isinstance(raw, dict):
        return {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "app_token": "",
            "table_id": "",
            "tester": "",
            "site": DEFAULT_SITE,
            "field_names": {},
        }
    field_names = raw.get("field_names")
    if not isinstance(field_names, dict):
        field_names = {}
    site = str(raw.get("site") or "").strip() or DEFAULT_SITE
    return {
        "enabled": bool(raw.get("enabled")),
        "app_id": str(raw.get("app_id") or "").strip(),
        "app_secret": str(raw.get("app_secret") or "").strip(),
        "app_token": str(raw.get("app_token") or "").strip(),
        "table_id": str(raw.get("table_id") or "").strip(),
        "tester": str(raw.get("tester") or "").strip(),
        "site": site,
        "field_names": field_names,
    }


def _tasks_from_order(order: Mapping[str, object]) -> List[Dict[str, object]]:
    states = order.get("item_states")
    items = order.get("items")
    merged: List[Dict[str, object]] = []
    if isinstance(items, list):
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            code = str(raw.get("code") or raw.get("barcode") or "").strip()
            row = dict(raw)
            if isinstance(states, Mapping) and code and isinstance(states.get(code), Mapping):
                state = dict(states[code])  # type: ignore[index]
                state.update(
                    {
                        "index": raw.get("index"),
                        "item_id": raw.get("item_id") or code,
                        "barcode": raw.get("barcode") or code,
                        "name": raw.get("name") or state.get("name") or "",
                        "location_code": raw.get("location_code")
                        or state.get("location_code")
                        or "",
                        "quantity": raw.get("quantity") or 1,
                        "code": code,
                    }
                )
                merged.append(state)
            else:
                merged.append(dict(row))
        return merged
    if isinstance(states, Mapping):
        for code, state in states.items():
            if isinstance(state, Mapping):
                row = dict(state)
                row["code"] = str(code)
                merged.append(row)
    return merged


def preview_feishu_form(
    order: Optional[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
    dashboard_mode: str,
    settings: Mapping[str, object],
) -> Dict[str, object]:
    feishu = _feishu_settings(settings)
    working_order = dict(order or {})
    working_tasks: List[Mapping[str, object]]
    if tasks:
        working_tasks = list(tasks)
    else:
        working_tasks = _tasks_from_order(working_order)
    fields, meta = build_feishu_form_fields(
        working_order,
        working_tasks,
        dashboard_mode,
        str(feishu.get("tester") or ""),
        feishu.get("field_names"),
        str(feishu.get("site") or ""),
    )
    return {
        "ok": True,
        "enabled": bool(feishu.get("enabled")),
        "fields": fields,
        "meta": meta,
        "config": {
            "app_id": str(feishu.get("app_id") or ""),
            "app_token": str(feishu.get("app_token") or ""),
            "table_id": str(feishu.get("table_id") or ""),
            "tester": str(feishu.get("tester") or ""),
            "site": str(feishu.get("site") or ""),
            "has_app_secret": bool(str(feishu.get("app_secret") or "").strip()),
        },
    }


def fetch_feishu_site_options(settings: Mapping[str, object]) -> Dict[str, object]:
    feishu = _feishu_settings(settings)
    field_names = feishu.get("field_names")
    site_field = DEFAULT_FIELD_NAMES["site"]
    if isinstance(field_names, Mapping):
        override = str(field_names.get("site") or "").strip()
        if override:
            site_field = override
    options = list_bitable_select_options(
        str(feishu.get("app_id") or ""),
        str(feishu.get("app_secret") or ""),
        str(feishu.get("app_token") or ""),
        str(feishu.get("table_id") or ""),
        site_field,
    )
    selected = str(feishu.get("site") or "").strip() or DEFAULT_SITE
    return {
        "ok": True,
        "field_name": site_field,
        "options": options,
        "site": selected,
    }


def _order_outcome_ready(
    await_kind: object, order: Optional[Mapping[str, object]]
) -> bool:
    """Feishu 达成情况是工单状态：仅在打包/报错/取消等工单级门禁提交。"""
    kind = str(await_kind or "").strip().lower()
    if kind in {"pack", "error"}:
        return True
    if order is None:
        return False
    lifecycle = order.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    reason = str(lifecycle.get("end_reason") or "").strip().lower()
    broker = str(lifecycle.get("broker_status") or "").strip().lower()
    blob = "%s %s" % (reason, broker)
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
    """Submit on order-level speak/gate (pack/error/cancel), not mid-item confirm key."""
    if not (bool(needs_confirm) or bool(human_confirm_seen)):
        # Also allow ended lifecycle without an open await (broker terminal).
        return _order_outcome_ready(await_kind, order)
    return _order_outcome_ready(await_kind, order)


def should_submit_on_confirm(
    order: Optional[Mapping[str, object]], await_kind: object
) -> bool:
    return should_submit_on_human_prompt(False, False, await_kind, order)


def should_submit_on_closed(order: Optional[Mapping[str, object]]) -> bool:
    return _order_outcome_ready("", order)


def _pending_stale(state: Mapping[str, object], max_age_seconds: int) -> bool:
    if not bool(state.get("pending")):
        return False
    if bool(state.get("submitted")):
        return False
    attempted = _parse_iso(state.get("at"))
    if attempted is None:
        return True
    age = (datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)).total_seconds()
    return age >= float(max_age_seconds)


def _already_submitted(order: Mapping[str, object]) -> bool:
    state = order.get("feishu_submit")
    if not isinstance(state, Mapping):
        return False
    if bool(state.get("submitted")):
        return True
    # In-flight claim blocks concurrent polls; stale pending is retryable.
    if bool(state.get("pending")) and not _pending_stale(state, 90):
        return True
    return False


def clear_feishu_dedupe_key(order: Mapping[str, object]) -> None:
    key = _order_dedupe_key(order)
    if key:
        _SUBMITTED_KEYS.discard(key)


def _parse_iso(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _recent_failed_attempt(order: Mapping[str, object], cooldown_seconds: int) -> bool:
    state = order.get("feishu_submit")
    if not isinstance(state, Mapping):
        return False
    if bool(state.get("ok")):
        return False
    attempted = _parse_iso(state.get("at"))
    if attempted is None:
        return False
    age = (datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)).total_seconds()
    return age < float(cooldown_seconds)


def maybe_submit_feishu_form(
    order: Optional[Dict[str, object]],
    tasks: Sequence[Mapping[str, object]],
    dashboard_mode: str,
    settings: Mapping[str, object],
    trigger: str,
    persist_callback: object,
    load_active_callback: object,
) -> Dict[str, object]:
    """
    Submit once per order when enabled.

    persist_callback(order_dict) must persist feishu_submit onto active order.
    load_active_callback() must return the latest active order (or None).
    """
    if order is None:
        return {"ok": False, "skipped": True, "reason": "no_order"}

    feishu = _feishu_settings(settings)
    if not feishu.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "disabled"}

    with _SUBMIT_LOCK:
        dedupe_key = _order_dedupe_key(order)
        # Submit is per work order — require order_no or task_id.
        if not dedupe_key:
            return {
                "ok": False,
                "skipped": True,
                "reason": "missing_order_identity",
            }
        # Always re-check the live active order inside the lock. Concurrent
        # dashboard polls each hold a stale deepcopy without feishu_submit.
        if callable(load_active_callback):
            active = load_active_callback()
            if isinstance(active, dict):
                active_key = _order_dedupe_key(active)
                same_order = bool(dedupe_key) and dedupe_key == active_key
                if same_order and isinstance(active.get("feishu_submit"), dict):
                    order["feishu_submit"] = deepcopy(active.get("feishu_submit"))
                active_state = active.get("feishu_submit")
                if (
                    same_order
                    and isinstance(active_state, Mapping)
                    and _pending_stale(active_state, 90)
                    and dedupe_key
                ):
                    _SUBMITTED_KEYS.discard(dedupe_key)
                if same_order and _already_submitted(active):
                    previous = active.get("feishu_submit")
                    if dedupe_key:
                        _SUBMITTED_KEYS.add(dedupe_key)
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": "already_submitted",
                        "previous": deepcopy(previous)
                        if isinstance(previous, dict)
                        else previous,
                    }

        order_state = order.get("feishu_submit")
        if (
            isinstance(order_state, Mapping)
            and _pending_stale(order_state, 90)
            and dedupe_key
        ):
            _SUBMITTED_KEYS.discard(dedupe_key)

        if dedupe_key and dedupe_key in _SUBMITTED_KEYS:
            previous = order.get("feishu_submit")
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_submitted_memory",
                "previous": deepcopy(previous) if isinstance(previous, dict) else previous,
            }

        if _already_submitted(order):
            previous = order.get("feishu_submit")
            if dedupe_key:
                _SUBMITTED_KEYS.add(dedupe_key)
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_submitted",
                "previous": deepcopy(previous) if isinstance(previous, dict) else previous,
            }

        # Poll retries are rate-limited; explicit confirm/manual may retry.
        if trigger not in {"confirm", "manual", "confirm_fallback"} and _recent_failed_attempt(
            order, 120
        ):
            previous = order.get("feishu_submit")
            return {
                "ok": False,
                "skipped": True,
                "reason": "recent_failure_cooldown",
                "previous": deepcopy(previous) if isinstance(previous, dict) else previous,
            }

        # Claim before network I/O so waiters see the in-flight submit.
        if dedupe_key:
            _SUBMITTED_KEYS.add(dedupe_key)
        order["feishu_submit"] = {
            "submitted": False,
            "ok": False,
            "pending": True,
            "trigger": trigger,
            "at": _now_iso(),
        }
        if callable(persist_callback):
            persist_callback(order)

        fields: Dict[str, object] = {}
        meta: Dict[str, object] = {}
        try:
            preview = preview_feishu_form(order, tasks, dashboard_mode, settings)
            preview_fields = preview["fields"]
            preview_meta = preview["meta"]
            fields = preview_fields if isinstance(preview_fields, dict) else {}
            meta = preview_meta if isinstance(preview_meta, dict) else {}
            if not fields:
                if dedupe_key:
                    _SUBMITTED_KEYS.discard(dedupe_key)
                order["feishu_submit"] = {
                    "submitted": False,
                    "ok": False,
                    "pending": False,
                    "trigger": trigger,
                    "at": _now_iso(),
                    "error": "empty_fields",
                }
                if callable(persist_callback):
                    persist_callback(order)
                return {"ok": False, "skipped": True, "reason": "empty_fields"}

            # 问题描述 / 问题截图：仅达成情况=失败时填写
            outcome = str((meta or {}).get("outcome") or "")
            if outcome == OUTCOME_FAILED:
                raw_logs = ""
                try:
                    logs_payload = fetch_logs("0", 800, "")
                    raw_logs = str(logs_payload.get("logs") or "")
                except LogServiceError as error:
                    raw_logs = ""
                    meta = dict(meta or {})
                    meta["log_fetch_error"] = str(error)
                evidence = collect_failure_evidence(
                    order,
                    tasks if tasks else _tasks_from_order(order),
                    outcome,
                    raw_logs,
                    order.get("await_kind"),
                    order.get("await_line"),
                )
                if evidence is not None:
                    names = (meta or {}).get("field_names") or {}
                    desc_key = str(names.get("problem_desc") or "问题描述")
                    media_key = str(names.get("problem_media") or "问题截图/视频")
                    description = str(evidence.get("description") or "")
                    fields[desc_key] = description
                    attachments = []
                    upload_errors: List[str] = []

                    def _try_upload(file_name: str, content: bytes) -> None:
                        try:
                            file_token = upload_bitable_media(
                                str(feishu.get("app_id") or ""),
                                str(feishu.get("app_secret") or ""),
                                str(feishu.get("app_token") or ""),
                                file_name,
                                content,
                            )
                        except FeishuApiError as upload_error:
                            upload_errors.append(
                                "%s: %s" % (file_name, str(upload_error))
                            )
                            return
                        attachments.append({"file_token": file_token})

                    png_bytes = evidence.get("png_bytes")
                    if isinstance(png_bytes, (bytes, bytearray)) and png_bytes:
                        _try_upload(
                            str(evidence.get("png_name") or "robot_error.png"),
                            bytes(png_bytes),
                        )
                    txt_body = str(evidence.get("txt_body") or "")
                    if txt_body:
                        _try_upload(
                            str(evidence.get("txt_name") or "robot_error.txt"),
                            txt_body.encode("utf-8"),
                        )
                    if attachments:
                        fields[media_key] = attachments
                    # 附件权限不足时仍写入主记录；问题描述保留，并注明附件未上传。
                    if upload_errors:
                        note = (
                            "\n附件未上传（飞书应用缺少 drive/media 权限）："
                            + "; ".join(upload_errors)
                        )
                        fields[desc_key] = description + note
                    meta = dict(meta or {})
                    meta["failure_evidence"] = {
                        "error_line": evidence.get("error_line") or "",
                        "window_line_count": len(evidence.get("window_lines") or []),
                        "attachment_count": len(attachments),
                        "upload_errors": upload_errors,
                    }

            result = create_bitable_record(
                str(feishu.get("app_id") or ""),
                str(feishu.get("app_secret") or ""),
                str(feishu.get("app_token") or ""),
                str(feishu.get("table_id") or ""),
                fields,
            )
        except FeishuApiError as error:
            if dedupe_key:
                _SUBMITTED_KEYS.discard(dedupe_key)
            state = {
                "submitted": False,
                "ok": False,
                "pending": False,
                "trigger": trigger,
                "at": _now_iso(),
                "error": str(error),
                "status_code": error.status_code,
                "body": error.body,
                "fields": fields,
                "meta": meta,
            }
            order["feishu_submit"] = state
            if callable(persist_callback):
                persist_callback(order)
            return {
                "ok": False,
                "skipped": False,
                "error": str(error),
                "status_code": error.status_code,
                "body": error.body,
                "fields": state.get("fields"),
                "meta": state.get("meta"),
            }
        except Exception as error:
            if dedupe_key:
                _SUBMITTED_KEYS.discard(dedupe_key)
            state = {
                "submitted": False,
                "ok": False,
                "pending": False,
                "trigger": trigger,
                "at": _now_iso(),
                "error": str(error),
            }
            order["feishu_submit"] = state
            if callable(persist_callback):
                persist_callback(order)
            raise

        state = {
            "submitted": True,
            "ok": True,
            "pending": False,
            "trigger": trigger,
            "at": _now_iso(),
            "record_id": result.get("record_id") or "",
            "fields": fields,
            "meta": meta,
        }
        order["feishu_submit"] = state
        if callable(persist_callback):
            persist_callback(order)
        return {
            "ok": True,
            "skipped": False,
            "record_id": result.get("record_id") or "",
            "fields": fields,
            "meta": meta,
            "trigger": trigger,
        }
