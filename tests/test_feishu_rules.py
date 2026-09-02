from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq.feishu import rules
from ksq.feishu.form_builder import build_form_fields


def _rule(name: str, field_name: str = "测试用例ID") -> dict:
    return {
        "name": name,
        "fields": {"case_id": [field_name]},
        "sku_stages": {
            "recognize": ["识别"],
            "pick": ["抓取"],
            "scan": ["扫码"],
            "outbound": ["出库"],
            "place": ["放置"],
        },
        "sku_tool": ["末端工具"],
        "option_aliases": {},
    }


def _config(default_rule: str = "robot_test", rules_map: dict | None = None) -> dict:
    return {
        "version": 1,
        "default_rule": default_rule,
        "rules": rules_map or {default_rule: _rule("规则")},
    }


class FeishuRuleConfigTests(unittest.TestCase):
    def test_bundled_rule_is_loaded_and_exposes_existing_mapping(self) -> None:
        self.assertIn("robot_test", rules.FORM_RULES)
        self.assertEqual(rules.DEFAULT_RULE_ID, "robot_test")
        self.assertIn("测试开始时间", rules.FORM_RULES["robot_test"]["fields"]["start_time"])
        self.assertIn("扫码异常", rules.FORM_RULES["robot_test"]["option_aliases"]["error_category"])

    def test_external_file_takes_priority_over_bundled_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "feishu_rules.json"
            bundled = root / "rules.json"
            external.write_text(
                json.dumps(_config("custom", {"custom": _rule("外部规则", "外部字段")})),
                encoding="utf-8",
            )
            bundled.write_text(
                json.dumps(_config("robot_test")), encoding="utf-8"
            )
            default_id, loaded = rules._load_rules(external, bundled)
        self.assertEqual(default_id, "custom")
        self.assertEqual(loaded["custom"]["name"], "外部规则")

    def test_missing_external_file_falls_back_to_bundled_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "rules.json"
            bundled.write_text(
                json.dumps(_config("robot_test")), encoding="utf-8"
            )
            default_id, loaded = rules._load_rules(root / "missing.json", bundled)
        self.assertEqual(default_id, "robot_test")
        self.assertEqual(loaded["robot_test"]["name"], "规则")

    def test_invalid_rule_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "feishu_rules.json"
            bundled = root / "rules.json"
            external.write_text(
                json.dumps(_config("missing", {"robot_test": _rule("规则")})),
                encoding="utf-8",
            )
            bundled.write_text(json.dumps(_config()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "默认规则不存在"):
                rules._load_rules(external, bundled)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "feishu_rules.json"
            bundled = root / "rules.json"
            external.write_text(
                '{"version":1,"default_rule":"robot_test","rules":{},"rules":{}}',
                encoding="utf-8",
            )
            bundled.write_text(json.dumps(_config()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复键"):
                rules._load_rules(external, bundled)

    def test_custom_rule_mapping_is_used_by_form_builder(self) -> None:
        custom = _rule("自定义规则", "业务编号")
        with patch.object(rules, "FORM_RULES", {"custom": rules._normalize_rule("custom", custom)}), \
             patch.object(rules, "DEFAULT_RULE_ID", "custom"):
            fields, meta = build_form_fields(
                {"case_id": "L39", "skus": [], "task_status": "SUCCESS"},
                {},
                [{"field_name": "业务编号", "type": 1}],
                "custom",
            )
        self.assertEqual(fields, {"业务编号": "L39"})
        self.assertEqual(meta["rule"], "custom")

    def test_custom_option_aliases_are_used_by_form_builder(self) -> None:
        custom = _rule("自定义规则")
        custom["fields"].update(
            {
                "error_category": ["错误分类"],
                "error_subcategory": ["错误小类"],
            }
        )
        custom["option_aliases"] = {
            "error_category": {"扫码异常": ["算法问题"]},
            "error_subcategory": {"扫码超时": ["扫码问题"]},
        }
        normalized = rules._normalize_rule("custom", custom)
        table_fields = [
            {
                "field_name": "错误分类",
                "type": 3,
                "property": {"options": [{"name": "算法问题"}]},
            },
            {
                "field_name": "错误小类",
                "type": 3,
                "property": {"options": [{"name": "扫码问题"}]},
            },
        ]
        with patch.object(rules, "FORM_RULES", {"custom": normalized}), \
             patch.object(rules, "DEFAULT_RULE_ID", "custom"):
            fields, _meta = build_form_fields(
                {"case_id": "", "skus": [], "task_status": "FAILED"},
                {
                    "error_category": "扫码异常",
                    "error_subcategory": "扫码超时",
                },
                table_fields,
                "custom",
            )
        self.assertEqual(fields["错误分类"], "算法问题")
        self.assertEqual(fields["错误小类"], "扫码问题")


if __name__ == "__main__":
    unittest.main()
