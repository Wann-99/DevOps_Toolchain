"""自动提交目标为自选表单时，载荷按目标表结构定制的回归测试。

背景：「自动提交到」选中「药房功能测试」这类自选表单后，原先仍写入工单字段集，
目标表只碰巧有 task_id 同名列，产生只剩 task_id 的空壳记录。修复后应按目标表
schema 定制内容（task_id、起止时间、SKU 工具、用例关联等），且绕过人工级联。
"""

from __future__ import annotations

import unittest
from typing import Dict
from unittest.mock import patch

from ksq.feishu import submit
from ksq.feishu.client import FeishuApiError
from ksq.feishu.form_schema import build_form_spec, values_to_bitable

APP = {"app_id": "a", "app_secret": "s"}
CUSTOM_FORM = {
    "id": "药房功能测试",
    "name": "药房功能测试",
    "app_token": "tok_custom",
    "table_id": "tbl_custom",
}

SETTINGS = {
    "feishu": {
        "enabled": True,
        "app_id": "a",
        "app_secret": "s",
        "app_token": "tok_builtin",
        "table_id": "tbl_builtin",
        "auto_form": "药房功能测试",
        "forms": [CUSTOM_FORM],
    }
}

SPEC_FIELDS = [
    {"field_name": "测试用例id", "type": 18, "property": {"table_id": "tblCase"}},
    {"field_name": "测试开始时间（精确到日志的秒）", "type": 1},
    {"field_name": "整体测试结束时间（精确到秒）", "type": 1},
    {"field_name": "task_id", "type": 1},
    {
        "field_name": "SKU1末端工具",
        "type": 3,
        "property": {"options": [{"name": "双吸盘"}, {"name": "工具切换失败"}]},
    },
    {
        "field_name": "SKU2末端工具",
        "type": 3,
        "property": {"options": [{"name": "夹爪"}, {"name": "工具切换失败"}]},
    },
    {"field_name": "问题现象描述", "type": 1},
]


def _settings(auto_form: str = "药房功能测试") -> Dict[str, object]:
    import copy

    data = copy.deepcopy(SETTINGS)
    data["feishu"]["auto_form"] = auto_form  # type: ignore[index]
    return data


class AutoTargetFormTests(unittest.TestCase):
    def test_resolves_selected_custom_form(self) -> None:
        feishu = submit._feishu_settings(_settings())
        target = submit._auto_target_form(feishu)
        self.assertIsNotNone(target)
        self.assertEqual(target["app_token"], "tok_custom")
        self.assertEqual(target["table_id"], "tbl_custom")

    def test_builtin_target_returns_none(self) -> None:
        feishu = submit._feishu_settings(_settings(""))
        self.assertIsNone(submit._auto_target_form(feishu))

    def test_deleted_form_falls_back_to_builtin(self) -> None:
        settings = _settings()
        settings["feishu"]["forms"] = []  # type: ignore[index]
        feishu = submit._feishu_settings(settings)
        self.assertIsNone(submit._auto_target_form(feishu))


class CustomAutoSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        submit._SUBMITTED_KEYS.clear()

    def tearDown(self) -> None:
        submit._SUBMITTED_KEYS.clear()

    def test_custom_target_writes_tailored_payload(self) -> None:
        spec = build_form_spec(SPEC_FIELDS)
        order = {"task_id": "T1", "order_no": "ORDER-T1"}
        tasks = [
            {
                "code": "690001",
                "started_at": "2026-08-17T10:00:00Z",
                "ended_at": "2026-08-17T10:01:00Z",
            }
        ]
        created: Dict[str, object] = {}

        def fake_create(app_id, app_secret, app_token, table_id, fields):
            created.update(
                {
                    "app_token": app_token,
                    "table_id": table_id,
                    "fields": fields,
                }
            )
            return {"record_id": "recNew"}

        with (
            patch.object(submit, "_form_spec", return_value=spec),
            patch.object(submit, "list_bitable_records", return_value=[]),
            patch.object(
                submit,
                "_prefill_extras",
                return_value={
                    "case_record_id": "recX",
                    "group_id": "d007",
                    "tools": {1: "double_vacuum_gripper", 2: "gripper"},
                    "ended_at": "2026-08-17T10:02:00Z",
                    "description": "",
                },
            ),
            patch.object(submit, "_attach_error_screenshot", return_value=""),
            patch.object(submit, "create_bitable_record", side_effect=fake_create),
        ):
            result = submit.maybe_submit_feishu_form(
                order,
                tasks,
                "test",
                _settings(),
                "manual",
                None,
                None,
            )

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(created.get("app_token"), "tok_custom")
        self.assertEqual(created.get("table_id"), "tbl_custom")
        fields = created.get("fields") or {}
        self.assertEqual(fields.get("task_id"), "T1")
        self.assertEqual(fields.get("SKU1末端工具"), "双吸盘")
        # 级联不得拦截 SKU2：自动提交没有"上一步答案"的概念。
        self.assertEqual(fields.get("SKU2末端工具"), "夹爪")
        self.assertEqual(fields.get("测试用例id"), ["recX"])
        self.assertTrue(fields.get("测试开始时间（精确到日志的秒）"))
        self.assertTrue(fields.get("整体测试结束时间（精确到秒）"))

    def test_builtin_target_uses_work_order_payload(self) -> None:
        order = {"task_id": "T2", "order_no": "ORDER-T2"}
        created: Dict[str, object] = {}

        def fake_create(app_id, app_secret, app_token, table_id, fields):
            created.update({"app_token": app_token, "table_id": table_id})
            return {"record_id": "recBuiltin"}

        with (
            patch.object(
                submit,
                "preview_feishu_form",
                return_value={"fields": {"task_id": "T2"}, "meta": {}},
            ),
            patch.object(
                submit,
                "list_bitable_fields",
                side_effect=FeishuApiError("no table", 500, {}),
            ),
            patch.object(submit, "create_bitable_record", side_effect=fake_create),
        ):
            result = submit.maybe_submit_feishu_form(
                order,
                [],
                "test",
                _settings(""),
                "manual",
                None,
                None,
            )

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(created.get("app_token"), "tok_builtin")
        self.assertEqual(created.get("table_id"), "tbl_builtin")


class PrefillExtrasToolTests(unittest.TestCase):
    """末端工具：工具映射驱动，不再依赖测试下单状态。"""

    def _extras(self, ordered, mapping, tasks):
        with (
            patch.object(submit, "get_state", return_value={"ordered": ordered}),
            patch.object(submit, "_manual_failure_evidence", return_value=None),
            patch("ksq.web.state.loaded_tool_mapping", mapping),
        ):
            return submit._prefill_extras({"task_id": "T9"}, tasks, {})

    def test_tools_come_from_dataset_mapping(self) -> None:
        extras = self._extras(
            [],  # 测试下单状态为空（升级重置后）
            {"690001": "four_vacuum_gripper"},
            [{"code": "690001"}],
        )
        self.assertEqual(extras["tools"], {1: "four_vacuum_gripper"})
        self.assertEqual(extras["group_id"], "")

    def test_reorder_falls_back_to_sku_match(self) -> None:
        # 再次下单不回写 task_id：同一 SKU 的行按编码兜底，组号沿用。
        ordered = [
            {
                "task_id": "OLD-TASK",
                "sku_code": "690001",
                "group_id": "d007",
                "推荐工具": "gripper",
            }
        ]
        extras = self._extras(ordered, {}, [{"code": "690001"}])
        self.assertEqual(extras["group_id"], "d007")
        self.assertEqual(extras["tools"], {1: "gripper"})

    def test_state_rows_win_over_mapping(self) -> None:
        ordered = [
            {
                "task_id": "T9",
                "sku_code": "690001",
                "group_id": "e123",
                "推荐工具": "gripper",
            }
        ]
        extras = self._extras(
            ordered, {"690001": "four_vacuum_gripper"}, [{"code": "690001"}]
        )
        self.assertEqual(extras["group_id"], "e123")
        self.assertEqual(extras["tools"], {1: "gripper"})

    def test_default_tool_only_when_mapping_loaded(self) -> None:
        # 映射已加载但缺这个 SKU → 默认双吸盘；映射未加载 → 留空不猜。
        extras = self._extras([], {"other": "gripper"}, [{"code": "690001"}])
        self.assertEqual(extras["tools"], {1: "double_vacuum_gripper"})
        extras = self._extras([], {}, [{"code": "690001"}])
        self.assertEqual(extras["tools"], {})


class AttachErrorScreenshotTests(unittest.TestCase):
    """报错截图优先写入约定附件列「错误日志截图和机器拍照」。"""

    def _spec_with_attachments(self):
        return build_form_spec(
            [
                {"field_name": "问题截图", "type": 17},
                {"field_name": "错误日志截图和机器拍照", "type": 17},
            ]
        )

    def test_preferred_attachment_field_wins(self) -> None:
        payload: Dict[str, object] = {"task_id": "T1"}
        order = {"task_id": "T1"}
        with (
            patch.object(
                submit,
                "_manual_failure_evidence",
                return_value={"png_bytes": b"png", "png_name": "err.png"},
            ),
            patch.object(submit, "upload_bitable_media", return_value="ftok"),
        ):
            error = submit._attach_error_screenshot(
                {"app_id": "a", "app_secret": "s", "app_token": "t"},
                self._spec_with_attachments(),
                payload,
                order,
            )
        self.assertEqual(error, "")
        self.assertEqual(
            payload.get("错误日志截图和机器拍照"), [{"file_token": "ftok"}]
        )
        self.assertNotIn("问题截图", payload)

    def test_fallback_to_any_screenshot_attachment(self) -> None:
        payload: Dict[str, object] = {}
        spec = build_form_spec([{"field_name": "问题截图", "type": 17}])
        with (
            patch.object(
                submit,
                "_manual_failure_evidence",
                return_value={"png_bytes": b"png", "png_name": "err.png"},
            ),
            patch.object(submit, "upload_bitable_media", return_value="ftok"),
        ):
            submit._attach_error_screenshot(
                {"app_id": "a", "app_secret": "s", "app_token": "t"},
                spec,
                payload,
                {"task_id": "T1"},
            )
        self.assertEqual(payload.get("问题截图"), [{"file_token": "ftok"}])


class ItemStepOutcomeTests(unittest.TestCase):
    """级联步骤：识别→吸取→扫码→出库→放置，任一失败链条即止。"""

    def test_success_item_all_steps_pass(self) -> None:
        outcomes = submit._item_step_outcomes({"status": "success"})
        self.assertEqual(
            outcomes,
            {"identify": "成功", "pick": "成功", "scan": "成功", "outbound": "正确", "place": "成功"},
        )

    def test_failed_at_pick_breaks_chain(self) -> None:
        task = {
            "status": "failed",
            "started_at": "2026-08-18T10:00:00Z",
            "end_line": "pick_up_object failed",
        }
        outcomes = submit._item_step_outcomes(task)
        self.assertEqual(outcomes, {"identify": "成功", "pick": "失败"})

    def test_failed_at_identify_when_never_started(self) -> None:
        task = {"status": "failed", "end_line": "find object and shelf failed"}
        self.assertEqual(submit._item_step_outcomes(task), {"identify": "失败"})

    def test_scan_failure_line_maps_to_scan(self) -> None:
        # 真实扫码失败行：check scan object result failed: trace code not found
        task = {
            "status": "failed",
            "started_at": "2026-08-18T10:00:00Z",
            "end_line": "[ERROR] [FVR.ScanObjectPipeline] check scan object result failed: trace code not found",
        }
        outcomes = submit._item_step_outcomes(task)
        self.assertEqual(
            outcomes, {"identify": "成功", "pick": "成功", "scan": "失败"}
        )

    def test_percept_pusher_line_maps_to_identify(self) -> None:
        # 真实识别失败行：object <code> not found in percept_pusher results
        task = {
            "status": "failed",
            "end_line": "[ERROR] [FVR.GroupFunc] object 6925200100302 not found in percept_pusher results",
        }
        self.assertEqual(submit._item_step_outcomes(task), {"identify": "失败"})

    def test_failed_unknown_line_marks_identify_only(self) -> None:
        task = {
            "status": "failed",
            "started_at": "2026-08-18T10:00:00Z",
            "end_line": "something unexpected happened",
        }
        self.assertEqual(submit._item_step_outcomes(task), {"identify": "成功"})

    def test_pending_or_running_item_stays_empty(self) -> None:
        self.assertEqual(submit._item_step_outcomes({"status": "pending"}), {})
        self.assertEqual(submit._item_step_outcomes({"status": "processing"}), {})

    def test_prefill_extras_carries_steps(self) -> None:
        with (
            patch.object(submit, "get_state", return_value={"ordered": []}),
            patch.object(submit, "_manual_failure_evidence", return_value=None),
            patch("ksq.web.state.loaded_tool_mapping", {}),
        ):
            extras = submit._prefill_extras(
                {"task_id": "T9"},
                [{"code": "690001", "status": "success"}],
                {},
            )
        self.assertEqual(
            (extras.get("steps") or {}).get(1),
            {"identify": "成功", "pick": "成功", "scan": "成功", "outbound": "正确", "place": "成功"},
        )


class StepFieldPrefillTests(unittest.TestCase):
    """级联步骤列按名匹配填入；「副本」列不填；值必须在选项里。"""

    SPEC = build_form_spec(
        [
            {"field_name": "SKU1是否识别成功（是否过去吸取药盒）", "type": 3, "property": {"options": [{"name": "成功"}, {"name": "失败"}]}},
            {"field_name": "SKU1吸取/抓取成功", "type": 3, "property": {"options": [{"name": "成功"}, {"name": "失败"}]}},
            {"field_name": "SKU1扫码是否成功", "type": 3, "property": {"options": [{"name": "成功"}, {"name": "失败"}]}},
            {"field_name": "SKU1出库情况", "type": 3, "property": {"options": [{"name": "正确"}, {"name": "失败"}]}},
            {"field_name": "SKU1放置是否成功", "type": 3, "property": {"options": [{"name": "成功"}, {"name": "失败"}]}},
            {"field_name": "SKU1是否识别成功（是否过去吸取药盒） 副本", "type": 3, "property": {"options": [{"name": "成功"}, {"name": "失败"}]}},
        ]
    )

    def test_all_steps_filled_for_success(self) -> None:
        values = submit_prefill(
            self.SPEC,
            {"1": None},
            steps={1: {"identify": "成功", "pick": "成功", "scan": "成功", "outbound": "正确", "place": "成功"}},
        )
        self.assertEqual(values.get("SKU1是否识别成功（是否过去吸取药盒）"), "成功")
        self.assertEqual(values.get("SKU1吸取/抓取成功"), "成功")
        self.assertEqual(values.get("SKU1扫码是否成功"), "成功")
        self.assertEqual(values.get("SKU1出库情况"), "正确")
        self.assertEqual(values.get("SKU1放置是否成功"), "成功")
        # 「副本」列不自动填
        self.assertNotIn("SKU1是否识别成功（是否过去吸取药盒） 副本", values)

    def test_chain_stops_after_failed_stage(self) -> None:
        values = submit_prefill(
            self.SPEC,
            {},
            steps={1: {"identify": "成功", "pick": "失败"}},
        )
        self.assertEqual(values.get("SKU1是否识别成功（是否过去吸取药盒）"), "成功")
        self.assertEqual(values.get("SKU1吸取/抓取成功"), "失败")
        self.assertNotIn("SKU1扫码是否成功", values)
        self.assertNotIn("SKU1出库情况", values)
        self.assertNotIn("SKU1放置是否成功", values)


def submit_prefill(spec, _unused, steps=None):
    from ksq.feishu.form_schema import prefill

    return prefill(spec, {"task_id": "T1"}, [], {"steps": steps or {}})


class ValuesToBitableCascadeTests(unittest.TestCase):
    def test_respect_cascade_false_includes_later_groups(self) -> None:
        spec = build_form_spec(SPEC_FIELDS)
        values = {
            "task_id": "T1",
            "SKU1末端工具": "双吸盘",
            "SKU2末端工具": "夹爪",
        }
        payload = values_to_bitable(spec, values, respect_cascade=False)
        self.assertEqual(payload.get("SKU2末端工具"), "夹爪")
        # 默认人工路径仍按级联隐藏 SKU2（SKU1 未答完后续步骤）。
        payload_manual = values_to_bitable(spec, {"task_id": "T1", "SKU2末端工具": "夹爪"})
        self.assertNotIn("SKU2末端工具", payload_manual)


if __name__ == "__main__":
    unittest.main()
