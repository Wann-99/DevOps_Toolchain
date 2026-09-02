"""Load and validate configurable Feishu form rules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Mapping, Tuple

from ksq.constants import FEISHU_RULES_FILE


_BUNDLED_RULE_FILE = Path(__file__).with_name("rules.json")
_EXTERNAL_RULE_FILE = FEISHU_RULES_FILE
_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SKU_STAGES = frozenset({"recognize", "pick", "scan", "outbound", "place"})


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("规则文件存在重复键：%s" % key)
        result[key] = value
    return result


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        with path.open(encoding="utf-8") as file:
            payload = json.load(file, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("飞书规则文件读取失败：%s：%s" % (path, error)) from error
    if not isinstance(payload, Mapping):
        raise ValueError("飞书规则文件根节点必须是对象：%s" % path)
    return payload


def _aliases(value: object, context: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("规则 %s 必须是非空字符串数组。" % context)
    result = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise ValueError("规则 %s 不能包含空字段名。" % context)
        result.append(text)
    return tuple(result)


def _option_aliases(value: object, context: str) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("规则 %s 必须是对象。" % context)
    result: Dict[str, Tuple[str, ...]] = {}
    for source, targets in value.items():
        key = str(source or "").strip()
        if not key:
            raise ValueError("规则 %s 存在空映射键。" % context)
        result[key] = _aliases(targets, "%s.%s" % (context, key))
    return result


def _normalize_rule(rule_id: object, raw: object) -> Dict[str, object]:
    identifier = str(rule_id or "").strip()
    if not _RULE_ID_RE.match(identifier):
        raise ValueError("规则 ID 无效：%s（只能使用小写字母、数字和下划线）" % identifier)
    if not isinstance(raw, Mapping):
        raise ValueError("规则 %s 必须是对象。" % identifier)
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("规则 %s 缺少 name。" % identifier)

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, Mapping) or not fields_raw:
        raise ValueError("规则 %s 缺少非空 fields。" % identifier)
    fields = {
        str(key).strip(): _aliases(value, "%s.fields.%s" % (identifier, key))
        for key, value in fields_raw.items()
    }
    if any(not key for key in fields):
        raise ValueError("规则 %s 的 fields 存在空键。" % identifier)

    stages_raw = raw.get("sku_stages")
    if not isinstance(stages_raw, Mapping):
        raise ValueError("规则 %s 缺少 sku_stages。" % identifier)
    unknown_stages = set(stages_raw) - _SKU_STAGES
    if unknown_stages:
        raise ValueError(
            "规则 %s 存在未知 SKU 阶段：%s"
            % (identifier, ",".join(sorted(unknown_stages)))
        )
    sku_stages = {
        str(key): _aliases(value, "%s.sku_stages.%s" % (identifier, key))
        for key, value in stages_raw.items()
    }
    missing_stages = _SKU_STAGES - set(sku_stages)
    if missing_stages:
        raise ValueError(
            "规则 %s 缺少 SKU 阶段：%s"
            % (identifier, ",".join(sorted(missing_stages)))
        )

    option_raw = raw.get("option_aliases", {})
    if not isinstance(option_raw, Mapping):
        raise ValueError("规则 %s 的 option_aliases 必须是对象。" % identifier)
    option_aliases = {
        str(group).strip(): _option_aliases(
            value, "%s.option_aliases.%s" % (identifier, group)
        )
        for group, value in option_raw.items()
    }
    return {
        "name": name,
        "fields": fields,
        "sku_stages": sku_stages,
        "sku_tool": _aliases(raw.get("sku_tool"), "%s.sku_tool" % identifier),
        "option_aliases": option_aliases,
    }


def _load_rules(
    external_path: Path = _EXTERNAL_RULE_FILE,
    bundled_path: Path = _BUNDLED_RULE_FILE,
) -> Tuple[str, Dict[str, Dict[str, object]]]:
    """Load external rules first, falling back to the bundled defaults."""
    external_path = Path(external_path)
    bundled_path = Path(bundled_path)
    if external_path.exists() and not external_path.is_file():
        raise ValueError("飞书规则路径不是文件：%s" % external_path)
    source = external_path if external_path.is_file() else bundled_path
    if not source.is_file():
        raise ValueError("找不到飞书规则文件：%s" % source)
    payload = _read_json(source)
    version = payload.get("version")
    if version != 1:
        raise ValueError("不支持的飞书规则文件版本：%r" % version)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, Mapping) or not raw_rules:
        raise ValueError("飞书规则文件必须包含非空 rules。")
    rules: Dict[str, Dict[str, object]] = {}
    for rule_id, raw_rule in raw_rules.items():
        identifier = str(rule_id or "").strip()
        if identifier in rules:
            raise ValueError("飞书规则 ID 重复：%s" % identifier)
        rules[identifier] = _normalize_rule(identifier, raw_rule)
    default_rule = str(payload.get("default_rule") or "").strip()
    if default_rule not in rules:
        raise ValueError("飞书默认规则不存在：%s" % default_rule)
    return default_rule, rules


DEFAULT_RULE_ID, FORM_RULES = _load_rules()


def normalize_rule_id(value: object) -> str:
    rule_id = str(value or "").strip()
    return rule_id if rule_id in FORM_RULES else DEFAULT_RULE_ID


def get_rule(value: object) -> Mapping[str, object]:
    return FORM_RULES[normalize_rule_id(value)]


def public_rules() -> list:
    return [
        {
            "id": rule_id,
            "name": str(rule.get("name") or rule_id),
            "default": rule_id == DEFAULT_RULE_ID,
        }
        for rule_id, rule in FORM_RULES.items()
    ]
