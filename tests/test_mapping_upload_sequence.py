"""Firmware upload ordering and failures, with no chassis communication."""

from __future__ import annotations

import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from ksq.web import robot_map_api as robot
from ksq.web import robot_mapping_api as mapping
from ksq.web import auth

try:
    from ksq.web import handlers
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    handlers = None


UPLOAD_ID = "12345678-1234-4234-8234-123456789012"
OTHER_ID = "12345678-1234-4234-8234-123456789013"


class MappingUploadSequenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = "http://192.0.2.10:1448"
        settings = Path(directory.name) / "robot_map_settings.json"
        settings.write_text(json.dumps({"robot_base_url": self.base}), encoding="utf-8")
        self.calls = []
        self.fail_at = None
        self.reject_at = None
        self.raw = b"\x00current-runtime-map\xff"
        for target, name, options in (
            (robot, "ROBOT_MAP_SETTINGS_FILE", {"new": settings}),
            (robot, "_request", {"side_effect": self.request}),
            (robot, "get_current_action", {"return_value": {"state": {"status": 4}}}),
            (robot, "_invalidate_telemetry_cache", {}),
            (mapping, "_read_flag", {"return_value": False}),
            (mapping, "_stcm_request", {"side_effect": self.stcm}),
            (mapping, "_status", {"side_effect": mapping._load_state}),
            (mapping, "_DRIVE_LEASES", {"new": {}}),
            (mapping, "_UPLOAD_PROGRESS", {"new": {}}),
        ):
            patcher = patch.object(target, name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method, path, payload=None, **kwargs):
        self.assertEqual(kwargs["base_url"], self.base)
        if path.endswith("/floors"):
            return 200, [{"floor": "1F"}]
        self.calls.append((method, path, payload))
        if path == self.fail_at:
            raise robot.RobotApiError("request timed out", 504)
        return 200, False if path == self.reject_at else {}

    def stcm(self, method, base_url, payload=None):
        self.assertEqual(base_url, self.base)
        path = mapping._PERSISTENT_STCM_PATH if method == "POST" else mapping._STCM_PATH
        self.calls.append((method, path, payload))
        if path == self.fail_at:
            raise robot.RobotApiError("transfer timed out", 504)
        if path == self.reject_at:
            return b"false"
        return self.raw if method == "GET" else b""

    def upload(self, **values):
        return mapping.execute({
            "command": "upload", "expected_robot_base_url": self.base,
            "confirm": True, **values,
        })

    def test_upload_sends_current_binary_then_reload_then_save(self):
        state = self.upload()
        self.assertEqual(self.calls, [
            ("GET", mapping._STCM_PATH, None),
            ("POST", mapping._PERSISTENT_STCM_PATH, self.raw),
            ("POST", mapping._PERSISTENT_STCM_PATH + "/:reload", None),
            ("POST", mapping._PERSISTENT_STCM_PATH + "/:save", None),
        ])
        self.assertEqual(state["phase"], "saved")
        self.assertFalse(state["dirty"])
        self.assertFalse(state["map_write_uncertain"])

    def test_failure_at_each_stage_stops_subsequent_writes(self):
        paths = [mapping._STCM_PATH, mapping._PERSISTENT_STCM_PATH,
                 mapping._PERSISTENT_STCM_PATH + "/:reload", mapping._PERSISTENT_STCM_PATH + "/:save"]
        labels = ["读取当前地图", "第 1/3 步", "第 2/3 步", "第 3/3 步"]
        for index, (path, label) in enumerate(zip(paths, labels)):
            with self.subTest(stage=label):
                mapping._save_state(self.base, {"name": "Map", "phase": "saved", "dirty": False,
                                                "map_write_uncertain": False})
                self.calls.clear()
                self.fail_at = path
                with self.assertRaisesRegex(robot.RobotApiError, label) as caught:
                    self.upload()
                self.assertEqual(caught.exception.status_code, 504)
                self.assertEqual([entry[1] for entry in self.calls], paths[:index + 1])
                state = mapping._load_state(self.base)
                self.assertEqual(state["phase"], "finished")
                self.assertTrue(state["dirty"])
                self.assertEqual(state["map_write_uncertain"], index == 2)

    def test_explicit_firmware_rejection_never_reports_saved(self):
        for index, suffix in enumerate(("", "/:reload", "/:save")):
            with self.subTest(stage=suffix):
                mapping._save_state(self.base, {"name": "Map", "phase": "finished", "dirty": True,
                                                "map_write_uncertain": False})
                self.calls.clear()
                self.reject_at = mapping._PERSISTENT_STCM_PATH + suffix
                with self.assertRaisesRegex(robot.RobotApiError, f"第 {index + 1}/3 步"):
                    self.upload()
                self.assertEqual(len(self.calls), index + 2)
                self.assertTrue(mapping._load_state(self.base)["dirty"])

    def test_unconfirmed_request_never_exports_or_uploads(self):
        with self.assertRaises(ValueError):
            self.upload(confirm=False)
        self.assertEqual(self.calls, [])

    def test_cloud_reload_cannot_make_final_save_erase_other_floors(self):
        original = self.request
        floor_reads = iter(([{"floor": "1F"}], [{"floor": "1F"}, {"floor": "2F"}]))

        def request(method, path, payload=None, **kwargs):
            if path.endswith("/floors"):
                return 200, next(floor_reads)
            return original(method, path, payload, **kwargs)

        with patch.object(robot, "_request", side_effect=request):
            with self.assertRaisesRegex(robot.RobotApiError, "第 3/3 步.*保护其他楼层地图"):
                self.upload()
        self.assertEqual(self.calls[-1][1], mapping._PERSISTENT_STCM_PATH + "/:reload")
        self.assertTrue(mapping._load_state(self.base)["dirty"])

    def test_reload_timeout_blocks_navigation_and_blind_upload_retry(self):
        self.fail_at = mapping._PERSISTENT_STCM_PATH + "/:reload"
        with self.assertRaises(robot.RobotApiError):
            self.upload()
        self.calls.clear()
        with self.assertRaisesRegex(robot.RobotApiError, "地图替换结果尚未确认"):
            mapping.require_navigation_allowed(self.base)
        with self.assertRaisesRegex(robot.RobotApiError, "地图替换结果尚未确认"):
            self.upload()
        self.assertEqual(self.calls, [])

    def test_progress_tracks_actual_calls_and_the_completed_step_count(self):
        observed = []
        original_stcm, original_request = self.stcm, self.request

        def observe():
            snapshot = mapping.get_upload_progress(self.base, UPLOAD_ID)
            observed.append((snapshot["stage"], snapshot["completed_steps"], snapshot["status"]))

        def stcm(*args, **kwargs):
            observe()
            return original_stcm(*args, **kwargs)

        def request(*args, **kwargs):
            if not args[1].endswith("/floors"):
                observe()
            return original_request(*args, **kwargs)

        with patch.object(mapping, "_stcm_request", side_effect=stcm), patch.object(robot, "_request", side_effect=request):
            result = self.upload(upload_id=UPLOAD_ID)
        self.assertEqual(observed, [
            ("preparing", 0, "running"), ("upload", 0, "running"),
            ("reload", 1, "running"), ("save", 2, "running"),
        ])
        progress = mapping.get_upload_progress(self.base, UPLOAD_ID)
        self.assertEqual(progress, {
            "robot_base_url": self.base, "upload_id": UPLOAD_ID, "status": "succeeded",
            "stage": "save", "completed_steps": 3, "error": "",
        })
        self.assertEqual(result["upload_progress"], progress)
        progress["status"] = "changed"
        self.assertEqual(mapping.get_upload_progress(self.base, UPLOAD_ID)["status"], "succeeded")

    def test_progress_remains_readable_while_upload_holds_connection_lock(self):
        uploading, release, readable = threading.Event(), threading.Event(), threading.Event()
        failures = []
        original = self.stcm

        def stcm(method, *args, **kwargs):
            if method == "POST":
                uploading.set()
                if not release.wait(3):
                    raise AssertionError("test upload was not released")
            return original(method, *args, **kwargs)

        def upload():
            try:
                self.upload(upload_id=UPLOAD_ID)
            except Exception as error:
                failures.append(error)

        def read():
            try:
                self.assertEqual(mapping.get_upload_progress(self.base, UPLOAD_ID)["stage"], "upload")
            except Exception as error:
                failures.append(error)
            finally:
                readable.set()

        with patch.object(mapping, "_stcm_request", side_effect=stcm):
            worker = threading.Thread(target=upload)
            reader = threading.Thread(target=read)
            worker.start()
            try:
                self.assertTrue(uploading.wait(1))
                reader.start()
                self.assertTrue(readable.wait(1), "Progress must not wait for the upload's connection lock")
                with self.assertRaisesRegex(robot.RobotApiError, "已有地图正在上传"):
                    self.upload(upload_id=OTHER_ID)
            finally:
                release.set()
                worker.join(3)
                if reader.ident is not None:
                    reader.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)

    def test_failed_progress_preserves_exact_failed_stage(self):
        for index, path in enumerate((mapping._STCM_PATH, mapping._PERSISTENT_STCM_PATH,
                                      mapping._PERSISTENT_STCM_PATH + "/:reload", mapping._PERSISTENT_STCM_PATH + "/:save")):
            with self.subTest(path=path):
                mapping._UPLOAD_PROGRESS.clear()
                mapping._save_state(self.base, {"name": "Map", "phase": "finished", "dirty": True, "map_write_uncertain": False})
                self.fail_at = path
                with self.assertRaises(robot.RobotApiError):
                    self.upload(upload_id=UPLOAD_ID)
                snapshot = mapping.get_upload_progress(self.base, UPLOAD_ID)
                self.assertEqual(snapshot["status"], "failed")
                self.assertEqual(snapshot["stage"], ("preparing", "upload", "reload", "save")[index])
                self.assertEqual(snapshot["completed_steps"], max(0, index - 1))
                self.assertIn("未确认完成", snapshot["error"])

    def test_precondition_failure_is_terminal_and_used_id_is_not_reexecuted(self):
        with patch.object(mapping, "_require_idle", side_effect=robot.RobotApiError("robot is moving", 409)):
            with self.assertRaises(robot.RobotApiError):
                self.upload(upload_id=UPLOAD_ID)
        self.assertEqual(mapping.get_upload_progress(self.base, UPLOAD_ID), {
            "robot_base_url": self.base, "upload_id": UPLOAD_ID, "status": "failed",
            "stage": "preparing", "completed_steps": 0, "error": "robot is moving",
        })
        with self.assertRaisesRegex(robot.RobotApiError, "上传编号已使用"):
            self.upload(upload_id=UPLOAD_ID)
        self.assertEqual(self.calls, [])

    def test_unexpected_local_error_does_not_leave_progress_running(self):
        with patch.object(mapping, "_stcm_request", side_effect=OSError("local read failed")):
            with self.assertRaises(OSError):
                self.upload(upload_id=UPLOAD_ID)
        progress = mapping.get_upload_progress(self.base, UPLOAD_ID)
        self.assertEqual((progress["status"], progress["stage"], progress["completed_steps"]), ("failed", "preparing", 0))
        self.assertEqual(progress["error"], "local read failed")

    def test_unknown_or_old_progress_never_claims_success(self):
        with self.assertRaises(robot.RobotApiError) as caught:
            mapping.get_upload_progress(self.base, UPLOAD_ID)
        self.assertEqual(caught.exception.status_code, 404)
        self.upload(upload_id=UPLOAD_ID)
        self.upload(upload_id=OTHER_ID)
        with self.assertRaises(robot.RobotApiError) as caught:
            mapping.get_upload_progress(self.base, UPLOAD_ID)
        self.assertEqual(caught.exception.status_code, 404)
        with patch.object(robot, "_base_url", return_value="http://192.0.2.11:1448"):
            with self.assertRaises(ValueError):
                mapping.get_upload_progress(self.base, OTHER_ID)
            with self.assertRaises(robot.RobotApiError):
                mapping.get_upload_progress("http://192.0.2.11:1448", OTHER_ID)

    def test_invalid_progress_id_is_rejected_without_robot_calls(self):
        for identifier in (None, "", "not-a-uuid", {}, 1, "a" * 100):
            with self.subTest(identifier=identifier):
                with self.assertRaises(ValueError):
                    mapping.get_upload_progress(self.base, identifier)
                with self.assertRaises(ValueError):
                    self.upload(upload_id=identifier)
        self.assertEqual(self.calls, [])


@unittest.skipIf(handlers is None, "HTTP handlers require Python 3.12 or older")
class MappingUploadProgressHandlerTests(unittest.TestCase):
    def request(self, *, logged_in=True, expected="http://192.0.2.10:1448", upload_id=UPLOAD_ID):
        handler = handlers.QueryHandler.__new__(handlers.QueryHandler)
        handler.path = "/api/map/mapping/upload-progress?" + urlencode({
            "expected_robot_base_url": expected, "upload_id": upload_id,
        })
        handler.command = "GET"
        handler.headers = {}
        handler.rfile, handler.wfile = io.BytesIO(), io.BytesIO()
        response = {}
        handler._send_json = lambda status, data: response.update(status=int(status), data=data)
        session = {"username": "progress-test", "role": auth.ROLE_VIEWER} if logged_in else None
        with patch.object(auth, "session_from_cookie", return_value=session):
            handler.do_GET()
        return response

    def test_progress_route_uses_nonblocking_reader_not_global_status(self):
        with (
            patch.object(mapping, "get_upload_progress", return_value={"status": "running"}) as reader,
            patch.object(mapping, "get_status") as status,
            patch.object(robot, "require_current_base_url") as connection,
        ):
            self.assertEqual(self.request(), {"status": 200, "data": {"status": "running"}})
            reader.assert_called_once_with("http://192.0.2.10:1448", UPLOAD_ID)
            status.assert_not_called()
            connection.assert_not_called()

    def test_progress_requires_authentication(self):
        with patch.object(mapping, "get_upload_progress") as reader:
            self.assertEqual(self.request(logged_in=False)["status"], 401)
            reader.assert_not_called()

    def test_missing_base_and_unknown_progress_keep_error_status(self):
        with patch.object(mapping, "get_upload_progress") as reader:
            self.assertEqual(self.request(expected="")["status"], 400)
            reader.assert_not_called()
        for error, status in ((ValueError("changed chassis"), 400), (robot.RobotApiError("unknown upload", 404), 404)):
            with self.subTest(status=status), patch.object(mapping, "get_upload_progress", side_effect=error):
                self.assertEqual(self.request(), {"status": status, "data": {"error": str(error)}})


class PersistentStcmTransportTests(unittest.TestCase):
    def test_post_uses_persistent_endpoint_and_exact_binary_body(self):
        response = Mock(headers={"Content-Length": "0"})
        response.read.return_value = b""
        with patch.object(mapping.urllib.request, "urlopen") as open_request:
            open_request.return_value.__enter__.return_value = response
            self.assertEqual(mapping._stcm_request("POST", "http://192.0.2.10:1448", b"\x00\xffSTCM"), b"")
        request = open_request.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.0.2.10:1448/api/multi-floor/map/v1/stcm")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"\x00\xffSTCM")
        self.assertEqual(request.get_header("Content-type"), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
