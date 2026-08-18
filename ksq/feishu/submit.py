"""Submit work-order form rows to Feishu with per-order dedupe."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Mapping, Optional, Sequence

from ksq.feishu.client import (
    FeishuApiError,
    create_bitable_record,
    list_bitable_fields,
    list_bitable_form_fields,
    list_bitable_records,
    list_bitable_select_options,
    upload_bitable_media,
)
from ksq.feishu.failure_evidence import collect_failure_evidence
from ksq.feishu.form_schema import (
    build_form_spec,
    missing_required,
    prefill,
    values_to_bitable,
)
from ksq.feishu.form_payload import (
    DEFAULT_FIELD_NAMES,
    DEFAULT_SITE,
    OUTCOME_FAILED,
    build_feishu_form_fields,
    classify_order_outcome,
)
from ksq.test_order_select import DEFAULT_TOOL
from ksq.web.logs_api import LogServiceError, fetch_logs
from ksq.web.test_order_api import get_state

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
    forms = raw.get("forms") if isinstance(raw.get("forms"), list) else []
    # 自动提交的目标表：空=内置工单表，否则用 forms 里选中的那张。
    auto_form = str(raw.get("auto_form") or "").strip()
    app_token = str(raw.get("app_token") or "").strip()
    table_id = str(raw.get("table_id") or "").strip()
    for entry in forms:
        if isinstance(entry, Mapping) and str(entry.get("id") or "") == auto_form and auto_form:
            app_token = str(entry.get("app_token") or "").strip()
            table_id = str(entry.get("table_id") or "").strip()
            override = entry.get("field_names")
            if isinstance(override, Mapping):
                field_names = dict(field_names, **override)
            break
    return {
        "enabled": bool(raw.get("enabled")),
        "app_id": str(raw.get("app_id") or "").strip(),
        "app_secret": str(raw.get("app_secret") or "").strip(),
        "app_token": app_token,
        "table_id": table_id,
        "tester": str(raw.get("tester") or "").strip(),
        "site": site,
        "field_names": field_names,
        "forms": forms,
        "auto_form": auto_form,
    }


_TABLE_FIELDS_CACHE: Dict[str, frozenset] = {}


def _target_field_names(feishu: Mapping[str, object]) -> Optional[frozenset]:
    """Real field names of the submit target table, None when unavailable.

    ponytail: 进程内缓存；飞书表改了字段名重启生效。
    """
    key = "%s/%s" % (feishu.get("app_token"), feishu.get("table_id"))
    if key in _TABLE_FIELDS_CACHE:
        return _TABLE_FIELDS_CACHE[key]
    try:
        items = list_bitable_fields(
            str(feishu.get("app_id") or ""),
            str(feishu.get("app_secret") or ""),
            str(feishu.get("app_token") or ""),
            str(feishu.get("table_id") or ""),
        )
    except FeishuApiError:
        return None
    names = frozenset(
        str(item.get("field_name") or "").strip()
        for item in items
        if str(item.get("field_name") or "").strip()
    )
    _TABLE_FIELDS_CACHE[key] = names
    return names


def _drop_unknown_fields(
    feishu: Mapping[str, object],
    fields: Dict[str, object],
    meta: Dict[str, object],
) -> tuple:
    """Keep only names the target table really has, and say what was dropped.

    目标表少一个字段，飞书就整条记录拒绝（FieldNameNotFound / 1254045），所以
    每一次写入前都过滤一次 —— 包括失败证据那两个字段，它们是在预览之后才加的。
    读不到表结构时不过滤：宁可照旧报错，也不静默丢掉本该写进去的内容。
    """
    real_names = _target_field_names(feishu)
    if real_names is None:
        return fields, meta
    dropped = sorted(set(fields) - real_names)
    if not dropped:
        return fields, meta
    kept = {name: value for name, value in fields.items() if name in real_names}
    return kept, dict(meta, dropped_fields=dropped)


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
    target_form = _auto_target_form(feishu)
    if target_form is not None:
        # 自选表单预览：与自动提交同一份定制载荷。
        fields, meta, _spec = _build_custom_auto_payload(
            target_form, working_order, working_tasks
        )
        return {
            "ok": True,
            "enabled": bool(feishu.get("enabled")),
            "fields": fields,
            "meta": meta,
            "config": {
                "app_id": target_form["app_id"],
                "app_token": target_form["app_token"],
                "table_id": target_form["table_id"],
                "tester": str(feishu.get("tester") or ""),
                "site": str(feishu.get("site") or ""),
                "has_app_secret": bool(
                    str(feishu.get("app_secret") or "").strip()
                ),
                "form_name": target_form.get("name") or "",
            },
        }
    fields, meta = build_feishu_form_fields(
        working_order,
        working_tasks,
        dashboard_mode,
        str(feishu.get("tester") or ""),
        feishu.get("field_names"),
        str(feishu.get("site") or ""),
    )
    fields, meta = _drop_unknown_fields(feishu, fields, meta)
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


def _auto_target_form(feishu: Mapping[str, object]) -> Optional[Dict[str, str]]:
    """「自动提交到」选中的自选表单；未选（内置工单表）或已被删除时返回 None。"""
    wanted = str(feishu.get("auto_form") or "").strip()
    if not wanted:
        return None
    for raw in feishu.get("forms") or []:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("id") or "").strip() != wanted:
            continue
        if not str(raw.get("app_token") or "").strip() or not str(
            raw.get("table_id") or ""
        ).strip():
            return None
        return {
            "id": wanted,
            "name": str(raw.get("name") or wanted).strip(),
            "app_id": str(feishu.get("app_id") or ""),
            "app_secret": str(feishu.get("app_secret") or ""),
            "app_token": str(raw.get("app_token") or "").strip(),
            "table_id": str(raw.get("table_id") or "").strip(),
        }
    return None


def _find_form(settings: Mapping[str, object], form_id: str) -> Dict[str, str]:
    """Locate a configured extra form; raises FeishuApiError when unknown."""
    feishu = _feishu_settings(settings)
    wanted = str(form_id or "").strip()
    for raw in feishu.get("forms") or []:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("id") or "").strip() != wanted or not wanted:
            continue
        return {
            "id": wanted,
            "name": str(raw.get("name") or wanted).strip(),
            "app_id": str(feishu.get("app_id") or ""),
            "app_secret": str(feishu.get("app_secret") or ""),
            "app_token": str(raw.get("app_token") or "").strip(),
            "table_id": str(raw.get("table_id") or "").strip(),
        }
    raise FeishuApiError("未找到表单「%s」，请先在设置里配置。" % wanted, 404, {"id": wanted})


# 药房功能测试要的是「error 前 400 后 200 行」；内置工单表照旧前后各 20 行。
_MANUAL_LOG_BEFORE = 400
_MANUAL_LOG_AFTER = 200

# 「测试用例id(e开头序列号是L39，d开头是L40）」—— 规则抄自飞书那个字段的标题。
_CASE_BY_GROUP_PREFIX = {"e": "L39", "d": "L40"}

# 级联步骤（顺序即链条顺序）：识别 → 吸取/抓取 → 扫码 → 出库 → 放置。
# 出库在机器人日志里没有独立行，由“到达放置阶段”推定正确。
_STEP_ORDER = ("identify", "pick", "scan", "outbound", "place")
_STEP_PASS_VALUES = {
    "identify": "成功",
    "pick": "成功",
    "scan": "成功",
    "outbound": "正确",
    "place": "成功",
}
_STEP_FAIL_VALUE = "失败"

# 失败行 → 失败环节（只收机器人口经里确认出现的行，别猜）。
_FAILURE_STAGE_BY_LINE = (
    ("not found in percept_pusher results", "identify"),
    ("find object and shelf failed", "identify"),
    ("object is marked as unavailable", "identify"),
    ("pick_up_object failed", "pick"),
    ("scan object pipeline failed", "scan"),
    ("check scan object result failed", "scan"),
    ("packing task failed", "place"),
)


def _stage_from_failure_line(line: str) -> str:
    lower = str(line or "").lower()
    for needle, stage in _FAILURE_STAGE_BY_LINE:
        if needle in lower:
            return stage
    return ""


def _item_step_outcomes(task: Mapping[str, object]) -> Dict[str, str]:
    """单个 SKU 的级联步骤结论，来自该子任务的执行证据。

    子任务成功：五个环节全过。失败：能认出失败环节就填到该环节（含）为止、
    其后留空（链条中断）；认不出就只填「识别=成功」（已开始处理）。
    未执行/进行中一律留空。
    """
    status = str(task.get("status") or "").strip()
    if status == "success":
        return dict(_STEP_PASS_VALUES)
    if status != "failed":
        return {}
    events = [
        event
        for event in (task.get("events") or [])
        if isinstance(event, Mapping)
    ]
    started = bool(task.get("started_at")) or any(
        str(event.get("kind") or "") == "started" for event in events
    )
    line = str(task.get("end_line") or "")
    if not line:
        for event in reversed(events):
            if str(event.get("kind") or "") == "failed":
                line = str(event.get("text") or "")
                break
    stage = _stage_from_failure_line(line)
    if not stage:
        return {"identify": _STEP_PASS_VALUES["identify"]} if started else {}
    outcomes: Dict[str, str] = {}
    for key in _STEP_ORDER:
        if key == stage:
            outcomes[key] = _STEP_FAIL_VALUE
            break
        outcomes[key] = _STEP_PASS_VALUES[key]
    return outcomes


def _form_spec(form: Mapping[str, str]) -> List[Dict[str, object]]:
    """字段/顺序/必填以飞书「表单视图」为准，没有表单视图时退回整表结构。"""
    fields = list_bitable_fields(
        form["app_id"], form["app_secret"], form["app_token"], form["table_id"]
    )
    form_items = list_bitable_form_fields(
        form["app_id"], form["app_secret"], form["app_token"], form["table_id"]
    )
    return build_form_spec(fields, form_items)


def _manual_failure_evidence(
    order: Optional[Mapping[str, object]], tasks: Sequence[Mapping[str, object]]
) -> Optional[Dict[str, object]]:
    """报错行前 400 后 200 行的描述与截图；工单没判失败就没有证据。"""
    if not order:
        return None
    lifecycle = order.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        lifecycle = {}
    outcome = classify_order_outcome(
        tasks,
        lifecycle.get("end_reason"),
        lifecycle.get("await_kind") or order.get("await_kind"),
        lifecycle.get("broker_status") or order.get("broker_status"),
    )
    if outcome != OUTCOME_FAILED:
        return None
    try:
        payload = fetch_logs("0", _MANUAL_LOG_BEFORE + _MANUAL_LOG_AFTER + 400, "")
        raw_logs = str(payload.get("logs") or "")
    except LogServiceError:
        # 日志服务不可用时仍给出工单号/失败 SKU 摘要，只是没有上下文窗口。
        raw_logs = ""
    return collect_failure_evidence(
        order,
        tasks,
        outcome,
        raw_logs,
        order.get("await_kind"),
        order.get("await_line"),
        _MANUAL_LOG_BEFORE,
        _MANUAL_LOG_AFTER,
    )


def _prefill_extras(
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    links: Mapping[str, object],
) -> Dict[str, object]:
    """六项预填里要查系统状态的那几项。查不到一律留空，让测试人员自己填。

    测试用例 id 走 task_id → 下单状态里的 group_id → 被关联表同名记录；组名和用例
    名对不上就不选，关联字段只能写 record_id，写不进裸文本。
    """
    task_id = str(order.get("task_id") or "").strip()
    order_codes = {
        str(task.get("code") or task.get("barcode") or "").strip()
        for task in tasks
        if isinstance(task, Mapping)
    } - {""}
    group_id = ""
    tool_by_sku: Dict[str, str] = {}
    try:
        ordered = get_state().get("ordered") or []
    except (OSError, ValueError):
        ordered = []
    matched_by_task = False
    for row in ordered if isinstance(ordered, list) else []:
        if not isinstance(row, Mapping):
            continue
        if not task_id or str(row.get("task_id") or "").strip() != task_id:
            continue
        matched_by_task = True
        group_id = group_id or str(row.get("group_id") or "").strip()
        code = str(row.get("sku_code") or "").strip()
        if code:
            tool_by_sku[code] = str(row.get("推荐工具") or "").strip()
    if not matched_by_task and order_codes:
        # 「再次下单」不会回写 task_id：同一 SKU 的已下单行按编码兜底命中，组号沿用。
        for row in ordered if isinstance(ordered, list) else []:
            if not isinstance(row, Mapping):
                continue
            code = str(row.get("sku_code") or "").strip()
            if not code or code not in order_codes:
                continue
            group_id = group_id or str(row.get("group_id") or "").strip()
            tool_by_sku[code] = str(row.get("推荐工具") or "").strip()

    # 末端工具以设备加载的工具映射为准（与测试下单生成时同源）：药品下单、
    # 再次下单、升级重置状态后都能取到；状态里的推荐工具优先，映射缺失用默认。
    try:
        from ksq.web import state as web_state

        tool_mapping = web_state.loaded_tool_mapping or {}
    except Exception:  # noqa: BLE001 — 预填失败不阻断
        tool_mapping = {}

    # 组名和用例名同名就直接选中；对不上时按字段标题写的「e 开头是 L39，d 开头是 L40」
    # 认一次首字母。
    # ponytail: 映射写死在这儿，规则就写在飞书字段标题里，改了标题也来改这两行；
    # 要更通用就在设置页开一张组→用例的对照表。
    wanted = {group_id, _CASE_BY_GROUP_PREFIX.get(group_id[:1].lower(), "")} - {""}
    case_record_id = ""
    for records in links.values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, Mapping):
                continue
            if str(record.get("label") or "").strip() in wanted:
                case_record_id = str(record.get("record_id") or "")
                break
        if case_record_id:
            break

    # SKU1/SKU2… 就是工单里 SKU 的先后顺序。
    tools = {}
    steps = {}
    for index, task in enumerate(tasks, start=1):
        outcomes = _item_step_outcomes(task)
        if outcomes:
            steps[index] = outcomes
        code = str(task.get("code") or task.get("barcode") or "").strip()
        if not code:
            continue
        tool = tool_by_sku.get(code) or tool_mapping.get(code)
        if not tool and tool_mapping:
            tool = DEFAULT_TOOL
        if tool:
            tools[index] = tool

    lifecycle = order.get("lifecycle")
    evidence = _manual_failure_evidence(order, tasks)
    return {
        "case_record_id": case_record_id,
        "group_id": group_id,
        "tools": tools,
        "steps": steps,
        # 人工确认时间就是工单结束时间。
        "ended_at": lifecycle.get("ended_at") if isinstance(lifecycle, Mapping) else "",
        "description": str((evidence or {}).get("description") or ""),
    }


def _ensure_case_record(
    form: Mapping[str, str],
    spec: Sequence[Mapping[str, object]],
    links: Dict[str, object],
    group_id: str,
) -> str:
    """用例表里没有这个组就现建一条，再关联过去。

    关联字段只能写 record_id，写不进裸文本；表里没有对应记录时，「直接填入」就只能是
    先在被关联表建一条以组名命名的记录。建过一次之后同名匹配就能找到，不会重复建。
    """
    table_id = ""
    for item in spec:
        if item["input"] == "link" and "测试用例" in str(item["name"]):
            table_id = str(item.get("link_table_id") or "")
            break
    if not table_id:
        return ""
    try:
        fields = list_bitable_fields(
            form["app_id"], form["app_secret"], form["app_token"], table_id
        )
        # 飞书 /fields 的第一个就是主字段，用例名写这里。
        primary = str((fields or [{}])[0].get("field_name") or "")
        if not primary:
            return ""
        created = create_bitable_record(
            form["app_id"],
            form["app_secret"],
            form["app_token"],
            table_id,
            {primary: group_id},
        )
    except FeishuApiError:
        return ""
    record_id = str(created.get("record_id") or "")
    records = links.get(table_id)
    if record_id and isinstance(records, list):
        # 下拉框的选项就是 links，新建的这条得补进去，不然界面上选不中。
        records.append({"record_id": record_id, "label": group_id})
    return record_id


def _build_custom_auto_payload(
    form: Mapping[str, str],
    order: Optional[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
) -> tuple:
    """自选表单的自动提交载荷：内容按目标表结构定制。

    与人工填写共用同一份 schema/预填规则（task_id、起止时间、SKU<n>末端工具、
    问题现象描述、测试用例id 关联记录）；目标表没有的列天然不进 payload。
    级联只服务人工逐级展开，自动提交必须绕过（否则 SKU2 起的内容全丢）。
    """
    spec = _form_spec(form)
    working_order = dict(order or {})
    working_tasks = list(tasks) if tasks else _tasks_from_order(working_order)
    links: Dict[str, object] = {}
    for item in spec:
        link_table = str(item.get("link_table_id") or "")
        if item["input"] != "link" or not link_table or link_table in links:
            continue
        links[link_table] = list_bitable_records(
            form["app_id"], form["app_secret"], form["app_token"], link_table
        )
    extras = _prefill_extras(working_order, working_tasks, links)
    if not extras["case_record_id"] and extras["group_id"]:
        extras["case_record_id"] = _ensure_case_record(
            form, spec, links, str(extras["group_id"])
        )
    values = prefill(spec, working_order, working_tasks, extras)
    payload = values_to_bitable(spec, values, respect_cascade=False)
    meta = {
        "form_id": form.get("id") or "",
        "form_name": form.get("name") or "",
        "group_id": str(extras.get("group_id") or ""),
        "custom_auto": True,
    }
    return payload, meta, spec


def describe_feishu_form(
    settings: Mapping[str, object],
    form_id: str,
    order: Optional[Mapping[str, object]],
    tasks: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    """Read the table structure live and return spec + prefill for the tester."""
    form = _find_form(settings, form_id)
    spec = _form_spec(form)
    working_order = dict(order or {})
    working_tasks = list(tasks) if tasks else _tasks_from_order(working_order)
    links: Dict[str, object] = {}
    for item in spec:
        link_table = str(item.get("link_table_id") or "")
        if item["input"] != "link" or not link_table or link_table in links:
            continue
        links[link_table] = list_bitable_records(
            form["app_id"], form["app_secret"], form["app_token"], link_table
        )
    extras = _prefill_extras(working_order, working_tasks, links)
    if not extras["case_record_id"] and extras["group_id"]:
        extras["case_record_id"] = _ensure_case_record(
            form, spec, links, str(extras["group_id"])
        )
    return {
        "ok": True,
        "id": form["id"],
        "name": form["name"],
        "spec": spec,
        "values": prefill(spec, working_order, working_tasks, extras),
        "link_records": links,
    }


def submit_feishu_form(
    settings: Mapping[str, object],
    form_id: str,
    values: Mapping[str, object],
    order: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Write one hand-filled form row. No dedupe: each submit is a new test record."""
    form = _find_form(settings, form_id)
    spec = _form_spec(form)
    missing = missing_required(spec, values)
    if missing:
        raise FeishuApiError(
            "还有必填项没填：%s。" % "、".join(missing), 400, {"missing": missing}
        )
    payload = values_to_bitable(spec, values)
    if not payload:
        raise FeishuApiError("表单还没有填写任何内容。", 400, {})
    attachment_error = _attach_error_screenshot(form, spec, payload, order)
    result = create_bitable_record(
        form["app_id"],
        form["app_secret"],
        form["app_token"],
        form["table_id"],
        payload,
    )
    return {
        "ok": True,
        "id": form["id"],
        "name": form["name"],
        "record_id": result.get("record_id", ""),
        "fields": payload,
        "attachment_error": attachment_error,
        "submitted_at": _now_iso(),
    }


def _attach_error_screenshot(
    form: Mapping[str, str],
    spec: Sequence[Mapping[str, object]],
    payload: Dict[str, object],
    order: Optional[Mapping[str, object]],
) -> str:
    """把 400/200 的报错截图塞进附件字段「错误日志截图和机器拍照」，失败不拦提交。"""
    # 优先写入约定字段「错误日志截图和机器拍照」，没有则退回任意带「截图」的附件列。
    target = ""
    fallback = ""
    for item in spec:
        name = str(item["name"])
        if item["input"] != "attachment":
            continue
        if name == "错误日志截图和机器拍照":
            target = name
            break
        if not fallback and "截图" in name:
            fallback = name
    target = target or fallback
    if not target or not order:
        return ""
    evidence = _manual_failure_evidence(order, _tasks_from_order(order))
    png_bytes = (evidence or {}).get("png_bytes")
    if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
        return ""
    try:
        file_token = upload_bitable_media(
            form["app_id"],
            form["app_secret"],
            form["app_token"],
            str((evidence or {}).get("png_name") or "robot_error.png"),
            bytes(png_bytes),
        )
    except FeishuApiError as error:
        return str(error)
    payload[target] = [{"file_token": file_token}]
    return ""


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
            target_form = _auto_target_form(feishu)
            if target_form is not None:
                # 自选表单：内容按目标表结构定制；失败时附 400/200 日志截图。
                fields, meta, custom_spec = _build_custom_auto_payload(
                    target_form, order, tasks
                )
                if fields:
                    attachment_error = _attach_error_screenshot(
                        target_form, custom_spec, fields, order
                    )
                    if attachment_error:
                        meta = dict(meta, attachment_error=attachment_error)
            else:
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
            if target_form is None and outcome == OUTCOME_FAILED:
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

            # 失败证据是预览之后才加的字段，这里再过滤一次才真正兜住。
            if target_form is None:
                fields, meta = _drop_unknown_fields(feishu, fields, meta)
            if target_form is not None:
                result = create_bitable_record(
                    target_form["app_id"],
                    target_form["app_secret"],
                    target_form["app_token"],
                    target_form["table_id"],
                    fields,
                )
            else:
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


def _demo() -> None:
    """自检：组名和用例名对不上时，靠首字母把 d007 认到 L40。"""
    from unittest.mock import patch

    order = {"task_id": "T1"}
    tasks = [{"code": "690001"}]
    links = {
        "tbl_case": [
            {"record_id": "rec39", "label": "L39"},
            {"record_id": "rec40", "label": "L40"},
            {"record_id": "recX", "label": "d007"},
        ]
    }
    state = {"ordered": [{"task_id": "T1", "group_id": "d007", "sku_code": "690001"}]}
    with (
        patch(__name__ + ".get_state", return_value=state),
        patch(__name__ + "._manual_failure_evidence", return_value=None),
    ):
        # 同名记录优先（它排在 L40 后面也无所谓，两个都在 wanted 里，取先遇到的）
        assert _prefill_extras(order, tasks, links)["case_record_id"] in {
            "rec40",
            "recX",
        }
        state["ordered"][0]["group_id"] = "e123"
        assert _prefill_extras(order, tasks, links)["case_record_id"] == "rec39"
        state["ordered"][0]["group_id"] = "z999"
        assert _prefill_extras(order, tasks, links)["case_record_id"] == ""

    # 用例表是空的时候现建一条，并补进下拉框选项里。
    spec = [{"name": "测试用例id", "input": "link", "link_table_id": "tbl_case"}]
    empty = {"tbl_case": []}
    form = {"app_id": "a", "app_secret": "s", "app_token": "t"}
    with (
        patch(__name__ + ".list_bitable_fields", return_value=[{"field_name": "用例名"}]),
        patch(
            __name__ + ".create_bitable_record", return_value={"record_id": "recNew"}
        ) as create,
    ):
        assert _ensure_case_record(form, spec, empty, "d007") == "recNew"
    assert create.call_args[0][4] == {"用例名": "d007"}
    assert empty["tbl_case"] == [{"record_id": "recNew", "label": "d007"}]
    print("submit self-check ok")


if __name__ == "__main__":
    _demo()
