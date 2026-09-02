"""Build Feishu fields from deterministic parser output and optional AI output."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ksq.feishu.rules import get_rule, normalize_rule_id


_SKU_INDEX_RE = re.compile(r"sku\s*(\d+)", re.I)


def _normalized(value: object) -> str:
    return re.sub(r"[\s_（）()\/\\\-]+", "", str(value or "")).lower()


def _field_type(field: Mapping[str, object]) -> int:
    try:
        return int(field.get("type") or 0)
    except (TypeError, ValueError):
        return 0


def _options(field: Mapping[str, object]) -> set:
    prop = field.get("property")
    if not isinstance(prop, Mapping):
        return set()
    return {
        str(item.get("name") or "").strip()
        for item in prop.get("options", [])
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }


def _option_value(field: Mapping[str, object], value: object, aliases: Mapping[str, Sequence[str]] = ()) -> object:
    """Use a live single-select option, with compatibility aliases for older AI labels."""
    text = str(value or "").strip()
    options = _options(field)
    if not options or text in options:
        return text
    candidates = aliases.get(text, ()) if isinstance(aliases, Mapping) else ()
    for candidate in candidates:
        if candidate in options:
            return candidate
    return text


def _datetime_millis(value: object) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def _time_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.strftime("%H:%M:%S")


def _coerce(field: Mapping[str, object], value: object) -> object:
    type_code = _field_type(field)
    if type_code == 5:
        return _datetime_millis(value)
    if type_code == 2:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if type_code in {3, 4}:
        text = str(value or "").strip()
        options = _options(field)
        if options and text not in options:
            return None
        return [text] if type_code == 4 and text else text
    return value if type_code in {0, 1} else None


def _find_field(
    fields: Sequence[Mapping[str, object]], aliases: Sequence[object]
) -> Optional[Mapping[str, object]]:
    wanted = tuple(_normalized(alias) for alias in aliases if _normalized(alias))
    for field in fields:
        name = _normalized(field.get("field_name"))
        if any(name == alias or name.startswith(alias) for alias in wanted):
            return field
    return None


def _put(
    payload: Dict[str, object],
    field: Optional[Mapping[str, object]],
    value: object,
) -> bool:
    if field is None or value in (None, "", [], {}):
        return False
    converted = _coerce(field, value)
    if converted in (None, "", [], {}):
        return False
    name = str(field.get("field_name") or "").strip()
    if not name:
        return False
    payload[name] = converted
    return True


def _sku_stage_field(
    fields: Sequence[Mapping[str, object]], index: int, needles: Sequence[object]
) -> Optional[Mapping[str, object]]:
    for field in fields:
        name = str(field.get("field_name") or "")
        if "副本" in name:
            continue
        match = _SKU_INDEX_RE.search(name)
        if match is None or int(match.group(1)) != index:
            continue
        # The recognition question mentions "吸取药盒" in its help text;
        # do not mistake that text for the separate pick/grab field.
        if "抓取" in needles and "吸取" in needles:
            if "识别" in name and "吸取/抓取" not in name and "抓取" not in name:
                continue
        if any(str(needle) in name for needle in needles):
            return field
    return None


def _sku_tool_field(
    fields: Sequence[Mapping[str, object]], index: int, needles: Sequence[object]
) -> Optional[Mapping[str, object]]:
    for field in fields:
        name = str(field.get("field_name") or "")
        if "副本" in name:
            continue
        match = _SKU_INDEX_RE.search(name)
        if match is not None and int(match.group(1)) == index and any(
            str(needle) in name for needle in needles
        ):
            return field
    return None


def _aliases(value: object) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item or "").strip() for item in value if str(item or "").strip())
    return ()


def _rule_option_aliases(rule: Mapping[str, object], group: str) -> Mapping[str, Sequence[str]]:
    raw = rule.get("option_aliases")
    if not isinstance(raw, Mapping):
        return {}
    aliases = raw.get(group)
    return aliases if isinstance(aliases, Mapping) else {}


def build_form_fields(
    parsed: Mapping[str, object],
    analysis: Mapping[str, object],
    table_fields: Sequence[Mapping[str, object]],
    rule_id: object,
    screenshot_token: str = "",
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Map standard pipeline values only onto fields that exist in the table."""
    normalized_rule = normalize_rule_id(rule_id)
    rule = get_rule(normalized_rule)
    aliases = rule.get("fields") if isinstance(rule.get("fields"), Mapping) else {}
    stage_rules = (
        rule.get("sku_stages") if isinstance(rule.get("sku_stages"), Mapping) else {}
    )
    payload: Dict[str, object] = {}
    filled: List[str] = []

    values = {
        "case_id": parsed.get("case_id"),
        "start_time": parsed.get("start_time"),
        "end_time": parsed.get("end_time"),
        "task_id": parsed.get("task_id"),
        "problem_description": analysis.get("problem_description"),
        "error_category": analysis.get("error_category"),
        "error_subcategory": analysis.get("error_subcategory"),
    }
    for key, value in values.items():
        names = aliases.get(key) if isinstance(aliases, Mapping) else ()
        field = _find_field(table_fields, _aliases(names))
        # Text fields in Feishu should contain the log second only. Native
        # date-time fields keep the original ISO value for millisecond coercion.
        if key in {"start_time", "end_time"} and field is not None and _field_type(field) != 5:
            value = _time_text(value)
        if key == "error_category":
            value = _option_value(
                field, value, _rule_option_aliases(rule, "error_category")
            ) if field else value
        elif key == "error_subcategory":
            value = _option_value(
                field, value, _rule_option_aliases(rule, "error_subcategory")
            ) if field else value
        if _put(payload, field, value):
            filled.append(key)

    if screenshot_token:
        names = aliases.get("screenshot") if isinstance(aliases, Mapping) else ()
        field = _find_field(table_fields, _aliases(names))
        if field is not None and _field_type(field) == 17:
            name = str(field.get("field_name") or "").strip()
            payload[name] = [{"file_token": screenshot_token}]
            filled.append("screenshot")

    skus = parsed.get("skus") if isinstance(parsed.get("skus"), list) else []
    for index, sku in enumerate(skus, start=1):
        if not isinstance(sku, Mapping):
            continue
        tool = str(sku.get("tool") or "").strip()
        if tool:
            needles = rule.get("sku_tool") if isinstance(rule, Mapping) else ()
            field = _sku_tool_field(table_fields, index, _aliases(needles))
            tool_value = (
                _option_value(field, tool, _rule_option_aliases(rule, "sku_tool"))
                if field
                else tool
            )
            if _put(payload, field, tool_value):
                filled.append("sku%d.tool" % index)
        for stage in ("recognize", "pick", "scan", "outbound", "place"):
            state = sku.get(stage)
            if state is not True and state is not False:
                continue
            needles = stage_rules.get(stage) if isinstance(stage_rules, Mapping) else ()
            field = _sku_stage_field(table_fields, index, _aliases(needles))
            success_value = (
                "正确"
                if stage == "outbound" and field is not None and "正确" in _options(field)
                else "成功"
            )
            if _put(payload, field, success_value if state else "失败"):
                filled.append("sku%d.%s" % (index, stage))

    return payload, {
        "rule": normalized_rule,
        "task_status": str(parsed.get("task_status") or "UNKNOWN"),
        "sku_count": len(skus),
        "filled": filled,
        "table_field_count": len(table_fields),
        "used_ai": bool(analysis.get("used_ai")),
        "need_manual_check": bool(analysis.get("need_manual_check")),
        "ai_error": str(analysis.get("ai_error") or ""),
    }


if __name__ == "__main__":  # pragma: no cover
    fields = [
        {"field_name": "task_id", "type": 1},
        {
            "field_name": "SKU1扫码是否成功",
            "type": 3,
            "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
        },
    ]
    payload, _meta = build_form_fields(
        {"task_id": "T1", "skus": [{"scan": True}], "task_status": "SUCCESS"},
        {},
        fields,
        "robot_test",
    )
    assert payload == {"task_id": "T1", "SKU1扫码是否成功": "成功"}
    print("form_builder self-check ok")
