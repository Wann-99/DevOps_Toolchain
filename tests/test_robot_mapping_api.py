"""Mapping controls are tested only against an in-memory chassis."""

from __future__ import annotations

import base64
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ksq.web import robot_map_api as robot
from ksq.web import robot_mapping_api as mapping

_REAL_STCM_REQUEST = mapping._stcm_request


def _http_response(raw: bytes) -> http.client.HTTPResponse:
    class Socket:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(raw)

    response = http.client.HTTPResponse(Socket())
    response.begin()
    return response


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Path(directory.name) / "robot_map_settings.json"
        self.base = "http://192.168.5.9:1448"
        self.other = "http://192.168.5.10:1448"
        self.settings.write_text(json.dumps({"robot_base_url": self.base}), encoding="utf-8")
        self.mapping_enabled = False
        self.loop_enabled = True
        self.floors = [{"floor": "1F"}]
        self.calls = []
        self.raw_calls = []
        self.raw = b"example-stcm-from-firmware"
        self.factories = []
        self.speeds = {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5}
        mapping._KNOWN_MAPPING.clear()
        mapping._DRIVE_LEASES.clear()
        mapping._TELEOP_CAPABILITIES.clear()
        self.addCleanup(mapping._KNOWN_MAPPING.clear)
        self.addCleanup(mapping._DRIVE_LEASES.clear)
        self.addCleanup(mapping._TELEOP_CAPABILITIES.clear)
        for target, attribute, options in (
            (robot, "ROBOT_MAP_SETTINGS_FILE", {"new": self.settings}),
            (robot, "_request", {"side_effect": self.request}),
            (robot, "_invalidate_telemetry_cache", {}),
            (mapping, "_stcm_request", {"side_effect": self.stcm}),
        ):
            patcher = patch.object(target, attribute, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        if path in {mapping._MAPPING_PATH, mapping._LOOP_PATH}:
            attribute = "mapping_enabled" if path == mapping._MAPPING_PATH else "loop_enabled"
            if method == "PUT":
                setattr(self, attribute, payload["enable"])
                return 200, True
            return 200, getattr(self, attribute)
        if path == "/api/core/motion/v1/actions/:current" and method == "GET":
            return 200, {"state": {"status": 4}}
        if path == "/api/multi-floor/map/v1/floors":
            return 200, self.floors
        if path == "/api/core/motion/v1/action-factories":
            return 200, self.factories
        if path.startswith("/api/core/system/v1/parameter?param="):
            return 200, str(self.speeds[path.partition("?param=")[2]])
        if path == "/api/core/system/v1/parameter" and method == "PUT":
            self.speeds[payload["param"]] = float(payload["value"])
            return 200, True
        if path == "/api/core/motion/v1/actions" and method == "POST":
            return 200, {"action_id": 7, "state": {"status": 1}}
        return 200, {}

    def stcm(self, method, base_url, payload=None):
        self.raw_calls.append((method, base_url, payload))
        if method == "PUT":
            self.raw = payload
            return b""
        return self.raw

    def execute(self, command, **payload):
        return mapping.execute({"expected_robot_base_url": self.base, "command": command, **payload})

    def prepare_drive(self):
        self.mapping_enabled = True
        self.factories = [{"action_name": mapping._MOVE_ACTION}]
        mapping._TELEOP_CAPABILITIES.clear()
        return self.execute("drive-start", linear_speed=0.2, angular_speed=0.3)["drive_token"]

    def test_drive_prepares_limits_without_moving_and_pulses_without_local_reads(self):
        token = self.prepare_drive()
        self.assertEqual(self.speeds, {"base.max_moving_speed": 0.2, "base.max_angular_speed": 0.3})
        self.assertEqual(mapping._load_state(self.base)["drive_speed_restore"],
                         {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5})
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))
        self.calls.clear()
        with (
            patch.object(mapping, "_load_state", side_effect=AssertionError("pulse read state")),
            patch.object(mapping, "_read_flag", side_effect=AssertionError("pulse read flag")),
            patch.object(robot, "require_current_base_url", side_effect=AssertionError("pulse read settings")),
        ):
            for direction in ("forward", "backward", "left", "right"):
                result = self.execute("move", drive_token=token, direction=direction)
                self.assertEqual(result["action"]["action_id"], 7)
        self.assertEqual([call[2]["options"] for call in self.calls], [
            {"direction": direction, "duration": 200} for direction in (0, 1, 3, 2)
        ])
        self.assertTrue(all(call[3]["timeout"] == 0.3 for call in self.calls))
        with patch.object(robot, "_request", side_effect=AssertionError("active status polled firmware")):
            status = mapping.get_status(self.base)
        self.assertTrue(status["status_cached"])
        self.assertTrue(status["drive_active"])
        self.assertTrue(status["teleop_supported"])
        self.assertTrue(status["teleop_restore_pending"])
        self.assertTrue(self.execute("drive-stop", drive_token=token)["stopped"])
        self.assertEqual(self.speeds, {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5})
        self.assertNotIn("drive_speed_restore", mapping._load_state(self.base))

    def test_drive_tokens_expire_and_old_owners_cannot_stop_new_control(self):
        token = self.prepare_drive()
        self.calls.clear()
        for command in ("move", "drive-stop"):
            with self.assertRaises(robot.RobotApiError):
                self.execute(command, drive_token="wrong", direction="forward")
        self.assertEqual(self.calls, [])
        mapping._DRIVE_LEASES[self.base]["expires"] = 0
        with self.assertRaisesRegex(robot.RobotApiError, "过期"):
            self.execute("move", drive_token=token, direction="forward")
        self.assertEqual(self.calls, [])
        self.execute("drive-stop", drive_token=token)
        new_token = self.prepare_drive()
        self.calls.clear()
        with self.assertRaises(robot.RobotApiError):
            self.execute("drive-stop", drive_token=token)
        self.assertEqual(self.calls, [])
        self.execute("move", drive_token=new_token, direction="forward")

    def test_drive_uses_the_action_name_reported_by_the_chassis(self):
        for action_name in ("agent.actions.MoveByAction", mapping._MOVE_ACTION):
            with self.subTest(action_name=action_name):
                self.mapping_enabled = True
                self.factories = [{"action_name": action_name}]
                mapping._TELEOP_CAPABILITIES.clear()
                status = mapping.get_status(self.base)
                self.assertTrue(status["teleop_supported"])
                token = self.execute("drive-start", linear_speed=0.2, angular_speed=0.3)["drive_token"]
                self.calls.clear()
                self.execute("move", drive_token=token, direction="forward")
                self.assertEqual(self.calls[0][2]["action_name"], action_name)
                self.execute("drive-stop", drive_token=token)

    def test_manual_drive_without_mapping_does_not_change_mapping_mode(self):
        self.factories = [{"action_name": mapping._MOVE_ACTION}]
        for phase in ("idle", "paused", "finished", "saved"):
            with self.subTest(phase=phase):
                state = mapping._load_state(self.base)
                state["phase"] = phase
                mapping._save_state(self.base, state)
                self.calls.clear()
                token = self.execute("drive-start", linear_speed=0.2, angular_speed=0.3)["drive_token"]
                with self.assertRaises(robot.RobotApiError):
                    mapping.require_navigation_allowed(self.base)
                self.execute("move", drive_token=token, direction="forward")
                self.execute("drive-stop", drive_token=token)
                self.assertFalse(self.mapping_enabled)
                self.assertFalse(any(method == "PUT" and path == mapping._MAPPING_PATH for method, path, *_ in self.calls))
                self.assertEqual(mapping._load_state(self.base)["phase"], phase)

    def test_general_stop_restores_idle_manual_control_and_unlocks_navigation(self):
        self.factories = [{"action_name": mapping._MOVE_ACTION}]
        token = self.execute("drive-start", linear_speed=0.2, angular_speed=0.3)["drive_token"]
        self.execute("move", drive_token=token, direction="forward")
        robot.cancel_current_action(expected_base_url=self.base)
        self.assertNotIn("drive_speed_restore", mapping._load_state(self.base))
        self.assertEqual(self.speeds, {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5})
        mapping.require_navigation_allowed(self.base)
        with self.assertRaises(robot.RobotApiError):
            self.execute("move", drive_token=token, direction="forward")

    def test_speed_readback_not_response_shape_confirms_the_applied_limit(self):
        original = self.request
        for acknowledgement in (True, False, "true", {}, None):
            def respond(method, path, payload=None, **kwargs):
                result = original(method, path, payload, **kwargs)
                return (200, acknowledgement) if method == "PUT" and path.endswith("/parameter") else result

            with self.subTest(acknowledgement=acknowledgement), patch.object(robot, "_request", side_effect=respond):
                token = self.prepare_drive()
                self.assertEqual(self.speeds["base.max_moving_speed"], 0.2)
                self.execute("drive-stop", drive_token=token)
                self.assertEqual(self.speeds["base.max_moving_speed"], 0.8)

    def test_speed_success_reply_without_applied_limit_still_blocks_motion(self):
        original = self.request

        def unchanged(method, path, payload=None, **kwargs):
            if method == "PUT" and path.endswith("/parameter"):
                return 200, True
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=unchanged), patch.object(mapping.time, "sleep") as wait:
            with self.assertRaisesRegex(robot.RobotApiError, "请求 0.2 m/s，读回 0.8 m/s"):
                self.prepare_drive()
            self.assertEqual(wait.call_count, 2)
        self.assertNotIn(self.base, mapping._DRIVE_LEASES)
        self.assertNotIn("drive_speed_restore", mapping._load_state(self.base))
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))

    def test_already_restored_limits_do_not_require_another_write(self):
        state = mapping._load_state(self.base)
        state["drive_speed_restore"] = dict(self.speeds)
        mapping._save_state(self.base, state)
        self.assertTrue(mapping.restore_drive_speeds(self.base))
        self.assertNotIn("drive_speed_restore", mapping._load_state(self.base))
        self.assertFalse(any(method == "PUT" for method, *_ in self.calls))

    def test_speed_readback_can_settle_within_a_bounded_retry(self):
        readings = iter((0.8, 0.8, 0.2))
        with patch.object(mapping, "_read_speed", side_effect=lambda *_: next(readings)), patch.object(mapping.time, "sleep") as wait:
            mapping._set_speed("base.max_moving_speed", 0.2, self.base)
            wait.assert_called_once_with(0.1)

    def test_restart_keeps_restore_record_and_finish_can_recover(self):
        token = self.prepare_drive()
        mapping._DRIVE_LEASES.clear()
        mapping._KNOWN_MAPPING.clear()
        with self.assertRaises(robot.RobotApiError):
            self.execute("move", drive_token=token, direction="forward")
        self.mapping_enabled = False
        self.assertTrue(mapping.get_status(self.base)["teleop_restore_pending"])
        for command in ("start", "resume", "upload", "new", "clear", "import", "restore"):
            with self.subTest(command=command), self.assertRaises(robot.RobotApiError):
                self.execute(command, confirm=True)
        with self.assertRaisesRegex(robot.RobotApiError, "原速度"):
            mapping.require_navigation_allowed(self.base)
        result = self.execute("finish")
        self.assertFalse(result["teleop_restore_pending"])
        mapping.require_navigation_allowed(self.base)

    def test_speed_setup_failure_restores_both_originals_without_creating_lease(self):
        original = self.request

        def reject_angular(method, path, payload=None, **kwargs):
            if method == "PUT" and payload.get("param") == "base.max_angular_speed" and float(payload["value"]) == 0.3:
                return 200, False
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=reject_angular):
            with self.assertRaises(robot.RobotApiError):
                self.prepare_drive()
        self.assertEqual(self.speeds, {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5})
        self.assertNotIn(self.base, mapping._DRIVE_LEASES)
        self.assertNotIn("drive_speed_restore", mapping._load_state(self.base))
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))

    def test_restore_failure_stops_motion_but_blocks_navigation_until_retry(self):
        token = self.prepare_drive()
        original = self.request

        def reject_original_linear(method, path, payload=None, **kwargs):
            if method == "PUT" and payload.get("param") == "base.max_moving_speed" and float(payload["value"]) == 0.8:
                raise robot.RobotApiError("restore failed")
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=reject_original_linear):
            with self.assertRaisesRegex(robot.RobotApiError, "恢复未确认"):
                self.execute("drive-stop", drive_token=token)
        self.assertNotIn(self.base, mapping._DRIVE_LEASES)
        self.assertTrue(mapping.get_status(self.base)["teleop_restore_pending"])
        self.assertEqual(self.speeds["base.max_angular_speed"], 1.5)
        with self.assertRaisesRegex(robot.RobotApiError, "原速度"):
            mapping.require_navigation_allowed(self.base)
        result = self.execute("stop")
        self.assertTrue(result["stopped"])
        self.assertFalse(result["teleop_restore_pending"])
        self.assertEqual(self.speeds["base.max_moving_speed"], 0.8)

    def test_pulse_timeout_expires_lease_and_accepts_owned_cleanup(self):
        token = self.prepare_drive()
        with patch.object(robot, "_request", side_effect=robot.RobotApiError("timeout", 504)):
            with self.assertRaises(robot.RobotApiError):
                self.execute("move", drive_token=token, direction="forward")
        with self.assertRaisesRegex(robot.RobotApiError, "过期"):
            self.execute("move", drive_token=token, direction="forward")
        self.assertTrue(self.execute("drive-stop", drive_token=token)["stopped"])

    def test_invalid_move_acknowledgement_expires_control(self):
        for action in ({}, {"action_id": True}, {"action_id": "7"}):
            token = self.prepare_drive()
            with self.subTest(action=action), patch.object(robot, "_request", return_value=(200, action)):
                with self.assertRaisesRegex(robot.RobotApiError, "有效遥控动作"):
                    self.execute("move", drive_token=token, direction="forward")
            self.assertEqual(mapping._DRIVE_LEASES[self.base]["expires"], 0)
            self.execute("drive-stop", drive_token=token)

    def test_pause_and_finish_only_cancel_valid_mapping_or_owned_control(self):
        for command in ("pause", "finish"):
            with self.assertRaises(robot.RobotApiError):
                self.execute(command)
        self.assertFalse(any(method == "DELETE" for method, *_ in self.calls))
        token = self.prepare_drive()
        result = self.execute("pause")
        self.assertEqual(result["phase"], "paused")
        self.assertFalse(result["teleop_restore_pending"])
        self.assertEqual(self.speeds["base.max_moving_speed"], 0.8)
        with self.assertRaises(robot.RobotApiError):
            self.execute("move", drive_token=token, direction="forward")

    def test_public_stop_and_connection_switch_revoke_lease(self):
        token = self.prepare_drive()
        robot.cancel_current_action(expected_base_url=self.base)
        with self.assertRaises(robot.RobotApiError):
            self.execute("move", drive_token=token, direction="forward")
        self.execute("stop")
        token = self.prepare_drive()
        robot.save_settings({"robot_base_url": self.other, "expected_robot_base_url": self.base})
        self.assertNotIn(self.base, mapping._DRIVE_LEASES)
        self.assertEqual(self.speeds, {"base.max_moving_speed": 0.8, "base.max_angular_speed": 1.5})
        with self.assertRaises(robot.RobotApiError):
            self.execute("move", drive_token=token, direction="forward")

    def test_drive_rejects_unsupported_invalid_or_increased_speed_before_writes(self):
        self.mapping_enabled = True
        with self.assertRaises(robot.RobotApiError):
            self.execute("drive-start", linear_speed=0.2, angular_speed=0.3)
        for linear, angular in ((0, 0.3), (0.41, 0.3), (0.2, 0.61), (float("nan"), 0.3)):
            with self.assertRaises(ValueError):
                self.execute("drive-start", linear_speed=linear, angular_speed=angular)
        self.speeds["base.max_moving_speed"] = 0.1
        with self.assertRaisesRegex(ValueError, "原有速度"):
            self.prepare_drive()
        self.assertFalse(any(method in {"POST", "PUT"} for method, *_ in self.calls))

    def test_live_mapping_lifecycle_and_shared_navigation_guard(self):
        robot._create_action("GoHomeAction", {}, base_url=self.base)
        self.assertEqual(self.execute("start")["phase"], "active")
        self.assertTrue(self.mapping_enabled)
        with self.assertRaisesRegex(robot.RobotApiError, "建图会话"):
            robot._create_action("GoHomeAction", {}, base_url=self.base)
        with self.assertRaisesRegex(robot.RobotApiError, "建图会话"):
            robot._require_patrol_idle(self.base)
        self.assertEqual(self.execute("pause")["phase"], "paused")
        self.assertFalse(self.mapping_enabled)
        with self.assertRaises(robot.RobotApiError):
            self.execute("new", confirm=True)
        self.assertEqual(self.execute("resume")["phase"], "active")
        self.assertEqual(self.execute("finish")["phase"], "finished")
        robot._create_action("GoHomeAction", {}, base_url=self.base)

    def test_failed_or_uncertain_switch_never_unlocks_navigation(self):
        original = self.request

        def timeout(method, path, payload=None, **kwargs):
            result = original(method, path, payload, **kwargs)
            if method == "PUT" and path == mapping._MAPPING_PATH:
                raise robot.RobotApiError("request timed out", 504)
            return result

        with patch.object(robot, "_request", side_effect=timeout):
            with self.assertRaises(robot.RobotApiError):
                self.execute("start")
        self.assertEqual(mapping._load_state(self.base)["phase"], "uncertain")
        with self.assertRaises(robot.RobotApiError):
            robot._create_action("MoveToAction", {}, base_url=self.base)
        self.assertEqual(mapping.get_status(self.base)["phase"], "active")
        self.assertEqual(self.execute("finish")["phase"], "finished")

    def test_false_put_acknowledgement_is_not_success(self):
        original = self.request

        def reject(method, path, payload=None, **kwargs):
            if method == "PUT":
                return 200, False
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=reject):
            with self.assertRaisesRegex(robot.RobotApiError, "未确认"):
                self.execute("start")
        self.assertEqual(mapping._load_state(self.base)["phase"], "uncertain")
        self.assertEqual(mapping.get_status(self.base)["phase"], "finished")
        mapping.require_navigation_allowed(self.base)

    def test_start_refuses_active_or_unreadable_robot_action(self):
        original = self.request

        def active(method, path, payload=None, **kwargs):
            if path.endswith("actions/:current"):
                return 200, {"state": {"status": 1}}
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=active):
            with self.assertRaisesRegex(robot.RobotApiError, "已有动作"):
                self.execute("start")
        self.assertFalse(self.mapping_enabled)
        self.assertFalse(any(method == "PUT" for method, *_ in self.calls))

    def test_unsupported_loop_closure_does_not_hide_mapping_state(self):
        original = self.request

        def unsupported(method, path, payload=None, **kwargs):
            if path == mapping._LOOP_PATH:
                raise robot.RobotApiError("not supported", 404)
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=unsupported):
            state = mapping.get_status(self.base)
        self.assertFalse(state["mapping_enabled"])
        self.assertIsNone(state["loop_closure_enabled"])
        self.assertIn("loop_closure", state["capability_errors"])
        self.assertFalse(state["teleop_supported"])
        with self.assertRaisesRegex(robot.RobotApiError, "遥控令牌"):
            self.execute("move", linear=0.2, angular=0, duration=200)
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))

    def test_confirmation_and_endpoint_are_required_before_any_firmware_write(self):
        for command in ("new", "clear", "import", "restore", "delete-backup", "upload", "delete-object"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.execute(command)
        for payload in (
            {"command": "start"},
            {"command": "start", "expected_robot_base_url": self.other},
            {"command": [], "expected_robot_base_url": self.base},
        ):
            with self.assertRaises(ValueError):
                mapping.execute(payload)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.raw_calls, [])

    def test_failed_automatic_backup_prevents_clear(self):
        with patch.object(mapping, "_stcm_request", side_effect=robot.RobotApiError("no map")):
            with self.assertRaises(robot.RobotApiError):
                self.execute("clear", confirm=True)
        self.assertFalse(any(method == "DELETE" for method, *_ in self.calls))

    def test_incomplete_backup_prevents_clear(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort-map",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nshort-map",
        ):
            with (
                self.subTest(response=response),
                patch.object(mapping, "_stcm_request", side_effect=_REAL_STCM_REQUEST),
                patch.object(mapping.urllib.request, "urlopen", return_value=_http_response(response)),
            ):
                with self.assertRaisesRegex(robot.RobotApiError, "不完整"):
                    self.execute("clear", confirm=True)
            self.assertFalse(any(method == "DELETE" for method, *_ in self.calls))
            self.assertEqual(mapping._list_backups(self.base), [])

    def test_stop_is_sent_before_reading_corrupt_state(self):
        state_path = mapping._directory(self.base) / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{invalid", encoding="utf-8")
        result = self.execute("stop")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["phase"], "unavailable")
        self.assertIn("status", result["capability_errors"])
        self.assertEqual(self.calls[0][:2], ("DELETE", "/api/core/motion/v1/actions/:current"))

    def test_status_failure_cannot_hide_stop_success_but_cancel_failure_can(self):
        with patch.object(mapping, "_status", side_effect=OSError("state unreadable")):
            result = self.execute("stop")
        self.assertTrue(result["stopped"])
        with patch.object(robot, "_cancel_current_action_for", side_effect=robot.RobotApiError("stop failed")):
            with self.assertRaisesRegex(robot.RobotApiError, "stop failed"):
                self.execute("stop")

    def test_backup_restore_and_export_preserve_exact_bytes(self):
        original = self.raw
        backup = self.execute("backup", name="First map")["backups"][0]
        self.raw = b"replacement-runtime-map"
        result = self.execute("restore", backup_id=backup["id"], confirm=True)
        self.assertEqual(self.raw, original)
        self.assertEqual(mapping.export_map(self.base), original)
        self.assertEqual(result["name"], "First map")
        self.assertTrue(result["dirty"])
        self.assertEqual(len(result["backups"]), 2)
        self.assertFalse(any(path.endswith("stcm/:save") for _, path, *_ in self.calls))

    def test_backups_are_isolated_and_identifier_cannot_escape_directory(self):
        identifier = self.execute("backup")["backups"][0]["id"]
        with self.assertRaises(ValueError):
            mapping._read_backup(self.other, identifier)
        for identifier in ("../state", "/tmp/map", "a" * 32 + "/..", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.execute("restore", backup_id=identifier, confirm=True)
        self.assertEqual([method for method, *_ in self.raw_calls], ["GET"])

    def test_corrupt_backup_fails_before_replacing_current_map(self):
        backup = self.execute("backup")["backups"][0]
        (mapping._directory(self.base) / f"{backup['id']}.stcm").write_bytes(b"corrupt")
        with self.assertRaisesRegex(robot.RobotApiError, "校验失败"):
            self.execute("restore", backup_id=backup["id"], confirm=True)
        self.assertFalse(any(method == "PUT" for method, *_ in self.raw_calls))

    def test_import_validation_and_runtime_only_upload(self):
        for filename, content in (("map.png", "c3RjbQ=="), ("../map.stcm", "c3RjbQ=="), ("map.stcm", "???")):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.execute("import", filename=filename, content_base64=content, confirm=True)
        with patch.object(mapping, "MAX_STCM_BYTES", 2):
            with self.assertRaises(ValueError):
                self.execute("import", filename="map.stcm", content_base64="c3RjbQ==", confirm=True)
        self.assertEqual(self.raw_calls, [])
        imported = b"stcm-import-data"
        state = self.execute("import", filename="map.stcm", content_base64=base64.b64encode(imported).decode(), confirm=True)
        self.assertEqual(self.raw, imported)
        self.assertTrue(state["dirty"])
        self.assertEqual([call[0] for call in self.raw_calls], ["GET", "PUT"])
        self.assertFalse(any(path.endswith("stcm/:save") for _, path, *_ in self.calls))

    def test_upload_requires_exactly_one_known_floor_before_persistence(self):
        for floors in ([], {}, [{"floor": "1F"}, {"floor": "2F"}], [{}], [{"floor": ""}]):
            self.floors = floors
            with self.subTest(floors=floors), self.assertRaises(robot.RobotApiError):
                self.execute("upload", confirm=True)
        self.assertFalse(any(path.endswith("stcm/:save") for _, path, *_ in self.calls))
        self.floors = [{"floor": "1F"}]
        state = self.execute("upload", confirm=True)
        self.assertEqual(state["phase"], "saved")
        self.assertFalse(state["dirty"])
        self.assertEqual([method for method, path, *_ in self.calls if path.endswith("stcm/:save")], ["POST"])

    def test_persistence_failure_never_reports_saved(self):
        original = self.request

        def failure(method, path, payload=None, **kwargs):
            if path.endswith("stcm/:save"):
                raise robot.RobotApiError("save failed")
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=failure):
            with self.assertRaises(robot.RobotApiError):
                self.execute("upload", confirm=True)
        state = mapping.get_status(self.base)
        self.assertTrue(state["dirty"])
        self.assertEqual(state["phase"], "finished")

    def test_failed_replacement_blocks_navigation_until_confirmed_recovery(self):
        backup = self.execute("backup")["backups"][0]
        original_request = self.request
        original_stcm = self.stcm

        def failed_clear(method, path, payload=None, **kwargs):
            if method == "DELETE" and path == "/api/core/slam/v1/maps":
                raise robot.RobotApiError("clear result unknown", 504)
            return original_request(method, path, payload, **kwargs)

        def failed_import(method, base_url, payload=None):
            if method == "PUT":
                self.raw = b"partially-imported-map"
                raise robot.RobotApiError("import result unknown", 504)
            return original_stcm(method, base_url, payload)

        for command in ("new", "clear", "import", "restore"):
            values = {"confirm": True}
            if command == "import":
                values.update(filename="map.stcm", content_base64="c3RjbQ==")
            elif command == "restore":
                values["backup_id"] = backup["id"]
            with self.subTest(command=command):
                with (
                    patch.object(robot, "_request", side_effect=failed_clear),
                    patch.object(mapping, "_stcm_request", side_effect=failed_import),
                ):
                    with self.assertRaises(robot.RobotApiError):
                        self.execute(command, **values)
                mapping._KNOWN_MAPPING.clear()
                self.assertTrue(mapping.get_status(self.base)["map_write_uncertain"])
                with self.assertRaisesRegex(robot.RobotApiError, "地图替换结果"):
                    robot._create_action("GoHomeAction", {}, base_url=self.base)
                for blocked in ("start", "resume", "upload"):
                    with self.assertRaisesRegex(robot.RobotApiError, "地图替换结果"):
                        self.execute(blocked, confirm=True)
                self.mapping_enabled = True
                self.assertTrue(self.execute("finish")["map_write_uncertain"])
                recovered = self.execute(command, **values)
                self.assertFalse(recovered["map_write_uncertain"])
                mapping.require_navigation_allowed(self.base)

    def test_invalid_deployment_does_not_mark_map_dirty(self):
        with self.assertRaises(ValueError):
            self.execute("deploy", type="poi", name="Stop", x=float("nan"), y=0)
        self.assertFalse(mapping._load_state(self.base)["dirty"])
        self.assertFalse((mapping._directory(self.base) / "state.json").exists())
        self.assertFalse(any(method in {"POST", "PUT", "DELETE"} for method, *_ in self.calls))

    def test_deployment_uses_shared_idle_guard_and_invalidates_patrol(self):
        from ksq.web import robot_mapping_objects

        self.execute("start")
        with patch.object(robot_mapping_objects, "save_object", return_value={"object": {"id": "new"}}) as save:
            with self.assertRaises(robot.RobotApiError):
                self.execute("deploy", type="poi", name="Stop", x=0, y=0)
            save.assert_not_called()
            self.execute("finish")
            robot._PATROL_TRACK_PLANS[self.base] = {"plan_id": "old"}
            result = self.execute("deploy", type="poi", name="Stop", x=0, y=0)
            self.assertTrue(result["dirty"])
            self.assertEqual(save.call_args.args[1], self.base)
            self.assertEqual(save.call_args.args[0]["type"], "poi")
            self.assertNotIn(self.base, robot._PATROL_TRACK_PLANS)

    def test_delete_backup_removes_only_selected_map_files(self):
        first = self.execute("backup")["backups"][0]
        second = self.execute("backup")["backups"]
        self.assertEqual(len(second), 2)
        state = self.execute("delete-backup", backup_id=first["id"], confirm=True)
        self.assertEqual(len(state["backups"]), 1)
        self.assertTrue(self.settings.is_file())
        self.assertTrue((mapping._directory(self.base) / "state.json").is_file())

    def test_connection_switch_waits_for_whole_mapping_operation(self):
        entered = threading.Event()
        release = threading.Event()
        switched = threading.Event()
        failures = []

        def blocking_stcm(method, base_url, payload=None):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.stcm(method, base_url, payload)

        def backup():
            try:
                self.execute("backup")
            except Exception as error:
                failures.append(error)

        def switch():
            try:
                robot.save_settings({"robot_base_url": self.other, "expected_robot_base_url": self.base})
                switched.set()
            except Exception as error:
                failures.append(error)

        with patch.object(mapping, "_stcm_request", side_effect=blocking_stcm):
            first = threading.Thread(target=backup)
            first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=switch)
            second.start()
            self.assertFalse(switched.wait(0.05))
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(switched.is_set())
        with self.assertRaises(ValueError):
            self.execute("clear", confirm=True)
        self.assertEqual(self.raw_calls[0][1], self.base)


class StcmTransportTests(unittest.TestCase):
    def test_binary_transport_uses_pinned_endpoint_and_bounded_read(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}

        with patch.object(mapping.urllib.request, "urlopen", return_value=Response(b"map")) as open_url:
            result = mapping._stcm_request("GET", "http://192.168.5.9:1448")
        self.assertEqual(result, b"map")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.5.9:1448/api/core/slam/v1/maps/stcm")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 20)
        with patch.object(mapping, "MAX_STCM_BYTES", 2), patch.object(mapping.urllib.request, "urlopen", return_value=Response(b"long")):
            with self.assertRaises(robot.RobotApiError):
                mapping._stcm_request("GET", "http://192.168.5.9:1448")


if __name__ == "__main__":
    unittest.main()
