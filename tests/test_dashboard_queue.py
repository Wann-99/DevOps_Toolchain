from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ksq.web import dashboard_api, order_api


def order_payload(task_id: str, code: str) -> dict[str, object]:
    return {
        "task_id": task_id,
        "order_no": "ORDER-" + task_id,
        "items": [
            {
                "item_id": code,
                "barcode": code,
                "location_code": "010203",
                "quantity": 1,
            }
        ],
        "source": "order",
    }


def lifecycle_input(status: str) -> tuple[dict[str, object], dict[str, object]]:
    order = dashboard_api._build_active_order(order_payload("task-1", "690001"))
    tasks = [
        {
            "code": "690001",
            "status": "success" if status == "success" else "processing",
            "status_label": "完成" if status == "success" else "处理中",
            "started_at": "2026-08-12T10:00:00Z",
            "ended_at": "2026-08-12T10:01:00Z" if status == "success" else None,
            "elapsed_seconds": 60.0,
            "duration_seconds": 60.0 if status == "success" else None,
            "active": True,
            "needs_confirm": False,
        }
    ]
    return order, tasks[0]


class DashboardTerminalTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_active = dashboard_api._ACTIVE_ORDER
        self.old_loaded = dashboard_api._ACTIVE_ORDER_LOADED
        dashboard_api._ACTIVE_ORDER = None
        dashboard_api._ACTIVE_ORDER_LOADED = True

    def tearDown(self) -> None:
        dashboard_api._ACTIVE_ORDER = self.old_active
        dashboard_api._ACTIVE_ORDER_LOADED = self.old_loaded

    def apply(self, broker_status: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        order, task = lifecycle_input(broker_status)
        tasks = [task]
        broker = {
            "ok": True,
            "status": broker_status,
            "status_label": broker_status,
            "ended": broker_status in dashboard_api._BROKER_ORDER_ENDED,
            "terminal": broker_status in dashboard_api._BROKER_ORDER_TERMINAL,
        }
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                dashboard_api,
                "datetime",
                wraps=datetime,
            ) as clock,
        ):
            clock.now.return_value = datetime(2026, 8, 12, 10, 2, tzinfo=timezone.utc)
            lifecycle, result_tasks, _aggregate = dashboard_api._apply_order_lifecycle(
                order,
                {"human_confirm_seen": False},
                broker,
                tasks,
            )
        return lifecycle, result_tasks

    def test_success_freezes_elapsed_and_clears_stale_active_flag(self) -> None:
        lifecycle, tasks = self.apply("success")

        self.assertTrue(lifecycle["closed"])
        self.assertEqual(lifecycle["timer_stop_reason"], "broker_ended")
        self.assertEqual(lifecycle["frozen_elapsed_seconds"], 60.0)
        self.assertFalse(tasks[0]["active"])
        first = dashboard_api._order_elapsed_seconds(
            None, tasks, lifecycle, "2026-08-12T10:10:00Z"
        )
        second = dashboard_api._order_elapsed_seconds(
            None, tasks, lifecycle, "2026-08-12T11:10:00Z"
        )
        self.assertEqual(first, 60.0)
        self.assertEqual(second, 60.0)

    def test_broker_terminal_overrides_stale_human_prompt(self) -> None:
        order, task = lifecycle_input("success")
        task["status"] = "await_confirm"
        task["needs_confirm"] = True
        broker = {
            "ok": True,
            "status": "success",
            "status_label": "完成",
            "ended": True,
            "terminal": True,
        }
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
        ):
            lifecycle, tasks, aggregate = dashboard_api._apply_order_lifecycle(
                order,
                {
                    "human_confirm_seen": True,
                    "human_confirm_kind": "pack",
                    "order_await_active": True,
                },
                broker,
                [task],
            )

        self.assertTrue(lifecycle["closed"])
        self.assertEqual(aggregate, "success")
        self.assertFalse(tasks[0]["needs_confirm"])

    def test_awaiting_pack_keeps_human_confirmation_active(self) -> None:
        order, task = lifecycle_input("awaiting_pack")
        task["status"] = "await_confirm"
        task["status_label"] = "人工确认"
        task["needs_confirm"] = True
        broker = {
            "ok": True,
            "status": "awaiting_pack",
            "status_label": "等待打包",
            "ended": True,
            "terminal": False,
        }

        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
        ):
            lifecycle, tasks, aggregate = dashboard_api._apply_order_lifecycle(
                order,
                {
                    "human_confirm_seen": True,
                    "human_confirm_kind": "pack",
                    "order_await_active": True,
                },
                broker,
                [task],
            )

        self.assertTrue(lifecycle["ended"])
        self.assertFalse(lifecycle["closed"])
        self.assertEqual(lifecycle["label"], "待人工确认")
        self.assertEqual(aggregate, "await_confirm")
        self.assertFalse(tasks[0]["active"])
        self.assertTrue(tasks[0]["needs_confirm"])

    def test_awaiting_pack_synthesizes_confirmation_without_log_prompt(self) -> None:
        order, task = lifecycle_input("awaiting_pack")
        broker = {
            "ok": True,
            "status": "awaiting_pack",
            "status_label": "等待打包",
            "ended": True,
            "terminal": False,
        }

        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
        ):
            lifecycle, tasks, aggregate = dashboard_api._apply_order_lifecycle(
                order,
                {"human_confirm_seen": False},
                broker,
                [task],
            )

        self.assertTrue(lifecycle["ended"])
        self.assertFalse(lifecycle["closed"])
        self.assertEqual(lifecycle["end_reason"], "broker_awaiting_pack")
        self.assertEqual(aggregate, "await_confirm")
        self.assertFalse(tasks[0]["active"])

    def test_snapshot_exposes_broker_pack_confirmation_for_popup(self) -> None:
        order = dashboard_api._build_active_order(order_payload("task-1", "690001"))
        broker = {
            "ok": True,
            "status": "awaiting_pack",
            "status_label": "等待打包",
            "ended": True,
            "terminal": False,
            "task_id": "task-1",
            "order_no": "ORDER-task-1",
        }

        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                dashboard_api,
                "load_dashboard_settings",
                return_value={"mode": "test", "auto_confirm": False},
            ),
            patch.object(
                dashboard_api,
                "inspect_container",
                return_value={"running": True, "status": "running", "message": ""},
            ),
            patch.object(dashboard_api, "fetch_logs", return_value={"logs": ""}),
            patch.object(dashboard_api, "_fetch_broker_order", return_value=broker),
            patch.object(dashboard_api, "_is_broker_configured", return_value=True),
        ):
            snapshot = dashboard_api.get_dashboard_snapshot(2500)

        self.assertTrue(snapshot["needs_confirm"])
        self.assertEqual(snapshot["status"], "await_confirm")
        self.assertEqual(snapshot["await_kind"], "pack")
        self.assertEqual(snapshot["await_line"], "Broker 工单等待人工打包确认")

    def test_manual_transfer_freezes_but_is_not_queue_terminal(self) -> None:
        lifecycle, tasks = self.apply("manual_transferred")

        self.assertTrue(lifecycle["ended"])
        self.assertFalse(lifecycle["closed"])
        self.assertEqual(lifecycle["timer_stop_reason"], "broker_ended")
        self.assertFalse(tasks[0]["active"])
        order = dashboard_api._build_active_order(order_payload("task-1", "690001"))
        order["lifecycle"]["broker_status"] = "manual_transferred"
        self.assertFalse(dashboard_api._order_is_queue_terminal(order))

    def test_manual_complete_is_queue_terminal(self) -> None:
        order = dashboard_api._build_active_order(order_payload("task-1", "690001"))
        order["lifecycle"]["broker_status"] = "manual_transferred_completed"
        self.assertTrue(dashboard_api._order_is_queue_terminal(order))

        lifecycle, _tasks = self.apply("manual_transferred_completed")
        self.assertTrue(lifecycle["closed"])
        self.assertEqual(lifecycle["end_reason"], "broker_manual_completed")
        self.assertEqual(lifecycle["label"], "人工处理已完成")

    def test_manual_flow_keeps_robot_error_prompt_clickable(self) -> None:
        """标记完成之后机器人仍卡在自己的报错上，「确认处理」必须还能按。"""
        for status in ("manual_claimed_in_progress", "manual_claimed_completed"):
            with self.subTest(status=status):
                order, task = lifecycle_input(status)
                task["status"] = "await_error"
                task["status_label"] = "报错·请求人工处理"
                task["needs_confirm"] = True
                broker = {
                    "ok": True,
                    "status": status,
                    "status_label": status,
                    "ended": status in dashboard_api._BROKER_ORDER_ENDED,
                    "terminal": status in dashboard_api._BROKER_ORDER_TERMINAL,
                }
                with (
                    patch.object(dashboard_api, "_ACTIVE_ORDER", order),
                    patch.object(dashboard_api, "_save_active_order_unlocked"),
                ):
                    lifecycle, tasks, _aggregate = dashboard_api._apply_order_lifecycle(
                        order,
                        {"human_confirm_seen": True, "human_confirm_kind": "error"},
                        broker,
                        [task],
                    )

                self.assertTrue(tasks[0]["needs_confirm"])
                self.assertFalse(tasks[0]["active"])
                self.assertNotEqual(lifecycle["label"], "待确认报错")


class TwoOrderQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "active-order.json"
        self.file_patch = patch.object(
            dashboard_api, "DASHBOARD_ACTIVE_ORDER_FILE", self.state_file
        )
        self.file_patch.start()
        self.old_active = dashboard_api._ACTIVE_ORDER
        self.old_loaded = dashboard_api._ACTIVE_ORDER_LOADED
        dashboard_api._ACTIVE_ORDER = None
        dashboard_api._ACTIVE_ORDER_LOADED = True

    def tearDown(self) -> None:
        dashboard_api._ACTIVE_ORDER = self.old_active
        dashboard_api._ACTIVE_ORDER_LOADED = self.old_loaded
        self.file_patch.stop()
        self.temporary.cleanup()

    def register(self, task_id: str, code: str) -> dict[str, object]:
        body = order_payload(task_id, code)
        return dashboard_api.register_created_order(task_id, body, "order")

    def test_second_order_waits_and_is_persisted(self) -> None:
        first = self.register("task-1", "690001")
        second = self.register("task-2", "690002")

        self.assertEqual(first["queue_position"], 0)
        self.assertEqual(second["queue_position"], 1)
        status = dashboard_api.order_queue_status()
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["queued"][0]["task_id"], "task-2")
        persisted = self.state_file.read_text(encoding="utf-8")
        self.assertIn('"queued_orders"', persisted)
        self.assertIn('"task-2"', persisted)

    def test_manual_transfer_does_not_promote_but_manual_complete_does(self) -> None:
        self.register("task-1", "690001")
        self.register("task-2", "690002")
        dashboard_api._ACTIVE_ORDER["lifecycle"]["broker_status"] = "manual_transferred"

        self.assertFalse(dashboard_api.promote_queued_order_if_ready())
        self.assertEqual(dashboard_api.get_active_order()["task_id"], "task-1")

        dashboard_api._ACTIVE_ORDER["lifecycle"][
            "broker_status"
        ] = "manual_transferred_completed"
        self.assertTrue(dashboard_api.promote_queued_order_if_ready())
        self.assertEqual(dashboard_api.get_active_order()["task_id"], "task-2")
        self.assertEqual(dashboard_api.order_queue_status()["queued_count"], 0)

    def test_third_order_is_rejected_before_broker_request(self) -> None:
        self.register("task-1", "690001")
        self.register("task-2", "690002")
        with (
            patch.object(order_api, "load_order_config") as load_config,
            self.assertRaisesRegex(order_api.OrderQueueConflict, "已有两单"),
        ):
            order_api.create_order({"items": [], "mode": "test"})
        load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
