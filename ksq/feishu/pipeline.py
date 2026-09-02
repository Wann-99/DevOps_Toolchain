"""Robot Log -> Parser -> Events -> AI (abnormal only) -> Form Builder."""

from __future__ import annotations

from typing import Dict, Mapping, Sequence

from ksq.feishu.ai.analyzer import analyze_events, should_analyze
from ksq.feishu.client import (
    FeishuApiError,
    list_bitable_fields,
    upload_bitable_media,
)
from ksq.feishu.event_builder import build_events
from ksq.feishu.form_builder import build_form_fields
from ksq.feishu.log_parser import parse_robot_log, scope_robot_log
from ksq.feishu.screenshot import render_log_screenshot


def _status_hint(
    order: Mapping[str, object], tasks: Sequence[Mapping[str, object]]
) -> str:
    statuses = {str(task.get("status") or "").strip().lower() for task in tasks}
    if statuses & {"failed", "await_error"}:
        return "FAILED"
    lifecycle = order.get("lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    reason = str(lifecycle.get("end_reason") or "").strip().lower()
    if reason in {"human_error", "broker_error", "items_failed"}:
        return "FAILED"
    if tasks and statuses <= {"success", "skipped"} and "success" in statuses:
        return "SUCCESS"
    if reason in {"human_pack", "broker_success", "items_done"}:
        return "SUCCESS"
    return ""


def _case_id(
    order: Mapping[str, object], tasks: Sequence[Mapping[str, object]]
) -> str:
    for source in (order, *tasks):
        value = str(
            source.get("case_id")
            or source.get("test_case_id")
            or source.get("group_id")
            or ""
        ).strip()
        if value:
            return value
    return ""


def build_submission(
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    raw_logs: object,
    form: Mapping[str, str],
    ai_config: Mapping[str, object],
    upload_screenshot: bool,
) -> Dict[str, object]:
    """Run the document-defined pipeline and return fields plus audit metadata."""
    scoped_logs = scope_robot_log(raw_logs, order.get("task_id"), tasks)
    parsed = parse_robot_log(
        scoped_logs,
        order.get("task_id"),
        tasks,
        _status_hint(order, tasks),
        _case_id(order, tasks),
    )
    events = build_events(scoped_logs)
    skus = parsed.get("skus") if isinstance(parsed.get("skus"), list) else []
    analysis = analyze_events(
        events,
        parsed.get("task_status"),
        [sku for sku in skus if isinstance(sku, Mapping)],
        ai_config,
    )
    table_fields = list_bitable_fields(
        form["app_id"],
        form["app_secret"],
        form["app_token"],
        form["table_id"],
    )

    screenshot_token = ""
    screenshot_error = ""
    # A successful order may contain recovered warnings/timeouts. Keep those
    # facts for optional AI analysis, but do not attach an "error log" image
    # to a successful Feishu record.
    abnormal = str(parsed.get("task_status") or "UNKNOWN").upper() != "SUCCESS"
    if abnormal and upload_screenshot:
        message = events[0].get("message") if events else ""
        screenshot = render_log_screenshot(scoped_logs, message)
        if screenshot:
            try:
                screenshot_token = upload_bitable_media(
                    form["app_id"],
                    form["app_secret"],
                    form["app_token"],
                    "robot_error_%s.png" % (str(parsed.get("task_id") or "task")),
                    screenshot,
                )
            except FeishuApiError as error:
                screenshot_error = str(error)

    fields, builder_meta = build_form_fields(
        parsed,
        analysis,
        table_fields,
        form.get("rule"),
        screenshot_token,
    )
    if not fields:
        raise FeishuApiError(
            "当前表单没有可匹配的自动填写字段，请检查表单规则和飞书列名。",
            400,
            {"form": form.get("name"), "rule": form.get("rule")},
        )
    return {
        "fields": fields,
        "meta": {
            "form_id": form.get("id") or "",
            "form_name": form.get("name") or "",
            "rule": form.get("rule") or "",
            "task_status": parsed.get("task_status") or "UNKNOWN",
            "event_count": len(events),
            "ai_called": bool(analysis.get("used_ai")),
            "ai_skipped": bool(analysis.get("skipped")),
            "ai_error": str(analysis.get("ai_error") or ""),
            "need_manual_check": bool(analysis.get("need_manual_check")),
            "screenshot_uploaded": bool(screenshot_token),
            "screenshot_error": screenshot_error,
            "builder": builder_meta,
        },
        "parsed": parsed,
        "events": events,
    }
