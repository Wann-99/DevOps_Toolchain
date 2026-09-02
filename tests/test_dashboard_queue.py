from __future__ import annotations

import json
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

    def test_manual_cancel_is_a_terminal_broker_status(self) -> None:
        lifecycle, tasks = self.apply("manual_cancel")

        self.assertTrue(lifecycle["ended"])
        self.assertTrue(lifecycle["closed"])
        self.assertEqual(lifecycle["end_reason"], "broker_cancel")
        self.assertFalse(tasks[0]["active"])

    def test_log_prompt_remains_actionable_when_broker_is_terminal(self) -> None:
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
        self.assertEqual(aggregate, "await_confirm")
        self.assertTrue(tasks[0]["needs_confirm"])

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

    def test_awaiting_pack_does_not_synthesize_confirmation_without_log(self) -> None:
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
        self.assertEqual(aggregate, "order_ended")
        self.assertFalse(tasks[0]["active"])
        self.assertFalse(tasks[0]["needs_confirm"])

    def test_snapshot_does_not_use_broker_status_for_keyboard_popup(self) -> None:
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

        self.assertFalse(snapshot["needs_confirm"])
        self.assertEqual(snapshot["status"], "order_ended")
        self.assertEqual(snapshot["await_kind"], "")
        self.assertEqual(snapshot["await_line"], "")

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
        """完成之后机器人仍卡在自己的报错上，「确认处理」必须还能按。"""
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
                self.assertEqual(lifecycle["label"], "待确认报错")


class DashboardIdleLabelTests(unittest.TestCase):
    """无工单且无子任务活动时，仪表板应显示空闲而不是“工单进行中”。"""

    def test_idle_label_without_order_and_tasks(self) -> None:
        with patch.object(dashboard_api, "_save_active_order_unlocked"):
            lifecycle, _tasks, aggregate = dashboard_api._apply_order_lifecycle(
                None,
                {"human_confirm_seen": False},
                {"ok": False},
                [],
            )
        self.assertEqual(aggregate, "idle")
        self.assertEqual(lifecycle["label"], "空闲")

    def test_running_label_kept_with_order(self) -> None:
        order, task = lifecycle_input("running")
        task["status"] = "processing"
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
        ):
            lifecycle, _tasks, _aggregate = dashboard_api._apply_order_lifecycle(
                order,
                {"human_confirm_seen": False},
                {"ok": False},
                [task],
            )
        self.assertEqual(lifecycle["label"], "工单进行中")


class BrokerOrderCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_api._BROKER_ORDER_CACHE.clear()

    def tearDown(self) -> None:
        dashboard_api._BROKER_ORDER_CACHE.clear()

    def test_slow_fetch_starts_cache_ttl_after_response(self) -> None:
        with (
            patch.object(
                dashboard_api.time, "monotonic", side_effect=[10.0, 20.0, 21.0]
            ),
            patch.object(
                order_api,
                "get_task_detail",
                return_value=(
                    200,
                    {"data": {"task_id": "task-cache", "status": "running"}},
                ),
            ) as get_detail,
        ):
            first = dashboard_api._fetch_broker_order("task-cache")
            second = dashboard_api._fetch_broker_order("task-cache")

        self.assertTrue(first["ok"])
        self.assertEqual(second, first)
        get_detail.assert_called_once()


class BrokerTaskIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_active = dashboard_api._ACTIVE_ORDER
        self.old_loaded = dashboard_api._ACTIVE_ORDER_LOADED
        dashboard_api._ACTIVE_ORDER = None
        dashboard_api._ACTIVE_ORDER_LOADED = True
        dashboard_api._BROKER_TASK_IDENTITY_CACHE.clear()

    def tearDown(self) -> None:
        dashboard_api._ACTIVE_ORDER = self.old_active
        dashboard_api._ACTIVE_ORDER_LOADED = self.old_loaded
        dashboard_api._BROKER_TASK_IDENTITY_CACHE.clear()

    def test_log_task_is_replaced_by_broker_task_with_same_order_identity(self) -> None:
        order = dashboard_api._build_active_order(
            {
                "task_id": "robot-task",
                "order_no": "ORDER-42",
                "platform_order_no": "MT-42",
                "items": [],
                "source": "log",
            }
        )
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                order_api,
                "list_tasks",
                return_value={
                    "tasks": [
                        {
                            "task_id": "broker-task",
                            "task_detail": {
                                "order_no": "ORDER-42",
                                "platform_order_no": "MT-42",
                            },
                        }
                    ]
                },
            ),
        ):
            result = dashboard_api._reconcile_broker_task_id(order, "test")

        self.assertEqual(result["task_id"], "broker-task")
        self.assertEqual(result["robot_task_id"], "robot-task")
        self.assertEqual(result["source"], "order")

    def test_legacy_order_source_task_is_also_reconciled(self) -> None:
        order = dashboard_api._build_active_order(
            {
                "task_id": "robot-task",
                "order_no": "ORDER-42",
                "platform_order_no": "MT-42",
                "items": [],
                "source": "order",
            }
        )
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                order_api,
                "list_tasks",
                return_value={
                    "tasks": [
                        {
                            "task_id": "broker-task",
                            "task_detail": {
                                "order_no": "ORDER-42",
                                "platform_order_no": "MT-42",
                            },
                        }
                    ]
                },
            ),
        ):
            result = dashboard_api._reconcile_broker_task_id(order, "test")

        self.assertEqual(result["task_id"], "broker-task")
        self.assertEqual(result["robot_task_id"], "robot-task")

    def test_terminal_active_order_promotes_newest_running_broker_order(self) -> None:
        order = dashboard_api._build_active_order(
            {
                "task_id": "old-task",
                "order_no": "OLD-1",
                "items": [],
                "source": "order",
            }
        )
        with (
            patch.object(dashboard_api, "_ACTIVE_ORDER", order),
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                dashboard_api,
                "_fetch_broker_order",
                return_value={"ok": True, "terminal": True, "status": "success"},
            ),
            patch.object(
                order_api,
                "list_tasks",
                return_value={
                    "tasks": [
                        {
                            "task_id": "new-task",
                            "status": "running",
                            "create_time": "2026-08-17 18:16:28",
                            "task_detail": {
                                "order_no": "NEW-1",
                                "platform_order_no": "MT-1",
                                "items": [
                                    {
                                        "item_id": "123",
                                        "barcode": "690123",
                                        "item_name": "测试商品",
                                    }
                                ],
                            },
                        }
                    ]
                },
            ),
        ):
            result = dashboard_api._promote_latest_broker_order(order, "test")

        self.assertEqual(result["task_id"], "new-task")
        self.assertEqual(result["order_no"], "NEW-1")
        self.assertEqual(result["items"][0]["barcode"], "690123")
        self.assertEqual(result["registered_at"], "2026-08-17T10:16:28Z")
        log = "\n".join(
            [
                "2026-08-17T10:16:27.000Z MedicinePickUpTaskItem("
                "code=690123, task_id=old-robot-task, seq_id=1)",
                "2026-08-17T10:16:29.000Z MedicinePickUpTaskItem("
                "code=690123, task_id=robot-task, seq_id=1)",
            ]
        )
        _latest, _codes, last_seen = dashboard_api._discover_log_tasks(log)
        stale = dashboard_api._stale_log_tasks(result, last_seen)
        self.assertIn("old-robot-task", stale)
        self.assertNotIn("robot-task", stale)

    def test_order_payload_cannot_override_local_registration_time(self) -> None:
        payload = order_payload("task-1", "690001")
        payload["registered_at"] = "2000-01-01T00:00:00Z"
        with patch.object(dashboard_api, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(
                2026, 8, 17, 10, 16, 28, tzinfo=timezone.utc
            )
            result = dashboard_api._build_active_order(payload)

        self.assertEqual(result["registered_at"], "2026-08-17T10:16:28Z")

    def test_etm_recovery_preserves_cloud_create_time(self) -> None:
        cloud = {
            "task_id": "etm-task",
            "create_time": "2026-08-17T18:16:28+08:00",
            "params": {
                "order_no": "PROD-1",
                "items": [{"item_id": "690123", "location_code": "010203"}],
            },
        }
        with (
            patch.object(dashboard_api, "_save_active_order_unlocked"),
            patch.object(
                dashboard_api,
                "_etm_get_json",
                side_effect=[
                    {"data": {"task_id": "etm-task"}},
                    {"data": cloud},
                ],
            ),
        ):
            result, status = dashboard_api._sync_order_from_etm(
                None, {"etm_base_url": "http://127.0.0.1:12005"}
            )

        self.assertTrue(status["cloud_ok"])
        self.assertEqual(result["task_id"], "etm-task")
        self.assertEqual(result["registered_at"], "2026-08-17T10:16:28Z")


class SingleActiveOrderTests(unittest.TestCase):
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

    def test_second_order_is_rejected_while_first_is_active(self) -> None:
        first = self.register("task-1", "690001")

        self.assertEqual(first["queue_position"], 0)
        with self.assertRaisesRegex(ValueError, "上一单尚未完成"):
            self.register("task-2", "690002")
        status = dashboard_api.order_queue_status()
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["queued_count"], 0)
        self.assertEqual(dashboard_api.get_active_order()["task_id"], "task-1")
        persisted = self.state_file.read_text(encoding="utf-8")
        self.assertNotIn('"queued_orders"', persisted)

    def test_grouped_duplicate_sku_keeps_both_dashboard_rows(self) -> None:
        body = order_payload("task-group", "690001")
        first = dict(body["items"][0], group_id="A01", group_field="批次")
        second = dict(body["items"][0], group_id="A02", group_field="批次")
        body["items"] = [first, second]
        order = dashboard_api.register_created_order(
            "task-group", body, "test-order"
        )

        tasks = dashboard_api._merge_order_items(
            order,
            {
                "item_states": {
                    "690001": {
                        "code": "690001",
                        "status": "processing",
                        "status_label": "处理中",
                    }
                }
            },
            focus_task_id="task-group",
        )

        self.assertEqual([task["group_id"] for task in tasks], ["A01", "A02"])
        self.assertEqual([task["status"] for task in tasks], ["processing"] * 2)

    def test_manual_transfer_requires_completion_before_next_order(self) -> None:
        self.register("task-1", "690001")
        dashboard_api._ACTIVE_ORDER["lifecycle"]["broker_status"] = "manual_transferred"

        with self.assertRaisesRegex(ValueError, "上一单尚未完成"):
            self.register("task-2", "690002")
        self.assertEqual(dashboard_api.get_active_order()["task_id"], "task-1")

    def test_legacy_queued_broker_order_is_promoted_after_closed_log_order(self) -> None:
        current = dashboard_api._build_active_order(order_payload("robot-log", "690001"))
        current["source"] = "log"
        current["lifecycle"]["closed"] = True
        queued = dashboard_api._build_active_order(order_payload("broker-task", "690002"))
        self.state_file.write_text(
            json.dumps({"task_id": current["task_id"], "lifecycle": current["lifecycle"],
                        "items": current["items"], "source": "log",
                        "queued_orders": [queued]}),
            encoding="utf-8",
        )
        dashboard_api._ACTIVE_ORDER = None
        dashboard_api._ACTIVE_ORDER_LOADED = False

        active = dashboard_api.get_active_order()

        self.assertEqual(active["task_id"], "broker-task")
        self.assertNotIn("queued_orders", json.loads(self.state_file.read_text(encoding="utf-8")))

        dashboard_api._ACTIVE_ORDER["lifecycle"][
            "broker_status"
        ] = "manual_transferred_completed"
        self.register("task-2", "690002")
        self.assertEqual(dashboard_api.get_active_order()["task_id"], "task-2")
        self.assertEqual(dashboard_api.order_queue_status()["queued_count"], 0)

    def test_unresolved_item_prompt_requires_manual_completion(self) -> None:
        self.register("task-1", "690001")
        dashboard_api._ACTIVE_ORDER["item_states"] = {
            "690001": {"status": "await_confirm", "needs_confirm": True}
        }

        self.assertTrue(dashboard_api.active_order_requires_manual_completion())

        dashboard_api._ACTIVE_ORDER["lifecycle"][
            "broker_status"
        ] = "manual_claimed_completed"
        self.assertFalse(dashboard_api.active_order_requires_manual_completion())

    def test_order_level_log_prompt_no_longer_requires_manual_completion(self) -> None:
        """订单级日志提示仅作展示，不再触发人工完成拦截（lifecycle 由 Broker 驱动）。"""
        self.register("task-1", "690001")
        dashboard_api._ACTIVE_ORDER["lifecycle"].update(
            {
                "closed": False,
                "end_source": "log",
                "end_reason": "human_pack",
            }
        )

        self.assertFalse(dashboard_api.active_order_requires_manual_completion())

    def test_third_order_is_rejected_before_broker_request(self) -> None:
        self.register("task-1", "690001")
        with (
            patch.object(order_api, "load_order_config") as load_config,
            self.assertRaisesRegex(order_api.OrderQueueConflict, "上一单尚未完成"),
        ):
            order_api.create_order({"items": [], "mode": "test"})
        load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
