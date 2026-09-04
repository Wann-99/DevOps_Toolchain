"""Regression tests for HTTP body consumption, map telemetry, and path loading."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ksq.web import auth, dashboard_api, edit_workspace, pages, state
from ksq.web import handlers

try:
    QueryHandler = handlers.QueryHandler
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    QueryHandler = None


@unittest.skipIf(QueryHandler is None, "HTTP handlers require Python 3.12 or older")
class HandlerRegressionTests(unittest.TestCase):
    def _request(
        self,
        method: str,
        path: str,
        role: str | None,
        payload: object = None,
        raw_body: bytes | None = None,
    ) -> tuple[QueryHandler, int, object]:
        raw = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if raw_body is None and payload is not None
            else (raw_body or b"")
        )
        handler = QueryHandler.__new__(QueryHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(raw)
        response: dict[str, object] = {}
        handler._send_json = lambda status, data: response.update(
            status=int(status), data=data
        )
        handler._send_redirect = lambda location: response.update(
            status=302, data={"location": location}
        )
        handler.send_error = lambda status, message="": response.update(
            status=int(status), data={"error": message}
        )
        session = (
            None
            if role is None
            else {"username": "u", "display_name": "u", "role": role}
        )
        with patch.object(auth, "session_from_cookie", return_value=session):
            getattr(handler, f"do_{method}")()
        return handler, int(response["status"]), response["data"]

    def test_unauthenticated_post_drains_body_before_returning(self) -> None:
        handler, status, data = self._request(
            "POST",
            "/api/edit/persist",
            role=None,
            raw_body=b'{"ignored": true}',
        )
        self.assertEqual(status, 401)
        self.assertIn("error", data)
        self.assertEqual(handler.rfile.read(), b"")

    def test_viewer_forbidden_post_drains_body_before_returning(self) -> None:
        handler, status, data = self._request(
            "POST",
            "/api/edit/persist",
            role=auth.ROLE_VIEWER,
            raw_body=b'{"ignored": true}',
        )
        self.assertEqual(status, 403)
        self.assertIn("管理员", data["error"])
        self.assertEqual(handler.rfile.read(), b"")

    def test_rejected_put_drains_body_before_returning(self) -> None:
        for role in (None, auth.ROLE_VIEWER):
            with self.subTest(role=role):
                handler, status, data = self._request(
                    "PUT",
                    "/api/order/config",
                    role=role,
                    raw_body=b'{"server": "ignored"}',
                )
                self.assertIn(status, (401, 403))
                self.assertIn("error", data)
                self.assertEqual(handler.rfile.read(), b"")

    def test_invalid_put_task_id_drains_body_before_returning(self) -> None:
        handler, status, data = self._request(
            "PUT",
            "/api/order/tasks/",
            role=auth.ROLE_ADMIN,
            raw_body=b'{"retail_order_id": "ignored"}',
        )
        self.assertEqual(status, 400)
        self.assertIn("task_id", data["error"])
        self.assertEqual(handler.rfile.read(), b"")

    def test_persist_route_drains_body_even_without_request_fields(self) -> None:
        with (
            patch.object(state, "require_full_data_source"),
            patch.object(edit_workspace, "persist_dirty_files", return_value={}) as persist,
        ):
            handler, status, data = self._request(
                "POST",
                "/api/edit/persist",
                role=auth.ROLE_ADMIN,
                raw_body=b"{}",
            )
        self.assertEqual(status, 200)
        self.assertEqual(data, {})
        persist.assert_called_once_with()
        self.assertEqual(handler.rfile.read(), b"")

    def test_viewer_cannot_sync_active_dashboard_order(self) -> None:
        payload = {
            "task_id": "task-viewer",
            "items": [{"item_id": "SKU-1", "quantity": 1}],
        }
        with patch.object(dashboard_api, "set_active_order") as set_order:
            handler, status, data = self._request(
                "POST",
                "/api/dashboard/order",
                role=auth.ROLE_VIEWER,
                payload=payload,
            )
        self.assertEqual(status, 403)
        self.assertIn("管理员", data["error"])
        set_order.assert_not_called()
        self.assertEqual(handler.rfile.read(), b"")

    def test_dashboard_order_rejects_empty_or_malformed_items(self) -> None:
        invalid_payloads = (
            {},
            {"items": []},
            {"items": ["SKU-1"]},
            {"items": [{"item_id": "SKU-1", "quantity": True}]},
            {"items": [{"item_id": "SKU-1", "quantity": 0}]},
            {"items": [{"item_id": "SKU-1", "quantity": -1}]},
            {"items": [{"item_id": "SKU-1", "quantity": 1.5}]},
            {"items": [{"item_id": "SKU-1"}]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(
                dashboard_api, "set_active_order"
            ) as set_order:
                handler, status, _data = self._request(
                    "POST",
                    "/api/dashboard/order",
                    role=auth.ROLE_ADMIN,
                    payload=payload,
                )
            self.assertEqual(status, 400)
            set_order.assert_not_called()
            self.assertEqual(handler.rfile.read(), b"")

    def test_dashboard_order_accepts_positive_integer_items(self) -> None:
        payload = {
            "task_id": "task-admin",
            "items": [{"item_id": "SKU-1", "quantity": 2}],
        }
        with patch.object(
            dashboard_api, "set_active_order", return_value={"task_id": "task-admin"}
        ) as set_order:
            handler, status, data = self._request(
                "POST",
                "/api/dashboard/order",
                role=auth.ROLE_ADMIN,
                payload=payload,
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        set_order.assert_called_once_with(payload)
        self.assertEqual(handler.rfile.read(), b"")

    def test_dashboard_status_reads_resident_monitor_cache(self) -> None:
        with patch.object(
            dashboard_api,
            "get_dashboard_monitor_snapshot",
            return_value={"status": "idle"},
        ) as get_snapshot:
            _handler, status, data = self._request(
                "GET",
                "/api/dashboard/status?tail=2500",
                role=auth.ROLE_VIEWER,
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"status": "idle"})
        get_snapshot.assert_called_once_with(2500, force=False)

    def test_dashboard_manual_refresh_forces_new_snapshot(self) -> None:
        with patch.object(
            dashboard_api,
            "get_dashboard_monitor_snapshot",
            return_value={"status": "processing"},
        ) as get_snapshot:
            _handler, status, _data = self._request(
                "GET",
                "/api/dashboard/status?tail=2500&refresh=1",
                role=auth.ROLE_VIEWER,
            )

        self.assertEqual(status, 200)
        get_snapshot.assert_called_once_with(2500, force=True)

    def test_map_telemetry_returns_coherent_read_only_snapshot(self) -> None:
        snapshot = {
            "seq": 12,
            "received_at": 1788340800.050,
            "stale": False,
            "partial": False,
            "age_ms": 0,
            "pose": {"x": 1.0, "y": 2.0, "yaw": 0.25},
            "scan_pose": {"x": 1.0, "y": 2.0, "yaw": 0.25},
            "localization_pose": {"x": 1.0, "y": 2.0, "yaw": 0.25},
            "localization_quality": 98,
            "laser_points": [
                {"distance": 1.2, "angle": -0.5, "valid": True}
            ],
            "sensor_info": {"point_count": 1, "valid_count": 1},
            "errors": {},
            "error": None,
        }
        with patch.object(
            handlers.robot_map_api,
            "get_telemetry_snapshot",
            return_value=snapshot,
        ) as get_snapshot:
            _handler, status, data = self._request(
                "GET", "/api/map/telemetry", role=auth.ROLE_VIEWER
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, snapshot)
        get_snapshot.assert_called_once_with()

    def test_map_current_action_translates_idle_404_to_an_idle_payload(self) -> None:
        with patch.object(
            handlers.robot_map_api,
            "get_current_action",
            side_effect=handlers.RobotApiError("Action Not Found", status_code=404),
        ) as get_current_action:
            _handler, status, data = self._request(
                "GET", "/api/map/current-action", role=auth.ROLE_VIEWER
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"active": False, "action": None})
        get_current_action.assert_called_once_with(expected_base_url=None)

    def test_map_current_action_returns_the_active_action(self) -> None:
        action = {
            "action_id": 13,
            "action_name": "slamtec.agent.actions.GoHomeAction",
            "state": {"status": 1, "result": 0},
        }
        with patch.object(
            handlers.robot_map_api, "get_current_action", return_value=action
        ) as get_current_action:
            _handler, status, data = self._request(
                "GET", "/api/map/current-action", role=auth.ROLE_VIEWER
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"active": True, "action": action})
        get_current_action.assert_called_once_with(expected_base_url=None)

    def test_map_home_pose_and_patrol_plan_are_exposed_read_only(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with patch.object(
            handlers.robot_map_api,
            "get_home_pose",
            return_value={"x": 1.0, "y": 2.0, "yaw": 0.3},
        ):
            _handler, status, data = self._request(
                "GET", "/api/map/home-pose", role=auth.ROLE_VIEWER
            )
        self.assertEqual(status, 200)
        self.assertEqual(data["pose"]["yaw"], 0.3)

        with patch.object(
            handlers.robot_map_api,
            "get_remaining_path",
            return_value={"path_points": [[1, 2], [3, 4]]},
        ) as get_path:
            _handler, status, data = self._request(
                "GET",
                "/api/map/path?expected_robot_base_url=" + base_url,
                role=auth.ROLE_VIEWER,
            )
        self.assertEqual(status, 200)
        self.assertEqual(data["path_points"], [[1, 2], [3, 4]])
        get_path.assert_called_once_with(expected_base_url=base_url)

        with patch.object(
            handlers.robot_map_api,
            "get_home_pose",
            side_effect=handlers.RobotApiError("timeout", status_code=504),
        ):
            _handler, status, data = self._request(
                "GET", "/api/map/home-pose", role=auth.ROLE_VIEWER
            )
        self.assertEqual(status, 502)
        self.assertEqual(data, {"error": "timeout"})

    def test_map_speed_limit_uses_the_current_robot_maximum(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with patch.object(
            handlers.robot_map_api, "get_max_moving_speed", return_value=0.8
        ) as get_max_speed:
            _handler, status, data = self._request(
                "GET",
                "/api/map/speed-limit?expected_robot_base_url=" + base_url,
                role=auth.ROLE_VIEWER,
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            data,
            {
                "min_speed_mps": 0.08,
                "max_speed_mps": 0.8,
                "default_speed_mps": 0.64,
            },
        )
        get_max_speed.assert_called_once_with(expected_base_url=base_url)

    def test_map_settings_exposes_force_switch_confirmation(self) -> None:
        payload = {
            "robot_base_url": "http://192.168.5.10:1448",
            "expected_robot_base_url": "http://192.168.5.9:1448",
        }
        with patch.object(
            handlers.robot_map_api,
            "save_settings",
            side_effect=handlers.robot_map_api.RobotConnectionSwitchRequired(
                "无法确认旧底盘已停止"
            ),
        ):
            _handler, status, data = self._request(
                "PUT", "/api/map/settings", role=auth.ROLE_ADMIN, payload=payload
            )

        self.assertEqual(status, 409)
        self.assertEqual(data["code"], "force_switch_required")
        self.assertIn("无法确认旧底盘已停止", data["error"])

    def test_viewer_cannot_trigger_map_control_actions(self) -> None:
        control_routes = (
            ("/api/map/navigate", b'{"x": 1, "y": 2}'),
            ("/api/map/patrol", b'{"targets": [{"x": 1, "y": 2}], "speed_mps": 0.2}'),
            ("/api/map/actions/cancel", b"{}"),
            ("/api/map/gohome", b"{}"),
            ("/api/map/relocate", b"{}"),
            ("/api/map/pois", b'{"name": "p", "x": 1, "y": 2}'),
            ("/api/map/pois/delete", b'{"id": "p"}'),
        )
        for path, raw_body in control_routes:
            with self.subTest(path=path), patch.object(
                handlers.robot_map_api, "move_to"
            ) as move_to, patch.object(
                handlers.robot_map_api, "series_move_to"
            ) as series_move_to, patch.object(
                handlers.robot_map_api, "cancel_current_action"
            ) as cancel, patch.object(
                handlers.robot_map_api, "go_home"
            ) as go_home, patch.object(
                handlers.robot_map_api, "recover_localization"
            ) as relocate, patch.object(
                handlers.robot_map_api, "create_poi"
            ) as create_poi, patch.object(
                handlers.robot_map_api, "delete_poi"
            ) as delete_poi:
                handler, status, data = self._request(
                    "POST", path, role=auth.ROLE_VIEWER, raw_body=raw_body
                )
            self.assertEqual(status, 403)
            self.assertIn("管理员", data["error"])
            self.assertEqual(handler.rfile.read(), b"")
            move_to.assert_not_called()
            series_move_to.assert_not_called()
            cancel.assert_not_called()
            go_home.assert_not_called()
            relocate.assert_not_called()
            create_poi.assert_not_called()
            delete_poi.assert_not_called()

    def test_map_navigate_rejects_non_finite_or_unsafe_speed(self) -> None:
        invalid_bodies = (
            b'{"x": NaN, "y": 1}',
            b'{"x": 0, "y": Infinity}',
            b'{"x": 0, "y": 1, "speed_ratio": 1.1}',
            b'{"x": 0, "y": 1, "speed_ratio": -0.1}',
            b'{"x": 0, "y": 1, "speed_ratio": 0.05}',
            b'{"x": 0, "y": 1, "speed_mps": NaN}',
            b'{"x": 0, "y": 1, "speed_mps": 0}',
            b'{"x": 0, "y": 1, "speed_mps": -0.1}',
            b'{"x": 0, "y": 1, "speed_mps": 0.4, "speed_ratio": 0.5}',
        )
        for raw_body in invalid_bodies:
            with self.subTest(raw_body=raw_body), patch.object(
                handlers.robot_map_api, "move_to"
            ) as move_to:
                handler, status, data = self._request(
                    "POST",
                    "/api/map/navigate",
                    role=auth.ROLE_ADMIN,
                    raw_body=raw_body,
                )
            self.assertEqual(status, 400)
            self.assertTrue(data["error"])
            move_to.assert_not_called()
            self.assertEqual(handler.rfile.read(), b"")

    def test_map_navigate_forwards_real_speed_when_requested(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with patch.object(
            handlers.robot_map_api,
            "move_to",
            return_value={"action_id": 7},
        ) as move_to:
            _handler, status, data = self._request(
                "POST",
                "/api/map/navigate",
                role=auth.ROLE_ADMIN,
                payload={
                    "x": 1,
                    "y": 2,
                    "speed_mps": 0.4,
                    "expected_robot_base_url": base_url,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"action_id": 7})
        move_to.assert_called_once_with(
            1.0,
            2.0,
            yaw=None,
            precise=True,
            speed_ratio=0.8,
            expected_base_url=base_url,
            speed_mps=0.4,
        )

    def test_map_patrol_forwards_ordered_targets_and_real_speed(self) -> None:
        base_url = "http://192.168.5.9:1448"
        with patch.object(
            handlers.robot_map_api,
            "series_move_to",
            return_value={"action_id": 8},
        ) as series_move_to:
            _handler, status, data = self._request(
                "POST",
                "/api/map/patrol",
                role=auth.ROLE_ADMIN,
                payload={
                    "targets": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
                    "speed_mps": 0.4,
                    "expected_robot_base_url": base_url,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"action_id": 8})
        series_move_to.assert_called_once_with(
            [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}],
            speed_mps=0.4,
            expected_base_url=base_url,
        )

    def test_map_navigate_rejects_a_stale_robot_endpoint(self) -> None:
        payload = {
            "x": 1,
            "y": 2,
            "expected_robot_base_url": "http://192.168.5.10:1448",
        }
        with patch.object(
            handlers.robot_map_api,
            "move_to",
            side_effect=ValueError("底盘连接已变更，请刷新地图后重试。"),
        ) as move_to:
            handler, status, data = self._request(
                "POST",
                "/api/map/navigate",
                role=auth.ROLE_ADMIN,
                payload=payload,
            )

        self.assertEqual(status, 400)
        self.assertIn("底盘连接已变更", data["error"])
        move_to.assert_called_once_with(
            1.0,
            2.0,
            yaw=None,
            precise=True,
            speed_ratio=0.8,
            expected_base_url="http://192.168.5.10:1448",
        )
        self.assertEqual(handler.rfile.read(), b"")

    def test_map_navigate_requires_the_rendered_robot_endpoint(self) -> None:
        with patch.object(handlers.robot_map_api, "move_to") as move_to:
            handler, status, data = self._request(
                "POST",
                "/api/map/navigate",
                role=auth.ROLE_ADMIN,
                payload={"x": 1, "y": 2},
            )

        self.assertEqual(status, 400)
        self.assertIn("底盘连接信息已过期", data["error"])
        move_to.assert_not_called()
        self.assertEqual(handler.rfile.read(), b"")


@unittest.skipIf(QueryHandler is None, "HTTP handlers require Python 3.12 or older")
class LoadPathsRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        state_fields = (
            *handlers._LOAD_PATH_STATE_FIELDS,
            "configured_config_pnp",
            "configured_vfm_app",
            "_cli_config_paths",
            "_cli_knowledge_root",
            "_cli_knowledge_path",
        )
        self._old_state = {
            name: getattr(state, name) for name in state_fields
        }
        self.addCleanup(self._restore_state)

    def _restore_state(self) -> None:
        for name, value in self._old_state.items():
            setattr(state, name, value)

    def _request(
        self, payload: dict[str, object], path: str = "/load-paths"
    ) -> tuple[int, object]:
        raw = json.dumps(payload).encode("utf-8")
        handler = QueryHandler.__new__(QueryHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(raw)
        response: dict[str, object] = {}
        handler._send_json = lambda status, data: response.update(
            status=int(status), data=data
        )
        handler._send_redirect = lambda location: response.update(
            status=302, data={"location": location}
        )
        handler.send_error = lambda status, message="": response.update(
            status=int(status), data={"error": message}
        )
        admin = {"username": "admin", "display_name": "管理员", "role": auth.ROLE_ADMIN}
        with patch.object(auth, "session_from_cookie", return_value=admin):
            handler.do_POST()
        return int(response["status"]), response["data"]

    def _seed_state(self) -> dict[str, object]:
        markers = {
            "configured_knowledge": Path("/old/knowledge"),
            "configured_knowledge_root": None,
            "configured_shelves": Path("/old/shelves.csv"),
            "configured_unavailable": Path("/old/unavailable.json"),
            "configured_tool_mapping": Path("/old/tool.json"),
            "configured_pick_strategy": Path("/old/pick.json"),
            "_explicit_config_keys": frozenset({"old"}),
            "loaded_dataset": object(),
            "loaded_tool_mapping": {"old": "tool"},
            "loaded_closed_loop_ids": frozenset({"old"}),
            "loaded_unavailable_ids": frozenset({"old"}),
            "data_source_ready": True,
            "data_load_method": "paths",
            "edit_workspace": {"marker": "old"},
            "data_revision": 41,
        }
        for name, value in markers.items():
            setattr(state, name, value)
        return markers

    def _valid_paths(self) -> tuple[tempfile.TemporaryDirectory[str], dict[str, object]]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        knowledge = root / "knowledge"
        knowledge.mkdir()
        (knowledge / "SKU-1.json").write_text(
            json.dumps({"id": "SKU-1", "name": "test"}), encoding="utf-8"
        )
        shelves = root / "shelves.csv"
        shelves.write_text("商品编码,库位\nSKU-1,01-01-01\n", encoding="utf-8")
        return temp, {"knowledge": str(knowledge), "shelves": str(shelves)}

    def _assert_state_matches(self, markers: dict[str, object]) -> None:
        for name, expected in markers.items():
            actual = getattr(state, name)
            if name == "loaded_dataset":
                self.assertIs(actual, expected)
            else:
                self.assertEqual(actual, expected)

    def test_cli_knowledge_value_is_saved_for_one_click(self) -> None:
        from ksq import cli

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        knowledge = root / "knowledge"
        knowledge.mkdir()
        saved = {
            name: getattr(state, name)
            for name in (
                "configured_knowledge",
                "configured_knowledge_root",
                "configured_shelves",
                "configured_unavailable",
                "configured_tool_mapping",
                "configured_pick_strategy",
                "configured_config_pnp",
                "configured_vfm_app",
                "_cli_config_paths",
                "_cli_knowledge_root",
                "_cli_knowledge_path",
                "_explicit_config_keys",
            )
        }

        def restore_state() -> None:
            for name, value in saved.items():
                setattr(state, name, value)

        self.addCleanup(restore_state)

        class FakeServer:
            def __init__(self, *_args: object) -> None:
                pass

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        with (
            patch.object(cli, "ThreadingHTTPServer", FakeServer),
            patch.object(
                cli,
                "configure_runtime_logging",
                return_value=root / "app.log",
            ),
            patch.object(cli, "reset_state_if_version_changed"),
            patch.object(state, "reload_config_pnp_paths"),
            patch.object(cli.dashboard_api, "start_dashboard_monitor") as start_monitor,
            patch.object(cli.dashboard_api, "stop_dashboard_monitor") as stop_monitor,
        ):
            cli.serve(
                [
                    "--knowledge-root",
                    str(root),
                    "--config-pnp",
                    str(root),
                ]
            )

        start_monitor.assert_called_once_with()
        stop_monitor.assert_called_once_with()

        self.assertEqual(state.configured_knowledge_root, root.resolve())
        self.assertEqual(state.configured_knowledge, knowledge.resolve())
        self.assertEqual(
            state._cli_config_paths,
            {},
        )
        self.assertEqual(state._cli_knowledge_root, root.resolve())
        self.assertEqual(state._cli_knowledge_path, knowledge.resolve())
        self.assertEqual(
            state._explicit_config_keys,
            frozenset(),
        )

    def test_cli_relative_knowledge_cannot_escape_root(self) -> None:
        from ksq import cli

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "templates"
        root.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "Knowledge|knowledge|超出"):
            cli.serve(
                [
                    "--knowledge-root",
                    str(root),
                    "--knowledge",
                    "../outside",
                    "--config-pnp",
                    str(root),
                ]
            )

    def test_load_failure_restores_all_state_fields(self) -> None:
        markers = self._seed_state()
        temp, payload = self._valid_paths()
        self.addCleanup(temp.cleanup)
        with patch.object(
            handlers, "load_from_configured_paths", side_effect=ValueError("bad data")
        ):
            status, data = self._request(payload)
        self.assertEqual(status, 400)
        self.assertIn("bad data", data["error"])
        self._assert_state_matches(markers)

    def test_relative_paths_resolve_from_current_directories(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        knowledge_root = root / "templates"
        knowledge = (
            knowledge_root / "pnp_percept/templates_260827/knowledge"
        )
        knowledge.mkdir(parents=True)
        config_pnp = root / "config_pnp"
        config_pnp.mkdir()
        paths = {
            "shelves": config_pnp / "shelves.csv",
            "unavailable": config_pnp / "unavailable.json",
            "tool_mapping": config_pnp / "mapping.json",
            "pick_strategy": config_pnp / "pick.json",
        }
        for path in paths.values():
            path.touch()
        state.configured_knowledge_root = knowledge_root
        state.configured_vfm_app = None
        state.configured_config_pnp = config_pnp

        with (
            patch.object(
                handlers,
                "load_from_configured_paths",
                return_value=(object(), None, None, [], 0.01),
            ),
            patch.object(edit_workspace, "init_workspace_from_loaded"),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            for knowledge_value in (
                "pnp_percept/templates_260827/knowledge",
                "templates/pnp_percept/templates_260827/knowledge",
            ):
                with self.subTest(knowledge=knowledge_value):
                    status, _data = self._request(
                        {
                            "knowledge": knowledge_value,
                            "shelves": "shelves.csv",
                            "unavailable": "unavailable.json",
                            "tool_mapping": "mapping.json",
                            "pick_strategy": "pick.json",
                        }
                    )
                    self.assertEqual(status, 200)

        self.assertEqual(state.configured_knowledge, knowledge.resolve())
        self.assertEqual(
            state.configured_knowledge_root, knowledge_root.resolve()
        )
        self.assertEqual(state.configured_shelves, paths["shelves"].resolve())
        self.assertEqual(
            state.configured_unavailable, paths["unavailable"].resolve()
        )
        self.assertEqual(
            state.configured_tool_mapping, paths["tool_mapping"].resolve()
        )
        self.assertEqual(
            state.configured_pick_strategy, paths["pick_strategy"].resolve()
        )

    def test_relative_knowledge_cannot_escape_current_directory(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        knowledge_base = root / "templates"
        knowledge_base.mkdir(parents=True)
        outside = root / "outside"
        outside.mkdir()
        (knowledge_base / "escape").symlink_to(
            outside, target_is_directory=True
        )
        state.configured_knowledge_root = knowledge_base
        state.configured_vfm_app = None

        for raw_path in ("../outside", "escape"):
            with self.subTest(raw_path=raw_path):
                status, data = self._request(
                    {
                        "knowledge": raw_path,
                        "shelves": "/data/config_pnp/shelves.csv",
                    }
                )
                self.assertEqual(status, 400)
                self.assertIn("不能超出当前目录", data["error"])

    def test_scene_directory_input_uses_knowledge_child(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "templates"
        scene = root / "pnp_percept/templates_260827"
        knowledge = scene / "knowledge"
        knowledge.mkdir(parents=True)
        config_pnp = Path(temporary.name) / "config_pnp"
        config_pnp.mkdir()
        shelves = config_pnp / "shelves.csv"
        shelves.touch()
        state.configured_knowledge_root = root
        state.configured_vfm_app = None
        state.configured_config_pnp = config_pnp

        with (
            patch.object(
                handlers,
                "load_from_configured_paths",
                return_value=(object(), None, None, [], 0.01),
            ),
            patch.object(edit_workspace, "init_workspace_from_loaded"),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            status, _data = self._request(
                {
                    "knowledge": "templates/pnp_percept/templates_260827",
                    "shelves": "shelves.csv",
                }
            )

        self.assertEqual(status, 200)
        self.assertEqual(state.configured_knowledge, knowledge.resolve())

    def test_scene_knowledge_symlink_cannot_escape_root(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "templates"
        scene = root / "pnp_percept/templates_260827"
        scene.mkdir(parents=True)
        outside = Path(temporary.name) / "outside-knowledge"
        outside.mkdir()
        (scene / "knowledge").symlink_to(outside, target_is_directory=True)
        state.configured_knowledge_root = root
        state.configured_vfm_app = None
        state.configured_config_pnp = root

        status, data = self._request(
            {
                "knowledge": "pnp_percept/templates_260827",
                "shelves": "shelves.csv",
            }
        )

        self.assertEqual(status, 400)
        self.assertIn("不能超出当前目录", data["error"])

    def test_mounted_knowledge_root_resolves_default_child(self) -> None:
        temporary, paths = self._valid_paths()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        knowledge = Path(str(paths["knowledge"]))
        state.configured_knowledge_root = root
        state.configured_knowledge = knowledge
        state.configured_config_pnp = root
        state.configured_vfm_app = None
        state._cli_config_paths = {}
        state._cli_knowledge_root = root
        state._cli_knowledge_path = knowledge
        state._explicit_config_keys = frozenset()

        with (
            patch.object(
                handlers,
                "load_from_configured_paths",
                return_value=(object(), None, None, [], 0.01),
            ),
            patch.object(edit_workspace, "init_workspace_from_loaded"),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            status, _data = self._request(
                {"knowledge": "knowledge", "shelves": "shelves.csv"}
            )

        self.assertEqual(status, 200)
        self.assertEqual(state.configured_knowledge, knowledge.resolve())
        self.assertEqual(state.configured_knowledge_root, root.resolve())

        status, data = self._request(
            {"knowledge": "../outside", "shelves": "shelves.csv"}
        )
        self.assertEqual(status, 400)
        self.assertIn("不能超出当前目录", data["error"])

    def test_init_failure_after_load_also_restores_state(self) -> None:
        markers = self._seed_state()
        temp, payload = self._valid_paths()
        self.addCleanup(temp.cleanup)
        loaded = object()
        with (
            patch.object(
                handlers,
                "load_from_configured_paths",
                return_value=(loaded, None, None, [], 0.01),
            ),
            patch.object(
                edit_workspace,
                "init_workspace_from_loaded",
                side_effect=ValueError("workspace failed"),
            ),
        ):
            status, data = self._request(payload)
        self.assertEqual(status, 400)
        self.assertIn("workspace failed", data["error"])
        self._assert_state_matches(markers)

    def test_auto_load_failure_restores_state(self) -> None:
        markers = self._seed_state()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        (Path(config_dir.name) / "config.py").write_text("# test\n", encoding="utf-8")
        old_config_dir = state.configured_config_pnp
        state.configured_config_pnp = Path(config_dir.name)
        self.addCleanup(setattr, state, "configured_config_pnp", old_config_dir)
        with (
            patch.object(state, "reload_config_pnp_paths"),
            patch.object(
                handlers,
                "apply_configured_paths_reload",
                side_effect=ValueError("auto load failed"),
            ) as reload_paths,
        ):
            status, data = self._request({}, "/load-auto")
        self.assertEqual(status, 400)
        self.assertIn("auto load failed", data["error"])
        reload_paths.assert_called_once_with()
        self._assert_state_matches(markers)

    def test_auto_load_returns_final_loaded_paths(self) -> None:
        self._seed_state()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        knowledge_root = root / "templates"
        final_knowledge = knowledge_root / "knowledge"
        nested_knowledge = (
            knowledge_root / "pnp_percept/templates_260827/knowledge"
        )
        final_knowledge.mkdir(parents=True)
        nested_knowledge.mkdir(parents=True)
        config_pnp = root / "config_pnp"
        config_pnp.mkdir()
        (config_pnp / "config.py").write_text("# test\n", encoding="utf-8")
        final_shelves = config_pnp / "etm_sku_locations_cache.csv"
        old_config_dir = state.configured_config_pnp
        state.configured_config_pnp = config_pnp
        state.configured_knowledge_root = knowledge_root
        state.configured_vfm_app = root / "missing-vfm"
        state._cli_knowledge_root = knowledge_root
        state._cli_knowledge_path = final_knowledge
        self.addCleanup(setattr, state, "configured_config_pnp", old_config_dir)

        def apply_reload() -> dict[str, object]:
            state.configured_knowledge = final_knowledge
            state.configured_shelves = final_shelves
            state.configured_unavailable = None
            state.configured_tool_mapping = None
            state.configured_pick_strategy = None
            state.loaded_dataset = object()
            return {"elapsed_seconds": 0.01, "unavailable_ids": []}

        with (
            patch.object(
                handlers,
                "apply_configured_paths_reload",
                side_effect=apply_reload,
            ),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            status, data = self._request({}, "/load-auto")

        self.assertEqual(status, 200)
        self.assertEqual(
            data["paths"]["knowledge"],
            "knowledge",
        )
        self.assertEqual(
            data["paths"]["shelves"], "etm_sku_locations_cache.csv"
        )
        for key in ("unavailable", "tool_mapping", "pick_strategy"):
            self.assertEqual(data["paths"][key], "")

    def test_root_only_auto_load_restores_default_target(self) -> None:
        self._seed_state()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "templates"
        default_knowledge = root / "knowledge"
        nested_knowledge = root / "pnp_percept/templates_260827/knowledge"
        default_knowledge.mkdir(parents=True)
        nested_knowledge.mkdir(parents=True)
        config_pnp = Path(temporary.name) / "config_pnp"
        config_pnp.mkdir()
        (config_pnp / "config.py").write_text("# test\n", encoding="utf-8")

        state.configured_knowledge_root = root
        state.configured_knowledge = nested_knowledge
        state.configured_vfm_app = Path(temporary.name) / "missing-vfm"
        state.configured_config_pnp = config_pnp
        state._cli_knowledge_root = root
        state._cli_knowledge_path = default_knowledge

        seen: dict[str, Path] = {}

        def apply_reload() -> dict[str, object]:
            seen["root"] = state.configured_knowledge_root
            seen["knowledge"] = state.configured_knowledge
            state.loaded_dataset = object()
            return {"elapsed_seconds": 0.01, "unavailable_ids": []}

        with (
            patch.object(
                handlers,
                "apply_configured_paths_reload",
                side_effect=apply_reload,
            ),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            status, data = self._request({}, "/load-auto")

        self.assertEqual(status, 200)
        self.assertEqual(seen["root"], root.resolve())
        self.assertEqual(seen["knowledge"], default_knowledge.resolve())
        self.assertEqual(data["paths"]["knowledge"], "knowledge")

    def test_auto_load_can_clear_stale_optional_path_inputs(self) -> None:
        source = (pages.STATIC_DIRECTORY / "load.js").read_text(encoding="utf-8")
        helper = source.split("function setPathValue", 1)[1].split("\n  }", 1)[0]
        self.assertIn('if (typeof value !== "string") return;', helper)
        self.assertIn("el.value = value;", helper)
        self.assertNotIn("value &&", helper)

    def test_load_page_hides_mounted_path_prefixes(self) -> None:
        root = Path("/data/knowledge")
        state.configured_knowledge_root = root
        state.configured_vfm_app = None
        state.configured_config_pnp = Path("/data/config_pnp")
        state.configured_knowledge = root / "knowledge"
        state.configured_shelves = Path("/data/config_pnp/shelves.csv")
        state.configured_unavailable = Path("/data/config_pnp/unavailable.json")
        state.configured_tool_mapping = Path("/data/config_pnp/mapping.json")
        state.configured_pick_strategy = Path("/data/config_pnp/pick.json")

        content = pages.shell_page_html()
        self.assertIn('value="knowledge"', content)

        state.configured_knowledge = root / "pnp_percept/templates_260827/knowledge"
        content = pages.shell_page_html()
        self.assertIn(
            'value="pnp_percept/templates_260827/knowledge"', content
        )
        for value in (
            "shelves.csv",
            "unavailable.json",
            "mapping.json",
            "pick.json",
        ):
            self.assertIn(f'value="{value}"', content)

    def test_auto_load_preserves_cli_knowledge_without_vfm_config(self) -> None:
        self._seed_state()
        old_cli_config_paths = state._cli_config_paths
        self.addCleanup(
            setattr,
            state,
            "_cli_config_paths",
            old_cli_config_paths,
        )
        temporary, paths = self._valid_paths()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config.py").write_text("# test\n", encoding="utf-8")
        state.configured_config_pnp = root
        state.configured_vfm_app = None
        cli_knowledge = Path(str(paths["knowledge"]))
        manual_knowledge = root / "manual-knowledge"
        manual_knowledge.mkdir()
        state.configured_knowledge = manual_knowledge
        state.configured_shelves = Path(str(paths["shelves"]))
        state.configured_unavailable = None
        state.configured_tool_mapping = None
        state.configured_pick_strategy = None
        state._cli_config_paths = {"knowledge": cli_knowledge}
        state._explicit_config_keys = frozenset({"knowledge", "shelves"})

        def apply_reload() -> dict[str, object]:
            state.reload_config_pnp_paths()
            state.loaded_dataset = object()
            return {"elapsed_seconds": 0.01, "unavailable_ids": []}

        with (
            patch.object(
                handlers,
                "apply_configured_paths_reload",
                side_effect=apply_reload,
            ),
            patch.object(handlers, "format_status_html", return_value="loaded"),
            patch.object(handlers, "build_missing_rows", return_value=[]),
        ):
            status, data = self._request({}, "/load-auto")

        self.assertEqual(status, 200)
        self.assertEqual(data["paths"]["knowledge"], ".")
        self.assertEqual(
            state._explicit_config_keys,
            frozenset({"knowledge"}),
        )
