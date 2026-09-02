from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ksq.feishu import submit
from ksq.feishu.ai.analyzer import (
    ERROR_CATEGORIES,
    ERROR_SUBCATEGORIES,
    analyze_events,
    build_ai_prompt,
    should_analyze,
)
from ksq.feishu.event_builder import build_events
from ksq.feishu.form_builder import build_form_fields
from ksq.feishu.log_parser import parse_robot_log
from ksq.feishu.pipeline import build_submission
from ksq.web import dashboard_api


LOG = """2026-08-20T10:00:00Z case_id=L39
2026-08-20T10:00:01Z MedicinePickUpTaskItem(code=sku-a, task_id=T-1, seq_id=1)
2026-08-20T10:00:02Z start process object {'code': 'sku-a', 'barcode': '690001', 'location_code': '01-01-01'}
2026-08-20T10:00:03Z recognize success
2026-08-20T10:00:04Z pick_up_object success
2026-08-20T10:00:05Z Scanner timeout
2026-08-20T10:00:06Z retry scan
2026-08-20T10:00:07Z scan object pipeline success
2026-08-20T10:00:08Z outbound success
2026-08-20T10:00:09Z place object pipeline success
2026-08-20T10:00:10Z item sku-a process end time
2026-08-20T10:00:11Z task success
"""


TABLE_FIELDS = [
    {"field_name": "测试用例ID", "type": 1},
    {"field_name": "测试开始时间", "type": 5},
    {"field_name": "task_id", "type": 1},
    {
        "field_name": "SKU1识别",
        "type": 3,
        "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
    },
    {
        "field_name": "SKU1抓取",
        "type": 3,
        "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
    },
    {
        "field_name": "SKU1扫码",
        "type": 3,
        "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
    },
    {
        "field_name": "SKU1出库情况",
        "type": 3,
        "property": {"options": [{"name": "正确"}, {"name": "失败"}]},
    },
    {
        "field_name": "SKU1放置",
        "type": 3,
        "property": {"options": [{"name": "成功"}, {"name": "失败"}]},
    },
    {"field_name": "问题现象描述", "type": 1},
    {
        "field_name": "错误原因归类",
        "type": 3,
        "property": {"options": [{"name": value} for value in ERROR_CATEGORIES]},
    },
    {
        "field_name": "错误原因小类",
        "type": 3,
        "property": {"options": [{"name": value} for value in ERROR_SUBCATEGORIES]},
    },
    {"field_name": "问题截图", "type": 17},
]


class ParserAndEventTests(unittest.TestCase):
    def test_robot_logger_format_keeps_barcode_tool_and_seconds(self) -> None:
        actual = """[2026-08-21 12:56:19,992] [INFO] task_id: run-T-sku-a-0; current_event: start pick up object executing pipeline
[2026-08-21 12:56:38,470] [INFO] task_id: run-T-sku-a-0; current_event: start scan object pipeline
[2026-08-21 12:56:39,933] [INFO] scan_object final codes: ['690001']
[2026-08-21 12:56:49,433] [INFO] task_id: run-T-sku-a-0; current_event: place object pipeline success
[2026-08-21 12:56:49,831] [INFO] item sku-a process end time: 1
[2026-08-21 12:56:49,836] [INFO] task id: T, task item: [SubTaskDetail(barcode='690001', sku_id='sku-a', end_tools=['double_vacuum_gripper'])]
"""
        parsed = parse_robot_log(actual, "T", [{"code": "sku-a", "status": "success"}], "SUCCESS")
        self.assertEqual(parsed["start_time"], "2026-08-21T04:56:19Z")
        self.assertEqual(parsed["sku1"]["barcode"], "690001")
        self.assertEqual(parsed["sku1"]["tool"], "double_vacuum_gripper")

    def test_parser_outputs_only_proven_facts(self) -> None:
        parsed = parse_robot_log(LOG, expected_skus=[{"code": "sku-a"}, {"code": "sku-b"}])
        self.assertEqual(parsed["case_id"], "L39")
        self.assertEqual(parsed["task_id"], "T-1")
        self.assertEqual(parsed["start_time"], "2026-08-20T10:00:02Z")
        self.assertEqual(parsed["end_time"], "2026-08-20T10:00:11Z")
        self.assertEqual(parsed["task_status"], "SUCCESS")
        self.assertTrue(parsed["sku1"]["outbound"])
        self.assertFalse(parsed["sku2"]["executed"])
        self.assertIsNone(parsed["sku2"]["recognize"])

    def test_event_builder_is_compact_and_marks_retry_recovery(self) -> None:
        events = build_events(LOG)
        timeout = next(event for event in events if event["message"] == "Scanner timeout")
        self.assertEqual(timeout["type"], "scan")
        self.assertTrue(timeout["retry"])
        self.assertTrue(timeout["recovered"])
        self.assertLessEqual(len(events), 20)
        self.assertTrue(all(len(str(event["message"])) <= 240 for event in events))

    def test_motion_convergence_error_is_not_an_abnormal_event(self) -> None:
        events = build_events("[2026-08-21 12:56:31,101] [INFO] current-target error: [0.1], threshold: 0.08")
        self.assertEqual(events, [])

    def test_historical_task_errors_do_not_leak_into_current_task(self) -> None:
        history = """2026-08-20T09:00:00Z MedicinePickUpTaskItem(code=old, task_id=OLD, seq_id=1)
2026-08-20T09:00:01Z old task failed with error
""" + LOG
        parsed = parse_robot_log(history, "T-1", [{"code": "sku-a"}])
        self.assertEqual(parsed["task_id"], "T-1")
        self.assertEqual(parsed["task_status"], "SUCCESS")
        self.assertNotIn("old", {sku["code"] for sku in parsed["skus"]})


class AiAnalyzerTests(unittest.TestCase):
    def test_clean_success_never_calls_ai(self) -> None:
        with patch("ksq.feishu.ai.analyzer._request_analysis") as request:
            result = analyze_events([], "SUCCESS", [], {"enabled": True, "api_key": "x"})
        request.assert_not_called()
        self.assertFalse(should_analyze("SUCCESS", []))
        self.assertEqual(result["error_category"], "无异常")
        self.assertFalse(result["used_ai"])

    def test_ai_receives_structured_events_and_enforces_recovery_wording(self) -> None:
        events = [{"type": "scan", "message": "Scanner timeout", "retry": True, "recovered": True}]
        prompt = json.loads(build_ai_prompt("SUCCESS", [{"scan": True}], events))
        self.assertEqual(set(prompt), {"task_status", "sku1", "abnormal_events"})
        with patch(
            "ksq.feishu.ai.analyzer._request_analysis",
            return_value={
                "problem_description": "扫码超时，任务完成",
                "error_category": "扫码异常",
                "error_subcategory": "扫码超时",
                "need_manual_check": False,
            },
        ) as request:
            result = analyze_events(events, "SUCCESS", [{"scan": True}], {"enabled": True, "api_key": "x"})
        self.assertIn("重试后恢复", result["problem_description"])
        self.assertNotIn("2026-08-20", request.call_args.args[0])

        not_run = json.loads(build_ai_prompt("FAILED", [{"executed": False}], events))
        self.assertEqual(not_run["sku1"], {"executed": False})

    def test_invalid_ai_enum_is_rejected_without_guessing(self) -> None:
        with patch(
            "ksq.feishu.ai.analyzer._request_analysis",
            return_value={
                "problem_description": "异常",
                "error_category": "随便分类",
                "error_subcategory": "随便小类",
                "need_manual_check": False,
            },
        ):
            result = analyze_events(
                [{"type": "unknown", "message": "error", "retry": False, "recovered": False}],
                "FAILED",
                [],
                {"enabled": True, "api_key": "x"},
            )
        self.assertEqual(result["error_category"], "")
        self.assertEqual(result["error_subcategory"], "")
        self.assertTrue(result["need_manual_check"])


class FormAndPipelineTests(unittest.TestCase):
    def test_form_builder_uses_live_field_types_and_options(self) -> None:
        parsed = parse_robot_log(LOG)
        fields, meta = build_form_fields(
            parsed,
            {
                "problem_description": "扫码超时，重试后恢复",
                "error_category": "扫码异常",
                "error_subcategory": "扫码超时",
                "used_ai": True,
            },
            TABLE_FIELDS,
            "robot_test",
            "file-token",
        )
        self.assertEqual(fields["测试用例ID"], "L39")
        self.assertIsInstance(fields["测试开始时间"], int)
        self.assertEqual(fields["SKU1出库情况"], "正确")
        self.assertEqual(fields["问题截图"], [{"file_token": "file-token"}])
        self.assertTrue(meta["used_ai"])

    def test_form_builder_formats_text_times_as_seconds(self) -> None:
        fields, _meta = build_form_fields(
            {
                "case_id": "L39",
                "start_time": "2026-08-20T10:00:02Z",
                "end_time": "2026-08-20T10:00:11Z",
                "skus": [],
            },
            {},
            [
                {"field_name": "测试开始时间", "type": 1},
                {"field_name": "测试结束时间", "type": 1},
            ],
            "robot_test",
        )
        self.assertEqual(fields["测试开始时间"], "10:00:02")
        self.assertEqual(fields["测试结束时间"], "10:00:11")

    def test_pipeline_skips_ai_for_clean_success(self) -> None:
        form = {
            "id": "robot",
            "name": "机器人测试",
            "app_id": "app",
            "app_secret": "secret",
            "app_token": "token",
            "table_id": "table",
            "rule": "robot_test",
        }
        clean_log = LOG.replace("2026-08-20T10:00:05Z Scanner timeout\n2026-08-20T10:00:06Z retry scan\n", "")
        with (
            patch("ksq.feishu.pipeline.list_bitable_fields", return_value=TABLE_FIELDS),
            patch("ksq.feishu.ai.analyzer._request_analysis") as request,
        ):
            result = build_submission({}, [], clean_log, form, {"enabled": True, "api_key": "x"}, False)
        request.assert_not_called()
        self.assertFalse(result["meta"]["ai_called"])
        self.assertEqual(result["fields"]["错误原因归类"], "无异常")

    def test_success_does_not_upload_error_log_screenshot(self) -> None:
        order = {"task_id": "T", "lifecycle": {"end_reason": "broker_success"}}
        tasks = [{"code": "sku-a", "status": "success"}]
        with (
            patch("ksq.feishu.pipeline.list_bitable_fields", return_value=TABLE_FIELDS),
            patch("ksq.feishu.pipeline.render_log_screenshot") as render,
            patch("ksq.feishu.pipeline.upload_bitable_media") as upload,
        ):
            result = build_submission(
                order,
                tasks,
                LOG,
                {"app_id": "a", "app_secret": "s", "app_token": "t", "table_id": "tb"},
                {},
                True,
            )
        render.assert_not_called()
        upload.assert_not_called()
        self.assertEqual(result["meta"]["task_status"], "SUCCESS")


class ConfigurationAndSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        submit._SUBMITTED_KEYS.clear()

    def tearDown(self) -> None:
        submit._SUBMITTED_KEYS.clear()

    def test_feishu_log_cache_accumulates_poll_windows(self) -> None:
        first = dashboard_api._merge_feishu_log_cache_lines([], "a\nb\n")
        second = dashboard_api._merge_feishu_log_cache_lines(first, "b\nc\n")
        self.assertEqual(second, ["a", "b", "c"])

    def test_submission_prefers_cached_log_snapshot(self) -> None:
        with patch.object(submit, "fetch_logs") as fetch:
            logs, error = submit._robot_logs(
                {"feishu_log_cache": {"lines": ["cached-1", "cached-2"]}}
            )
        self.assertEqual(logs, "cached-1\ncached-2")
        self.assertEqual(error, "")
        fetch.assert_not_called()

    def test_new_config_ignores_old_business_fields(self) -> None:
        config = dashboard_api._normalize_feishu_settings(
            {
                "enabled": False,
                "tester": "old",
                "site": "old",
                "app_token": "old",
                "table_id": "old",
                "forms": [
                    {
                        "id": "new-form",
                        "name": "新表单",
                        "app_token": "token",
                        "table_id": "table",
                        "rule": "robot_test",
                    }
                ],
                "selected_form": "new-form",
            },
            None,
        )
        self.assertNotIn("tester", config)
        self.assertNotIn("site", config)
        self.assertNotIn("app_token", config)
        self.assertEqual(config["selected_form"], "new-form")

    def test_feishu_link_is_parsed_into_submission_target(self) -> None:
        form = dashboard_api._normalize_feishu_forms(
            [
                {
                    "name": "药房功能测试",
                    "url": "https://flexivrobotics.feishu.cn/base/OZwNbKMIma2yVhsKSczcObQhnLf?table=tblad10B23HgHSn0&view=vewNaYpfxR",
                }
            ],
            strict=True,
        )[0]
        self.assertEqual(form["app_token"], "OZwNbKMIma2yVhsKSczcObQhnLf")
        self.assertEqual(form["table_id"], "tblad10B23HgHSn0")
        self.assertTrue(form["url"].startswith("https://flexivrobotics.feishu.cn/base/"))

    def test_feishu_link_rejects_missing_table_query(self) -> None:
        with self.assertRaisesRegex(ValueError, "链接无效"):
            dashboard_api._normalize_feishu_forms(
                [{"name": "缺少表格", "url": "https://feishu.cn/base/token"}],
                strict=True,
            )

    def test_selected_form_is_the_only_submit_target(self) -> None:
        settings = {
            "feishu": {
                "enabled": True,
                "app_id": "app",
                "app_secret": "secret",
                "forms": [
                    {"id": "one", "name": "一", "app_token": "tok1", "table_id": "tbl1"},
                    {"id": "two", "name": "二", "app_token": "tok2", "table_id": "tbl2"},
                ],
                "selected_form": "two",
            }
        }
        with (
            patch.object(submit, "_robot_logs", return_value=("", "")),
            patch.object(submit, "build_submission", return_value={"fields": {"task_id": "T1"}, "meta": {}}),
            patch.object(submit, "create_bitable_record", return_value={"record_id": "rec1"}) as create,
        ):
            result = submit.maybe_submit_feishu_form(
                {"task_id": "T1"}, [], "test", settings, "manual", None, None
            )
        self.assertTrue(result["ok"])
        self.assertEqual(create.call_args.args[2:4], ("tok2", "tbl2"))

    def test_manual_submit_does_not_depend_on_auto_submit_switch(self) -> None:
        settings = {
            "feishu": {
                "enabled": False,
                "app_id": "app",
                "app_secret": "secret",
                "forms": [
                    {"id": "one", "name": "一", "app_token": "tok1", "table_id": "tbl1"}
                ],
                "selected_form": "one",
            }
        }
        with (
            patch.object(submit, "_robot_logs", return_value=("", "")),
            patch.object(submit, "build_submission", return_value={"fields": {}, "meta": {}}),
            patch.object(submit, "create_bitable_record", return_value={"record_id": "rec1"}),
        ):
            result = submit.maybe_submit_feishu_form(
                {"task_id": "T1"}, [], "test", settings, "manual", None, None
            )
        self.assertTrue(result["ok"])

    def test_automatic_submit_respects_auto_submit_switch(self) -> None:
        result = submit.maybe_submit_feishu_form(
            {"task_id": "T1"}, [], "test", {"feishu": {"enabled": False}}, "confirm", None, None
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "disabled")

    def test_enabled_config_rejects_incomplete_form(self) -> None:
        with self.assertRaisesRegex(ValueError, "完整填写"):
            dashboard_api._normalize_feishu_settings(
                {
                    "enabled": True,
                    "app_id": "app",
                    "app_secret": "secret",
                    "forms": [{"name": "缺少目标", "app_token": "", "table_id": ""}],
                },
                None,
                strict=True,
            )


if __name__ == "__main__":
    unittest.main()
