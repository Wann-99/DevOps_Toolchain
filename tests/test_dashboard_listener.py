from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

from ksq.web import dashboard_api


def _order(task_id: str = "task-listener") -> dict[str, object]:
    return dashboard_api._build_active_order(
        {
            "task_id": task_id,
            "items": [{"item_id": "690001", "quantity": 1}],
            "source": "order",
        }
    )


def _prompt_snapshot(order: dict[str, object]) -> dict[str, object]:
    return {
        "order": dashboard_api._public_order(order),
        "task_id": order["task_id"],
        "status": "await_confirm",
        "status_label": "人工确认",
        "needs_confirm": True,
        "confirm_closed": False,
        "await_kind": "pack",
        "active_code": "690001",
        "object_hint": "690001",
        "await_at": "2026-09-01T08:00:00Z",
        "await_line": "等待按下目标按键",
        "current_item": {
            "code": "690001",
            "status": "success",
            "await_at": "2026-09-01T08:00:00Z",
            "await_line": "等待按下目标按键",
        },
    }


class DashboardListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_api.stop_dashboard_monitor(0.2)
        self.saved_active = dashboard_api._ACTIVE_ORDER
        self.saved_loaded = dashboard_api._ACTIVE_ORDER_LOADED
        with dashboard_api._DASHBOARD_CACHE_LOCK:
            self.saved_cache = dashboard_api._DASHBOARD_CACHE
            self.saved_generation = dashboard_api._DASHBOARD_CACHE_GENERATION
            dashboard_api._DASHBOARD_CACHE = None
            dashboard_api._DASHBOARD_CACHE_GENERATION = 0

    def tearDown(self) -> None:
        dashboard_api.stop_dashboard_monitor(0.2)
        dashboard_api._ACTIVE_ORDER = self.saved_active
        dashboard_api._ACTIVE_ORDER_LOADED = self.saved_loaded
        with dashboard_api._DASHBOARD_CACHE_LOCK:
            dashboard_api._DASHBOARD_CACHE = self.saved_cache
            dashboard_api._DASHBOARD_CACHE_GENERATION = self.saved_generation

    def test_pending_confirmation_survives_reload_and_log_gap(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_file = Path(temporary.name) / "active_order.json"
        order = _order()
        dashboard_api._ACTIVE_ORDER = order
        dashboard_api._ACTIVE_ORDER_LOADED = True

        with patch.object(dashboard_api, "DASHBOARD_ACTIVE_ORDER_FILE", state_file):
            fresh = dashboard_api._reconcile_pending_confirmation(
                _prompt_snapshot(order)
            )
            self.assertTrue(fresh["needs_confirm"])
            self.assertIn("pending_confirm", dashboard_api._ACTIVE_ORDER)

            # Simulate a backend process restart: only the state file survives.
            dashboard_api._ACTIVE_ORDER = None
            dashboard_api._ACTIVE_ORDER_LOADED = False
            loaded = dashboard_api.get_active_order()
            self.assertIsNotNone(loaded)
            self.assertIn("pending_confirm", loaded)

            gap = {
                "order": dashboard_api._public_order(loaded),
                "task_id": loaded["task_id"],
                "status": "processing",
                "needs_confirm": False,
                "confirm_closed": False,
                "current_item": None,
            }
            restored = dashboard_api._reconcile_pending_confirmation(gap)
            self.assertTrue(restored["needs_confirm"])
            self.assertEqual(restored["await_line"], "等待按下目标按键")
            self.assertEqual(
                restored["confirm_fingerprint"],
                fresh["confirm_fingerprint"],
            )

            closed = deepcopy(gap)
            closed["confirm_closed"] = True
            result = dashboard_api._reconcile_pending_confirmation(closed)
            self.assertFalse(result["needs_confirm"])
            self.assertNotIn("pending_confirm", dashboard_api.get_active_order())
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("pending_confirm", persisted)

    def test_status_cache_builds_once_until_forced(self) -> None:
        payload = {"status": "idle", "needs_confirm": False}
        with (
            patch.object(
                dashboard_api,
                "_build_dashboard_snapshot",
                return_value=deepcopy(payload),
            ) as build,
            patch.object(
                dashboard_api,
                "_reconcile_pending_confirmation",
                side_effect=lambda value: value,
            ),
        ):
            first = dashboard_api.get_dashboard_monitor_snapshot(2500)
            first["status"] = "changed-by-caller"
            second = dashboard_api.get_dashboard_monitor_snapshot(2500)
            forced = dashboard_api.get_dashboard_monitor_snapshot(2500, force=True)

        self.assertEqual(second["status"], "idle")
        self.assertEqual(forced["status"], "idle")
        self.assertEqual(build.call_count, 2)

    def test_monitor_start_is_idempotent_and_stop_joins(self) -> None:
        started = Event()

        def wait_for_stop(stop_event: Event) -> None:
            started.set()
            stop_event.wait()

        with patch.object(
            dashboard_api, "_dashboard_monitor_loop", side_effect=wait_for_stop
        ) as loop:
            dashboard_api.start_dashboard_monitor()
            self.assertTrue(started.wait(1.0))
            first_thread = dashboard_api._DASHBOARD_MONITOR_THREAD
            dashboard_api.start_dashboard_monitor()
            self.assertIs(dashboard_api._DASHBOARD_MONITOR_THREAD, first_thread)
            dashboard_api.stop_dashboard_monitor(1.0)

        self.assertEqual(loop.call_count, 1)
        self.assertIsNone(dashboard_api._DASHBOARD_MONITOR_THREAD)

    def test_monitor_loop_refreshes_without_http_request(self) -> None:
        stop_event = Event()

        def refresh(_tail: int) -> dict[str, object]:
            stop_event.set()
            return {"status": "idle", "needs_confirm": False}

        with patch.object(
            dashboard_api, "_refresh_dashboard_snapshot", side_effect=refresh
        ) as refresh_snapshot:
            dashboard_api._dashboard_monitor_loop(stop_event)

        refresh_snapshot.assert_called_once_with(2500)

    def test_unchanged_item_state_is_not_written_every_poll(self) -> None:
        order = _order()
        remembered = {
            "code": "690001",
            "status": "processing",
            "elapsed_seconds": 1.0,
        }
        task = deepcopy(remembered)
        task["elapsed_seconds"] = 2.0
        order["item_states"] = {"690001": remembered}
        dashboard_api._ACTIVE_ORDER = order
        dashboard_api._ACTIVE_ORDER_LOADED = True

        with patch.object(dashboard_api, "_save_active_order_unlocked") as save:
            dashboard_api._persist_item_states(order, [task])

        save.assert_not_called()

    def test_confirmation_modal_is_outside_hidden_dashboard_view(self) -> None:
        shell = (
            Path(__file__).parents[1] / "ksq" / "web" / "templates" / "shell.html"
        ).read_text(encoding="utf-8")
        dashboard_start = shell.index('<section id="view-dashboard"')
        dashboard_end = shell.index("</section>", dashboard_start)
        modal = shell.index('<div id="dash-confirm-modal"')
        scripts = shell.index('<script src="/static/dialog.js')

        self.assertGreater(modal, dashboard_end)
        self.assertLess(modal, scripts)


if __name__ == "__main__":
    unittest.main()
