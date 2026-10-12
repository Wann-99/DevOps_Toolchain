"""Regression tests for the re-order (再次下单) dashboard scope confusion.

Scenario: order A completes; the same SKUs are re-ordered from the 「已下单」
list as order B.  A's robot task lines are still in the log tail.  The new
order B must not inherit A's execution states (which previously pinned the
dashboard to the previous order's page forever).
"""

from __future__ import annotations

from copy import deepcopy

from ksq.dashboard import parsing as log_parser
from ksq.order import model as order_model
from ksq.order import store as order_store

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ksq.dashboard import service as dashboard_api

TASK_A = "7016999b-5a24-4b2d-9dd4-39278a057b00"
TASK_B = "8b687893-75ab-4549-ae70-8626503a32df"
CODE_1 = "6909221668942"
CODE_2 = "6932341800305"

# Order A execution lines (yesterday), still present in the log tail.
LOG_A = "\n".join(
    [
        f"2026-08-16T09:56:31.000Z MedicinePickUpTaskItem(code={CODE_1}, task_id={TASK_A}, seq_id=1-{TASK_A}-{CODE_1}-1)",
        f"2026-08-16T09:56:32.000Z start process object{{'code': '{CODE_1}', 'location_code': '6065'}}",
        f"2026-08-16T09:57:30.000Z item {CODE_1} process end time",
        f"2026-08-16T09:57:30.100Z item {CODE_1} process duration: 59.0",
        f"2026-08-16T09:57:31.000Z MedicinePickUpTaskItem(code={CODE_2}, task_id={TASK_A}, seq_id=2-{TASK_A}-{CODE_2}-1)",
        f"2026-08-16T09:57:32.000Z start process object{{'code': '{CODE_2}', 'location_code': '6053'}}",
        f"2026-08-16T09:58:20.000Z item {CODE_2} process end time",
        f"2026-08-16T09:58:20.100Z item {CODE_2} process duration: 48.0",
        "2026-08-16T09:58:21.000Z 请取走药品，进行打包",
    ]
)

# Order B's own first execution lines (today).
LOG_B_START = "\n".join(
    [
        f"2026-08-17T10:16:35.000Z MedicinePickUpTaskItem(code={CODE_1}, task_id={TASK_B}, seq_id=1-{TASK_B}-{CODE_1}-1)",
        f"2026-08-17T10:16:36.000Z start process object{{'code': '{CODE_1}', 'location_code': '6065'}}",
    ]
)


LOG_TASK_SCOPE = "\n".join(
    [
        f"2026-08-17T10:16:35.000Z Locked task {TASK_A} successfully, business_mode_code: MODE_PICK, start processing.",
        f"2026-08-17T10:16:35.100Z task id: {TASK_A}, task item: [SubTaskDetail(barcode='{CODE_1}', item_id='1')]",
        "2026-08-17T10:16:35.150Z start process object {'code': 'P000070037', 'barcode': '"
        + CODE_1
        + "', 'location_code': '00280603'}",
        f"2026-08-17T10:16:35.200Z item P000070037 process start time: 1786961795.2",
    ]
)


def _order_b() -> dict[str, object]:
    return order_model._build_active_order(
        {
            "task_id": TASK_B,
            "order_no": "TEST20260817101628822FJ",
            "items": [
                {"item_id": CODE_1, "barcode": CODE_1, "location_code": "6065"},
                {"item_id": CODE_2, "barcode": CODE_2, "location_code": "6053"},
            ],
            "source": "test-order",
        }
    )


class StaleLogTasksTests(unittest.TestCase):
    def test_previous_order_task_is_stale(self) -> None:
        order = _order_b()
        _latest, _codes, last_seen = log_parser._discover_log_tasks(LOG_A)
        stale = log_parser._stale_log_tasks(order, last_seen)
        self.assertIn(TASK_A, stale)

    def test_own_task_is_never_stale(self) -> None:
        order = _order_b()
        logs = LOG_A + "\n" + LOG_B_START
        _latest, _codes, last_seen = log_parser._discover_log_tasks(logs)
        stale = log_parser._stale_log_tasks(order, last_seen)
        self.assertIn(TASK_A, stale)
        self.assertNotIn(TASK_B, stale)

    def test_reconciled_robot_task_is_never_stale(self) -> None:
        order = _order_b()
        order["robot_task_id"] = TASK_A
        _latest, _codes, last_seen = log_parser._discover_log_tasks(LOG_A)
        stale = log_parser._stale_log_tasks(order, last_seen)
        self.assertNotIn(TASK_A, stale)

    def test_missing_timestamps_keep_legacy_behaviour(self) -> None:
        order = _order_b()
        plain = f"MedicinePickUpTaskItem(code={CODE_1}, task_id={TASK_A}, seq_id=1)"
        _latest, _codes, last_seen = log_parser._discover_log_tasks(plain)
        stale = log_parser._stale_log_tasks(order, last_seen)
        self.assertEqual(stale, frozenset())


class MatchLogTaskTests(unittest.TestCase):
    def test_stale_task_not_matched_for_new_order(self) -> None:
        order = _order_b()
        latest, codes_by_task, last_seen = log_parser._discover_log_tasks(LOG_A)
        self.assertEqual(latest, TASK_A)
        matched = log_parser._match_log_task_for_order(
            order, codes_by_task, latest, last_seen
        )
        # Falls back to the order's own task id instead of A's stale scope.
        self.assertEqual(matched, TASK_B)

    def test_fresh_task_matched_by_overlap(self) -> None:
        order = _order_b()
        logs = LOG_A + "\n" + LOG_B_START
        latest, codes_by_task, last_seen = log_parser._discover_log_tasks(logs)
        self.assertEqual(latest, TASK_B)
        matched = log_parser._match_log_task_for_order(
            order, codes_by_task, latest, last_seen
        )
        self.assertEqual(matched, TASK_B)

    def test_without_timestamps_legacy_overlap_match(self) -> None:
        order = _order_b()
        latest, codes_by_task, _ = log_parser._discover_log_tasks(LOG_A)
        matched = log_parser._match_log_task_for_order(
            order, codes_by_task, latest
        )
        # No timestamp information: keep the legacy overlap behaviour.
        self.assertEqual(matched, TASK_A)

    def test_broker_order_id_is_not_replaced_by_robot_log_id(self) -> None:
        order = _order_b()
        order["lifecycle"] = {"ended": True, "closed": True}
        log_task = "robot-internal-task"
        logs = (
            f"2026-08-17T10:16:35.000Z MedicinePickUpTaskItem(code={CODE_1}, "
            f"task_id={log_task}, seq_id=1-{log_task}-{CODE_1}-1)"
        )
        resolved = dashboard_api._resolve_active_order(order, logs)
        self.assertEqual(resolved["task_id"], TASK_B)
        self.assertEqual(resolved["order_no"], "TEST20260817101628822FJ")

    def test_robot_logs_do_not_change_the_selected_broker_order(self) -> None:
        current = _order_b()
        current["task_id"] = TASK_A
        current["registered_at"] = "2026-08-17T10:00:00Z"
        queued = _order_b()
        queued["task_id"] = TASK_B
        queued["registered_at"] = "2026-08-17T10:16:00Z"
        current["queued_orders"] = [queued]
        logs = LOG_B_START
        with (
            patch.object(order_store, "_ACTIVE_ORDER", deepcopy(current)),
            patch.object(order_store, "_save_active_order_unlocked"),
        ):
            resolved = dashboard_api._resolve_active_order(current, logs)

        self.assertEqual(resolved["task_id"], TASK_A)
        self.assertEqual(resolved["queued_orders"], [queued])


class ParseWithStaleTasksTests(unittest.TestCase):
    def test_task_scope_blocks_unscoped_new_log_lines(self) -> None:
        order = _order_b()
        latest, codes_by_task, last_seen = log_parser._discover_log_tasks(LOG_TASK_SCOPE)
        stale = log_parser._stale_log_tasks(order, last_seen)
        parsed = log_parser.parse_robot_log_text(
            LOG_TASK_SCOPE,
            focus_task_id=TASK_B,
            extra_allowed_codes={CODE_1},
            stale_task_ids=stale,
        )
        self.assertEqual(latest, TASK_A)
        self.assertIn(CODE_1, codes_by_task[TASK_A])
        self.assertIn(TASK_A, stale)
        self.assertEqual(parsed.get("item_states"), {})

    def test_stale_execution_does_not_poison_new_order(self) -> None:
        parsed = log_parser.parse_robot_log_text(
            LOG_A,
            focus_task_id=TASK_B,
            extra_allowed_codes={CODE_1, CODE_2},
            stale_task_ids=frozenset({TASK_A}),
        )
        states = parsed.get("item_states") or {}
        # Nothing from A's completed execution may appear for the new order.
        self.assertEqual(states, {})
        self.assertFalse(parsed.get("human_confirm_seen"))
        self.assertFalse(parsed.get("order_await_active"))

    def test_stale_lines_used_without_stale_hint(self) -> None:
        # Legacy behaviour when the caller provides no stale information.
        parsed = log_parser.parse_robot_log_text(
            LOG_A,
            focus_task_id=TASK_B,
            extra_allowed_codes={CODE_1, CODE_2},
        )
        states = parsed.get("item_states") or {}
        self.assertEqual(states.get(CODE_1, {}).get("status"), "success")

    def test_fresh_execution_tracked_after_stale_tail(self) -> None:
        logs = LOG_A + "\n" + LOG_B_START
        parsed = log_parser.parse_robot_log_text(
            logs,
            focus_task_id=TASK_B,
            extra_allowed_codes={CODE_1, CODE_2},
            stale_task_ids=frozenset({TASK_A}),
        )
        states = parsed.get("item_states") or {}
        self.assertEqual(states.get(CODE_1, {}).get("status"), "started")
        self.assertEqual(states.get(CODE_1, {}).get("parent_task_id"), TASK_B)
        # CODE_2 only ran under the stale task: not present.
        self.assertNotIn(CODE_2, states)


class MergeItemStateTests(unittest.TestCase):
    def test_reassigned_processing_state_is_pruned(self) -> None:
        order = _order_b()
        order["registered_at"] = "2026-08-17T10:20:00Z"
        order["item_states"] = {
            CODE_1: {
                "code": CODE_1,
                "status": "processing",
                "parent_task_id": TASK_B,
                "started_at": "2026-08-17T10:16:00Z",
            }
        }
        with patch.object(order_store, "_ACTIVE_ORDER", deepcopy(order)):
            with patch.object(order_store, "_save_active_order_unlocked"):
                dashboard_api._prune_stale_item_states(order, frozenset({TASK_A}))
        self.assertEqual(order["item_states"], {})

    def test_new_execution_overrides_remembered_terminal(self) -> None:
        remembered = {
            "code": CODE_1,
            "status": "success",
            "status_label": "完成",
            "parent_task_id": TASK_A,
            "duration_seconds": 59.0,
        }
        fresh = {
            "code": CODE_1,
            "status": "started",
            "parent_task_id": TASK_B,
        }
        merged = dashboard_api._merge_item_state(remembered, fresh)
        self.assertEqual(merged["status"], "started")

    def test_same_task_terminal_state_survives_rollover(self) -> None:
        remembered = {
            "code": CODE_1,
            "status": "success",
            "status_label": "完成",
            "parent_task_id": TASK_A,
            "duration_seconds": 59.0,
        }
        fresh = {
            "code": CODE_1,
            "status": "pending",
            "parent_task_id": TASK_A,
        }
        merged = dashboard_api._merge_item_state(remembered, fresh)
        self.assertEqual(merged["status"], "success")

    def test_unknown_parent_keeps_legacy_protection(self) -> None:
        remembered = {
            "code": CODE_1,
            "status": "success",
            "status_label": "完成",
            "parent_task_id": "",
            "duration_seconds": 59.0,
        }
        fresh = {
            "code": CODE_1,
            "status": "pending",
            "parent_task_id": TASK_B,
        }
        merged = dashboard_api._merge_item_state(remembered, fresh)
        self.assertEqual(merged["status"], "success")


class ItemTimingTests(unittest.TestCase):
    def test_error_wait_timer_freezes_at_prompt(self) -> None:
        start = datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc)
        await_at = datetime(2026, 8, 17, 10, 0, 10, tzinfo=timezone.utc)
        item = {
            "code": CODE_1,
            "status": "await_error",
            "_started_dt": start,
            "_await_dt": await_at,
        }
        log_parser._finalize_item_timing(
            item, datetime(2026, 8, 17, 10, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(item["elapsed_seconds"], 10.0)
        log_parser._refresh_live_elapsed([item], "2026-08-17T10:02:00Z")
        self.assertEqual(item["elapsed_seconds"], 10.0)


# 新版机器人日志格式：code 是 sku_id、task_id 是 ETM 侧 id，
# start process object 行同时给出 barcode（69码）。
NEW_FORMAT_LOG = "\n".join(
    [
        "2026-08-18T21:34:23.389Z [INFO] [FVR] TaskItem: MedicinePickUpTaskItem(code=sku-aaa, task_id=8302f452-9b09-11f1-9649-d7a8da00e18f, seq_id=1787060063174.1116-8302f452-9b09-11f1-9649-d7a8da00e18f-sku-aaa-0) succeeded at step: wrapper",
        "2026-08-18T21:34:23.400Z [INFO] [FVR.NavigateFunc] task_id: 1787060063174.1116-8302f452-9b09-11f1-9649-d7a8da00e18f-sku-aaa-0; current_event: start process object {'code': 'sku-aaa', 'barcode': '6924364520087', 'location_code': '560402'}",
        "2026-08-18T21:35:00.000Z [INFO] [FVR] item sku-aaa process end time: 1787060100.0",
        "2026-08-18T21:35:00.100Z [INFO] [FVR] item sku-aaa process duration: 36.6",
    ]
)


class NewLogFormatTests(unittest.TestCase):
    """新版机器人日志：sku_id 编号经 barcode 别名翻译成订单的 69码。"""

    def test_sku_id_translated_to_barcode(self) -> None:
        parsed = log_parser.parse_robot_log_text(
            NEW_FORMAT_LOG,
            focus_task_id="8302f452-9b09-11f1-9649-d7a8da00e18f",
            extra_allowed_codes={"6924364520087"},
        )
        states = parsed.get("item_states") or {}
        self.assertIn("6924364520087", states)
        self.assertNotIn("sku-aaa", states)
        item = states["6924364520087"]
        self.assertEqual(item.get("status"), "success")
        self.assertAlmostEqual(float(item.get("duration_seconds") or 0), 36.6)
        self.assertEqual(item.get("parent_task_id"), "8302f452-9b09-11f1-9649-d7a8da00e18f")

    def test_discover_tasks_uses_barcode_codes(self) -> None:
        latest, codes_by_task, _seen = log_parser._discover_log_tasks(NEW_FORMAT_LOG)
        self.assertEqual(latest, "8302f452-9b09-11f1-9649-d7a8da00e18f")
        self.assertIn("6924364520087", codes_by_task.get("8302f452-9b09-11f1-9649-d7a8da00e18f", []))

    def test_alias_survives_after_start_line_rolls_out(self) -> None:
        # 起始行已滚出当前窗口、订单上持久化了别名：结束行仍归入 69码 子任务，
        # 不再产生 sku_id 影子子任务（2 个商品的订单不会显示成 3 个）。
        order = {"task_id": "T", "code_aliases": {"sku-aaa": "6924364520087"}}
        late_log = (
            "2026-08-18T21:40:00.000Z [INFO] [FVR] "
            "item sku-aaa process end time: 1787060100.0"
        )
        aliases = log_parser._merged_code_aliases(order, late_log)
        self.assertEqual(aliases.get("sku-aaa"), "6924364520087")
        parsed = log_parser.parse_robot_log_text(
            late_log,
            focus_task_id="8302f452-9b09-11f1-9649-d7a8da00e18f",
            extra_allowed_codes={"6924364520087"},
            aliases=aliases,
        )
        states = parsed.get("item_states") or {}
        self.assertIn("6924364520087", states)
        self.assertNotIn("sku-aaa", states)

    def test_merged_aliases_union_persisted_and_current(self) -> None:
        order = {"task_id": "T", "code_aliases": {"sku-old": "6911111111111"}}
        merged = log_parser._merged_code_aliases(order, NEW_FORMAT_LOG)
        self.assertEqual(merged.get("sku-old"), "6911111111111")
        self.assertEqual(merged.get("sku-aaa"), "6924364520087")


class KeyboardPromptCompatibilityTests(unittest.TestCase):
    def test_error_voice_prompts_trigger_item_and_order_confirmation(self) -> None:
        for prefix in ("", LOG_B_START + "\n"):
            for prompt in ("报错，请求人工处理", "料框异常，请求人工处理"):
                with self.subTest(active_item=bool(prefix), prompt=prompt):
                    line = "2026-08-17T10:16:40.000Z [INFO] 正在播放: " + prompt
                    logs = prefix + line
                    for parse in (log_parser.parse_robot_log_text, log_parser.parse_keyboard_wait):
                        result = parse(logs)
                        self.assertTrue(result["needs_confirm"])
                        self.assertEqual(result["status"], "await_error")
                        self.assertEqual(result["await_kind"], "error")
                        self.assertEqual(result["await_line"], line.split("Z ", 1)[1])
                        waiting = parse(logs + "\n2026-08-17T10:16:41.000Z 等待按下目标按键 1")
                        self.assertEqual(waiting["await_kind"], "error")
                        resumed = parse(logs + "\n2026-08-17T10:17:00.000Z 确认成功，继续操作")
                        self.assertFalse(resumed["needs_confirm"])

    def test_target_key_prompt_variants_are_detected(self) -> None:
        for prompt in (
            "程序暂停，请按下指定按键 1 后继续",
            "等待输入目标键 1",
            "Waiting for the target key 1",
            "Press any key to continue",
        ):
            with self.subTest(prompt=prompt):
                parsed = log_parser.parse_robot_log_text(prompt)
                self.assertTrue(parsed["needs_confirm"])
                self.assertTrue(parsed["order_await_active"])
                self.assertEqual(parsed["await_kind"], "pack")

    def test_unrelated_keyboard_log_does_not_trigger_popup(self) -> None:
        parsed = log_parser.parse_robot_log_text("keyboard device connected")
        self.assertFalse(parsed["needs_confirm"])


if __name__ == "__main__":
    unittest.main()
