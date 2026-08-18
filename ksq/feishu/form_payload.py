"""Map dashboard work-order data to Feishu form fields."""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence, Tuple


FAILED_STATUSES = frozenset({"failed", "await_error"})
SUCCESS_STATUSES = frozenset({"success"})
SKIPPED_STATUSES = frozenset({"skipped"})
PENDING_STATUSES = frozenset({"pending", "idle", ""})
ACTIVE_STATUSES = frozenset(
    {"started", "processing", "await_confirm", "await_error"}
)

DEFAULT_FIELD_NAMES: Dict[str, str] = {
    "tester": "测试人员",
    "site": "测试场地（选项）",
    "source": "测试来源",
    "sku_codes": "货号",
    "shelves": "货架",
    "sku_count": "sku数量",
    "outcome": "达成情况",
    "order_no": "工单编号",
    "task_id": "task_id",
    "problem_desc": "问题描述",
    "problem_media": "问题截图/视频",
}

# Default when settings.site is empty; must match a form option.
DEFAULT_SITE = "药师帮-广州"
SITE_VALUE = DEFAULT_SITE  # backward-compatible alias
SOURCE_TEST = "离线"
SOURCE_PROD = "在线"
OUTCOME_SUCCESS = "成功"
OUTCOME_FAILED = "失败"
OUTCOME_REJECTED = "拒单"
OUTCOME_NOT_RUN = "未执行"


def shelf_code_from_location(location_code: object) -> str:
    text = str(location_code or "").strip()
    if len(text) < 2:
        return text
    return text[:2]


def _item_barcode(item: Mapping[str, object]) -> str:
    return str(item.get("barcode") or item.get("code") or "").strip()


def _item_location(item: Mapping[str, object]) -> str:
    return str(item.get("location_code") or "").strip()


def _item_status(item: Mapping[str, object]) -> str:
    return str(item.get("status") or "").strip().lower()


def classify_order_outcome(
    tasks: Sequence[Mapping[str, object]],
    end_reason: object,
    await_kind: object,
    broker_status: object,
) -> str:
    """Map whole-order status → 达成情况 (成功/失败/拒单/未执行)."""
    kind = str(await_kind or "").strip().lower()
    reason = str(end_reason or "").strip().lower()
    broker = str(broker_status or "").strip().lower()

    # 失败优先：报错播报 / human_error / 失败 SKU。
    # 例如 percept not found +「报错，请求人工处理」必须是失败，不能被
    # broker 里的 cancel 子串误判成拒单。
    if kind == "error" or reason in {
        "human_error",
        "broker_error",
        "items_failed",
    }:
        return OUTCOME_FAILED
    if broker in {"error", "failed", "fail"}:
        return OUTCOME_FAILED
    if any(_item_status(task) in FAILED_STATUSES for task in tasks):
        return OUTCOME_FAILED

    # 拒单：仅明确的取消 / 抢占转单终态（避免 cancel 子串误伤）
    reject_reasons = {
        "broker_cancel",
        "cancel",
        "cancelled",
        "canceled",
        "拒单",
    }
    reject_brokers = {
        "cancel",
        "cancelled",
        "canceled",
        "manual_claimed_in_progress",
        "manual_claimed_completed",
        "manual_transferred",
        "manual_transferred_completed",
    }
    if reason in reject_reasons or broker in reject_brokers:
        return OUTCOME_REJECTED
    if any(token in reason for token in ("抢占", "转单", "claimed")):
        return OUTCOME_REJECTED

    # 成功：打包门禁 / 成功终态 / 全部 SKU 成功
    if kind == "pack" or reason in {
        "human_pack",
        "broker_success",
        "broker_awaiting_pack",
        "items_done",
    }:
        return OUTCOME_SUCCESS
    if broker in {"success", "awaiting_pack"}:
        return OUTCOME_SUCCESS
    statuses = [_item_status(task) for task in tasks]
    if statuses and all(status in SUCCESS_STATUSES for status in statuses):
        return OUTCOME_SUCCESS

    # 未执行：工单级尚未形成成功/失败/拒单（含从未开始）
    return OUTCOME_NOT_RUN


def classify_outcome(
    tasks: Sequence[Mapping[str, object]],
    end_reason: object,
    await_kind: object,
) -> str:
    return classify_order_outcome(tasks, end_reason, await_kind, "")


def _select_items_for_codes(
    tasks: Sequence[Mapping[str, object]], outcome: str
) -> List[Mapping[str, object]]:
    if outcome == OUTCOME_FAILED:
        failed = [task for task in tasks if _item_status(task) in FAILED_STATUSES]
        if failed:
            return failed
    return list(tasks)


def _join_values(values: Sequence[str]) -> str:
    seen = set()
    ordered: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ",".join(ordered)


def resolve_field_names(overrides: object) -> Dict[str, str]:
    names = dict(DEFAULT_FIELD_NAMES)
    if isinstance(overrides, Mapping):
        for key, default in DEFAULT_FIELD_NAMES.items():
            raw = overrides.get(key)
            text = str(raw or "").strip()
            names[key] = text if text else default
    return names


def build_feishu_form_fields(
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    dashboard_mode: str,
    tester: str,
    field_names: object,
    site: object,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Return (bitable fields, debug meta)."""
    names = resolve_field_names(field_names)
    site_value = str(site or "").strip() or DEFAULT_SITE
    mode = "prod" if str(dashboard_mode or "").strip() == "prod" else "test"
    lifecycle = order.get("lifecycle") if isinstance(order.get("lifecycle"), dict) else {}
    end_reason = ""
    await_kind = ""
    broker_status = ""
    if isinstance(lifecycle, Mapping):
        end_reason = str(lifecycle.get("end_reason") or "")
        await_kind = str(lifecycle.get("await_kind") or "")
        broker_status = str(lifecycle.get("broker_status") or "")
    if not await_kind:
        await_kind = str(order.get("await_kind") or "")
    if not broker_status:
        broker_status = str(order.get("broker_status") or "")

    working_tasks: List[Mapping[str, object]] = list(tasks)
    if not working_tasks and isinstance(order.get("items"), list):
        working_tasks = [
            item for item in order["items"] if isinstance(item, Mapping)  # type: ignore[index]
        ]

    outcome = classify_order_outcome(
        working_tasks, end_reason, await_kind, broker_status
    )
    selected = _select_items_for_codes(working_tasks, outcome)
    barcodes = [_item_barcode(item) for item in selected]
    shelves = [shelf_code_from_location(_item_location(item)) for item in selected]
    sku_count = len(working_tasks)
    if sku_count == 0:
        try:
            sku_count = int(order.get("item_count") or 0)
        except (TypeError, ValueError):
            sku_count = 0

    source_value = SOURCE_PROD if mode == "prod" else SOURCE_TEST
    order_no = str(order.get("order_no") or "").strip()
    task_id = str(order.get("task_id") or "").strip()
    # One Feishu row per work order: identity + order-level aggregates.
    fields: Dict[str, object] = {
        names["site"]: site_value,
        names["source"]: source_value,
        names["sku_codes"]: _join_values(barcodes),
        names["shelves"]: _join_values(shelves),
        names["sku_count"]: sku_count,
        names["outcome"]: outcome,
    }
    if order_no:
        fields[names["order_no"]] = order_no
    if task_id:
        fields[names["task_id"]] = task_id
    # 「测试人员」在表里是人员字段，不能填姓名字符串；仅当传入 open_id 时写入。
    tester_text = str(tester or "").strip()
    if tester_text.startswith("ou_"):
        fields[names["tester"]] = [{"id": tester_text}]

    meta = {
        "outcome": outcome,
        "source": source_value,
        "site": site_value,
        "sku_count": sku_count,
        "barcode_count": len([value for value in barcodes if value]),
        "shelf_count": len([value for value in shelves if value]),
        "end_reason": end_reason,
        "await_kind": await_kind,
        "broker_status": broker_status,
        "order_no": order_no,
        "task_id": task_id,
        "selected_barcodes": [value for value in barcodes if value],
        "selected_shelves": [value for value in shelves if value],
        "field_names": names,
        "scope": "work_order",
    }
    return fields, meta
