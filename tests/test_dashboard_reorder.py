"""Regression tests for the re-order (再次下单) dashboard scope confusion.

Scenario: order A completes; the same SKUs are re-ordered from the 「已下单」
list as order B.  A's robot task lines are still in the log tail.  The new
order B must not inherit A's execution states (which previously pinned the
dashboard to the previous order's page forever).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ksq.web import dashboard_api

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


def _order_b() -> dict[str, object]:
    return dashboard_api._build_active_order(
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
        _latest, _codes, last_seen = dashboard_api._discover_log_tasks(LOG_A)
        stale = dashboard_api._stale_log_tasks(order, last_seen)
        self.assertIn(TASK_A, stale)

    def test_own_task_is_never_stale(self) -> None:
        order = _order_b()
        logs = LOG_A + "\n" + LOG_B_START
        _latest, _codes, last_seen = dashboard_api._discover_log_tasks(logs)
        stale = dashboard_api._stale_log_tasks(order, last_seen)
        self.assertIn(TASK_A, stale)
        self.assertNotIn(TASK_B, stale)

    def test_missing_timestamps_keep_legacy_behaviour(self) -> None:
        order = _order_b()
        plain = f"MedicinePickUpTaskItem(code={CODE_1}, task_id={TASK_A}, seq_id=1)"
        _latest, _codes, last_seen = dashboard_api._discover_log_tasks(plain)
        stale = dashboard_api._stale_log_tasks(order, last_seen)
        self.assertEqual(stale, frozenset())


class MatchLogTaskTests(unittest.TestCase):
    def test_stale_task_not_matched_for_new_order(self) -> None:
        order = _order_b()
        latest, codes_by_task, last_seen = dashboard_api._discover_log_tasks(LOG_A)
        self.assertEqual(latest, TASK_A)
        matched = dashboard_api._match_log_task_for_order(
            order, codes_by_task, latest, last_seen
        )
        # Falls back to the order's own task id instead of A's stale scope.
        self.assertEqual(matched, TASK_B)

    def test_fresh_task_matched_by_overlap(self) -> None:
        order = _order_b()
        logs = LOG_A + "\n" + LOG_B_START
        latest, codes_by_task, last_seen = dashboard_api._discover_log_tasks(logs)
        self.assertEqual(latest, TASK_B)
        matched = dashboard_api._match_log_task_for_order(
            order, codes_by_task, latest, last_seen
        )
        self.assertEqual(matched, TASK_B)

    def test_without_timestamps_legacy_overlap_match(self) -> None:
        order = _order_b()
        latest, codes_by_task, _ = dashboard_api._discover_log_tasks(LOG_A)
        matched = dashboard_api._match_log_task_for_order(
            order, codes_by_task, latest
        )
        # No timestamp information: keep the legacy overlap behaviour.
        self.assertEqual(matched, TASK_A)


class ParseWithStaleTasksTests(unittest.TestCase):
    def test_stale_execution_does_not_poison_new_order(self) -> None:
        parsed = dashboard_api.parse_robot_log_text(
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
        parsed = dashboard_api.parse_robot_log_text(
            LOG_A,
            focus_task_id=TASK_B,
            extra_allowed_codes={CODE_1, CODE_2},
        )
        states = parsed.get("item_states") or {}
        self.assertEqual(states.get(CODE_1, {}).get("status"), "success")

    def test_fresh_execution_tracked_after_stale_tail(self) -> None:
        logs = LOG_A + "\n" + LOG_B_START
        parsed = dashboard_api.parse_robot_log_text(
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


if __name__ == "__main__":
    unittest.main()
