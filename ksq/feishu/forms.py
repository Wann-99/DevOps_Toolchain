"""Native Feishu form prompts; filling and submission stay on the Feishu page."""

from copy import deepcopy

from ksq.dashboard import cache, settings
from ksq.order import model, store


def selected_form(feishu: dict) -> dict | None:
    for form in feishu.get("forms") or []:
        if not isinstance(form, dict) or form.get("id") != feishu.get("selected_form"):
            continue
        try:
            url = settings._normalize_feishu_page_url(form.get("url"))
        except ValueError:
            return None
        return {"id": form["id"], "name": form["name"], "url": url}
    return None


def observe_completed_orders(tasks: list, mode: str, feishu: dict) -> None:
    form = selected_form(feishu)
    if not tasks:
        return
    with store._ACTIVE_ORDER_LOCK:
        store._ensure_active_order_loaded()
        order = store._ACTIVE_ORDER
        if not isinstance(order, dict):
            return
        ui = order.setdefault("ui", {})
        previous = ui.get("feishu_forms") or {}
        if not feishu.get("enabled") or not form:
            if "seen" in previous:
                previous.pop("seen")
                store._save_active_order_unlocked()
            return
        state = deepcopy(previous) if previous.get("mode") == mode else {"mode": mode, "pending": []}
        seen = state.get("seen")
        pending = state.setdefault("pending", [])
        current = {}
        # shortcut: watch the newest Broker page; expand if over 50 unseen orders can finish between polls.
        for task in reversed(tasks):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or "").strip()
            status = str(task.get("status") or "").strip()
            if not task_id or not status:
                continue
            current[task_id] = (
                seen[task_id] if seen and seen.get(task_id) in model._BROKER_ORDER_TERMINAL else status
            )
            if seen is None or status not in model._BROKER_ORDER_TERMINAL:
                continue
            if seen.get(task_id) in model._BROKER_ORDER_TERMINAL:
                continue
            identifier = mode + ":" + task_id
            if not any(item["id"] == identifier for item in pending):
                pending.append({
                    "id": identifier, "task_id": task_id,
                    "order_no": model._order_no_from_task_dict(task),
                    "status": status, "form": deepcopy(form),
                })
        if not current:
            return
        state["seen"] = current
        if state != previous:
            ui["feishu_forms"] = state
            store._save_active_order_unlocked()


def form_status() -> dict:
    loaded = settings.load_dashboard_settings()
    config = loaded.get("feishu") or {}
    mode = loaded.get("mode") or "test"
    with store._ACTIVE_ORDER_LOCK:
        store._ensure_active_order_loaded()
        state = ((store._ACTIVE_ORDER or {}).get("ui") or {}).get("feishu_forms") or {}
        pending = deepcopy(state.get("pending") or []) if state.get("mode") == mode else []
    return {"enabled": bool(config.get("enabled")), "form": selected_form(config), "pending": pending}


def dismiss_form(identifier: object) -> dict:
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise ValueError("表单提示编号无效。")
    with store._ACTIVE_ORDER_LOCK:
        store._ensure_active_order_loaded()
        state = ((store._ACTIVE_ORDER or {}).get("ui") or {}).get("feishu_forms") or {}
        pending = state.get("pending") or []
        remaining = [item for item in pending if item["id"] != identifier]
        if remaining != pending:
            state["pending"] = remaining
            store._save_active_order_unlocked()
    cache._invalidate_dashboard_snapshot_cache()
    return {"ok": True}
