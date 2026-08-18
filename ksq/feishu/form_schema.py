"""Schema-driven Feishu forms: read the table structure, let the tester fill it in.

The work-order form is generated from system state (see form_payload.py). Other
forms are not derivable: the dashboard only tracks coarse per-item status and the
logs carry no 扫码 / 出库 signal at all. So those forms are filled by hand, and the
only thing this module hardcodes is the *shape* rules — field names and options
come back live from the bitable `/fields` API.

Cascade rule (from the 药房功能测试 form): fields named SKU<n>... form chain n.
A chain step reveals the next one only when its answer reads as a pass; a failure
ends the chain and every later step, including later SKU groups, stays hidden.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


# Feishu bitable field types we can render. Everything else is read-only or
# unsupported and gets dropped from the spec.
_INPUT_BY_TYPE: Dict[int, str] = {
    1: "text",
    2: "number",
    3: "select",
    4: "multiselect",
    5: "date",
    7: "checkbox",
    11: "person",
    13: "text",
    15: "text",
    17: "attachment",
    18: "link",
    21: "link",
}

# A chain step passes unless its answer reads as a failure.
# ponytail: 词表判定，够用且不必随表结构改；出现反例（例如一个正常的「否」选项）
# 再在这里加词或给表单加 fail_words 覆盖。
_FAIL_WORDS = ("失败", "错误", "异常", "不", "否")

_SKU_RE = re.compile(r"SKU\s*(\d+)", re.I)

_TOOL_FIELD_RE = re.compile(r"SKU\s*(\d+)\s*末端工具", re.I)

# 下单状态里的工具是英文名，表单选项是中文；对不上就不预填，让测试人员自己选。
_TOOL_LABEL = {
    "double_vacuum_gripper": "双吸盘",
    "four_vacuum_gripper": "四吸盘",
    "gripper": "夹爪",
}

# 真实表里有「… 副本」/「… 副本 2」这类复制出来的列。它们照旧显示（表里有就能填），
# 但不参与级联判定 —— 否则永远空着的副本会把链卡住，后面的 SKU 组再也出不来。
_COPY_RE = re.compile(r"副本\s*\d*$")

# 级联步骤列的关键词（顺序敏感：「是否识别成功（是否过去吸取药盒）」里也有“吸取”，
# 必须先判「是否识别」）。「副本」列不自动填。
_STEP_FIELD_KEYWORDS = (
    ("是否识别", "identify"),
    ("吸取/抓取", "pick"),
    ("扫码", "scan"),
    ("出库", "outbound"),
    ("放置", "place"),
)


def _step_for_field(name: str) -> Optional[Tuple[int, str]]:
    """「SKU<n>是否识别成功」这类级联列 → (SKU 序号, 环节)；其余返回 None。"""
    match = _SKU_RE.search(name)
    if not match or _COPY_RE.search(name):
        return None
    for needle, stage in _STEP_FIELD_KEYWORDS:
        if needle in name:
            return int(match.group(1)), stage
    return None


def _field_input(field: Mapping[str, object]) -> str:
    try:
        type_code = int(field.get("type") or 0)
    except (TypeError, ValueError):
        return ""
    return _INPUT_BY_TYPE.get(type_code, "")


def _field_options(field: Mapping[str, object]) -> List[str]:
    property_data = field.get("property")
    raw = property_data.get("options") if isinstance(property_data, dict) else None
    names: List[str] = []
    for option in raw if isinstance(raw, list) else []:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _field_group(name: str) -> int:
    match = _SKU_RE.search(name)
    return int(match.group(1)) if match else 0


def build_form_spec(
    fields: Sequence[Mapping[str, object]],
    form_items: Sequence[Mapping[str, object]] = (),
) -> List[Dict[str, object]]:
    """Turn bitable field descriptors into a renderable spec.

    给了 form_items（飞书「表单视图」）就以它为准：字段范围、顺序、必填、帮助文案
    全部来自表单本身，不再靠猜 —— 整表里那些副本列、研发/AI 列、SKU3/SKU4 在表单里
    visible=False，自然就被挡掉了。没有表单视图时退回整表并按 SKU 组排序。

    name 一律是 field_name（写飞书要用它），label 才是表单上显示的标题 —— 两者真的
    会不一样（「测试用例id」vs「测试用例id(e开头序列号是L39，d开头是L40）」），
    用错就是 FieldNameNotFound。
    """
    by_id = {str(field.get("field_id") or ""): field for field in fields}
    rows: List[tuple] = []
    for item in form_items:
        if not item.get("visible"):
            continue
        field = by_id.get(str(item.get("field_id") or ""))
        if field is None:
            continue
        rows.append(
            (
                field,
                str(item.get("title") or "").strip(),
                bool(item.get("required")),
                str(item.get("description") or "").strip(),
            )
        )
    if not rows:
        rows = [(field, "", False, "") for field in fields]

    spec: List[Dict[str, object]] = []
    for field, label, required, description in rows:
        name = str(field.get("field_name") or "").strip()
        input_kind = _field_input(field)
        if not name or not input_kind:
            continue
        property_data = field.get("property")
        link_table = ""
        if input_kind == "link" and isinstance(property_data, dict):
            link_table = str(property_data.get("table_id") or "").strip()
        spec.append(
            {
                "name": name,
                "label": label or name,
                "input": input_kind,
                "options": _field_options(field),
                "group": _field_group(name),
                "link_table_id": link_table,
                "required": required,
                "description": description,
            }
        )
    if form_items:
        return spec  # 表单视图自己的顺序就是权威顺序，不要再排
    # Base fields first, then chain groups in order; stable within each group.
    return sorted(spec, key=lambda item: item["group"])


def step_passed(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and not any(word in text for word in _FAIL_WORDS)


def visible_fields(
    spec: Sequence[Mapping[str, object]], values: Mapping[str, object]
) -> List[str]:
    """Names the tester should currently see, honouring the cascade."""
    names = [str(item["name"]) for item in spec if item["group"] == 0]
    groups = sorted({int(item["group"]) for item in spec if int(item["group"]) > 0})
    for group in groups:
        for item in spec:
            if int(item["group"]) != group:
                continue
            name = str(item["name"])
            names.append(name)
            if item["input"] != "select" or _COPY_RE.search(name):
                continue
            answer = str(values.get(name) or "").strip()
            if not answer or not step_passed(answer):
                return names
    return names


def missing_required(
    spec: Sequence[Mapping[str, object]], values: Mapping[str, object]
) -> List[str]:
    """Required *and* currently visible fields left blank, by their form label.

    只认表单视图给的 required，并且只拦当前可见的 —— 表里「测试人员」这类必填但
    表单上不显示的字段，测试人员根本填不了。附件字段也跳过：截图由后端上传。
    """
    visible = set(visible_fields(spec, values))
    missing: List[str] = []
    for item in spec:
        name = str(item["name"])
        if not item.get("required") or name not in visible:
            continue
        if item["input"] in {"checkbox", "attachment"}:
            continue
        if not str(values.get(name) or "").strip():
            missing.append(str(item.get("label") or name))
    return missing


def _local_seconds(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def prefill(
    spec: Sequence[Mapping[str, object]],
    order: Mapping[str, object],
    tasks: Sequence[Mapping[str, object]],
    extras: Optional[Mapping[str, object]] = None,
) -> Dict[str, str]:
    """Fill only what the system actually knows; matched by field-name substring.

    extras 由调用方（submit.py）算好：测试用例记录 id、每个 SKU 的末端工具（下单状态里
    的英文名）、人工确认时间、400/200 的问题描述 —— 这些要读下单状态和日志，form_schema
    不 import ksq.web，免得层级倒挂。查不到就留空，让测试人员自己填，不报错。

    ponytail: 末端工具照用户要求预填，但它仍是级联闸门 —— 预填只是省一次手输，
    测试人员随时可以改成「工具切换失败」结束这条链。
    """
    context = dict(extras or {})
    starts = [_local_seconds(task.get("started_at")) for task in tasks]
    ends = [_local_seconds(task.get("ended_at")) for task in tasks]
    started = min([text for text in starts if text], default="")
    ended = _local_seconds(context.get("ended_at")) or max(
        [text for text in ends if text], default=""
    )
    description = str(context.get("description") or "")
    tools = context.get("tools") if isinstance(context.get("tools"), Mapping) else {}
    steps = context.get("steps") if isinstance(context.get("steps"), Mapping) else {}
    case_record_id = str(context.get("case_record_id") or "")

    values: Dict[str, str] = {}
    for item in spec:
        name = str(item["name"])
        kind = item["input"]
        if kind == "link":
            if "测试用例" in name and case_record_id:
                values[name] = case_record_id
            continue
        if kind == "select":
            match = _TOOL_FIELD_RE.search(name)
            if match:
                tool = str(tools.get(int(match.group(1))) or "")
                label = _TOOL_LABEL.get(tool, "")
                if label and label in (item.get("options") or []):
                    values[name] = label
                continue
            # 级联步骤列（识别/吸取/扫码/出库/放置）：按子任务执行结论填写，
            # 失败环节之后留空；选项里没有该值就不填。
            step = _step_for_field(name)
            if step is not None:
                step_index, stage = step
                step_value = ""
                outcomes = steps.get(step_index)
                if isinstance(outcomes, Mapping):
                    step_value = str(outcomes.get(stage) or "")
                if step_value and step_value in (item.get("options") or []):
                    values[name] = step_value
            continue
        if kind not in {"text", "number", "date"}:
            continue
        if "task_id" in name.lower():
            values[name] = str(order.get("task_id") or "")
        elif "开始时间" in name:
            values[name] = started
        elif "结束时间" in name:
            values[name] = ended
        elif "问题现象" in name or "问题描述" in name:
            values[name] = description
    return {name: text for name, text in values.items() if text}


def values_to_bitable(
    spec: Sequence[Mapping[str, object]],
    values: Mapping[str, object],
    respect_cascade: bool = True,
) -> Dict[str, object]:
    """Convert submitted answers to a bitable `fields` payload, visible fields only.

    respect_cascade=False 用于自动提交：级联是给人工填写用的逐级展开，自动提交时
    没有"上一步答案"，按级联会把 SKU2 之后的内容全部丢掉。

    ponytail: 附件字段跳过；人工表单要传图再接 upload_bitable_media。
    """
    visible = (
        set(visible_fields(spec, values))
        if respect_cascade
        else {str(item["name"]) for item in spec}
    )
    fields: Dict[str, object] = {}
    for item in spec:
        name = str(item["name"])
        if name not in visible:
            continue
        raw = values.get(name)
        text = str(raw or "").strip()
        kind = item["input"]
        if kind == "checkbox":
            if raw is not None:
                fields[name] = bool(raw) and text.lower() not in {"false", "0", ""}
            continue
        if not text:
            continue
        if kind == "number":
            try:
                fields[name] = float(text)
            except ValueError:
                continue
        elif kind == "multiselect":
            fields[name] = [part for part in text.split(",") if part.strip()]
        elif kind == "person":
            if text.startswith("ou_"):
                fields[name] = [{"id": text}]
        elif kind == "link":
            fields[name] = [text]
        elif kind == "attachment":
            continue
        else:
            fields[name] = text
    return fields


if __name__ == "__main__":  # pragma: no cover - runnable self-check
    raw_fields = [
        {"field_name": "测试用例id", "type": 18, "property": {"table_id": "tblCase"}},
        {"field_name": "测试开始时间（精确到日志的秒）", "type": 1},
        {"field_name": "task_id", "type": 1},
        {"field_name": "自动编号", "type": 1005},
        {
            "field_name": "SKU1末端工具",
            "type": 3,
            "property": {
                "options": [
                    {"name": "夹爪"},
                    {"name": "双吸盘"},
                    {"name": "四吸盘"},
                    {"name": "工具切换失败"},
                ]
            },
        },
        {
            "field_name": "SKU1是否识别成功",
            "type": 3,
            "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
        },
        {
            "field_name": "SKU1扫码是否成功",
            "type": 3,
            "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
        },
        # 真实表里复制出来的列，永远空着，不能卡住级联。
        {
            "field_name": "SKU1是否识别成功 副本 2",
            "type": 3,
            "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
        },
        {
            "field_name": "SKU2末端工具",
            "type": 3,
            "property": {"options": [{"name": "夹爪"}, {"name": "工具切换失败"}]},
        },
    ]
    spec = build_form_spec(raw_fields)
    names = [item["name"] for item in spec]
    assert "自动编号" not in names, "read-only field must be dropped"
    assert names[:3] == [
        "测试用例id",
        "测试开始时间（精确到日志的秒）",
        "task_id",
    ], names
    assert [item["group"] for item in spec] == [0, 0, 0, 1, 1, 1, 1, 2], spec
    assert spec[0]["link_table_id"] == "tblCase"

    # Nothing chosen yet: only the first chain step shows.
    assert visible_fields(spec, {})[-1] == "SKU1末端工具"
    # Tool switch failed: chain ends immediately.
    assert visible_fields(spec, {"SKU1末端工具": "工具切换失败"})[-1] == "SKU1末端工具"
    # Pass reveals exactly the next step, not the whole chain.
    assert visible_fields(spec, {"SKU1末端工具": "双吸盘"})[-1] == "SKU1是否识别成功"
    # Mid-chain failure ends it; SKU2 never appears.
    partial = {"SKU1末端工具": "双吸盘", "SKU1是否识别成功": "失败"}
    assert "SKU1扫码是否成功" not in visible_fields(spec, partial)
    # SKU1 fully passed → SKU2 opens even though the 副本 column stays empty.
    full = {
        "SKU1末端工具": "双吸盘",
        "SKU1是否识别成功": "成功",
        "SKU1扫码是否成功": "成功",
    }
    assert visible_fields(spec, full)[-1] == "SKU2末端工具", visible_fields(spec, full)
    assert "SKU1是否识别成功 副本 2" in visible_fields(spec, full)

    # Hidden answers are never submitted, link fields become record id lists.
    payload = values_to_bitable(
        spec,
        dict(partial, **{"测试用例id": "recABC", "SKU1扫码是否成功": "成功"}),
    )
    assert payload["测试用例id"] == ["recABC"], payload
    assert "SKU1扫码是否成功" not in payload, payload

    assert _local_seconds("2026-08-17T03:04:05Z")

    # 表单视图接管：范围/顺序/必填/标题都听它的，整表的 SKU 组排序不再生效。
    for index, field in enumerate(raw_fields):
        field["field_id"] = "fld%d" % index
    form_items = [
        {"field_id": "fld0", "title": "测试用例id(e开头是L39)", "required": True, "visible": True},
        {"field_id": "fld4", "title": "SKU1末端工具", "required": True, "visible": True},
        {"field_id": "fld2", "title": "task_id", "required": False, "visible": True},
        {"field_id": "fld5", "title": "SKU1是否识别成功", "required": True, "visible": True},
        # 表单上不显示的必填字段（真实表里的「测试人员」），不能拿去拦提交。
        {"field_id": "fld1", "title": "测试开始时间", "required": True, "visible": False},
        {"field_id": "fld7", "title": "副本列", "required": False, "visible": False},
    ]
    view_spec = build_form_spec(raw_fields, form_items)
    assert [item["name"] for item in view_spec] == [
        "测试用例id",
        "SKU1末端工具",
        "task_id",
        "SKU1是否识别成功",
    ], view_spec
    # 写飞书用 field_name，界面上显示 title —— 两者真的不一样。
    assert view_spec[0]["label"] == "测试用例id(e开头是L39)"
    assert missing_required(view_spec, {}) == ["测试用例id(e开头是L39)", "SKU1末端工具"]
    filled = {"测试用例id": "recABC", "SKU1末端工具": "双吸盘"}
    assert missing_required(view_spec, filled) == ["SKU1是否识别成功"]
    # 链在 SKU1末端工具 断掉时，后面的必填不该被算进来。
    assert missing_required(view_spec, {**filled, "SKU1末端工具": "工具切换失败"}) == []

    # 末端工具：英文名映射到中文，且必须是该字段自己的选项才预填。
    order = {"task_id": "task-1"}
    tasks = [{"started_at": "2026-08-17T03:04:05Z"}]
    values = prefill(
        view_spec,
        order,
        tasks,
        {"case_record_id": "recCase", "tools": {1: "double_vacuum_gripper", 2: "gripper"}},
    )
    assert values["SKU1末端工具"] == "双吸盘", values
    assert values["测试用例id"] == "recCase" and values["task_id"] == "task-1"
    # 选项里没有的工具（SKU2 只有夹爪/工具切换失败）与查不到的组，一律留空。
    assert prefill(view_spec, order, tasks, {"tools": {1: "unknown_tool"}}) == {
        "task_id": "task-1"
    }
    print("form_schema self-check ok")
