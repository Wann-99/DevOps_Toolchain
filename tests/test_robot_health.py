"""Health reads/clears and pose responses stay pinned to the selected chassis."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import call, patch
from urllib.parse import urlencode

from ksq.web import auth
from ksq.robot import service as api

try:
    from ksq.web import handlers
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    handlers = None


BASE = "http://192.0.2.10:1448"
OTHER = "http://192.0.2.11:1448"
POSE = {"x": 1.5, "y": -2.0, "z": 0, "yaw": 0.4, "pitch": 0, "roll": 0}


def health() -> dict:
    return {
        "baseError": [{
            "component": 1, "componentErrorCode": 1792,
            "componentErrorDeviceId": -1, "componentErrorType": 2055,
            "errorCode": 33621760, "id": 0, "level": 2,
            "message": "motor brake released",
        }],
        "hasError": True, "hasFatal": False, "hasWarning": False,
        "hasDepthCameraDisconnected": False, "hasLidarDisconnected": False,
        "hasSdpDisconnected": False, "hasSystemEmergencyStop": False,
    }


class RobotHealthTests(unittest.TestCase):
    READERS = (
        (api.get_robot_health, "/api/core/system/v1/robot/health", health()),
        (api.get_current_pose, "/api/core/slam/v1/localization/pose", POSE),
    )

    def test_health_preserves_firmware_alerts_and_extension_fields(self) -> None:
        payload = {**health(), "firmwareExtension": {"detail": "unchanged"}}
        with (
            patch.object(api, "_base_url", return_value=BASE),
            patch.object(api, "_request", return_value=(200, payload)) as request,
        ):
            self.assertEqual(api.get_robot_health(expected_base_url=BASE), payload)
        request.assert_called_once_with(
            "GET", "/api/core/system/v1/robot/health", base_url=BASE,
        )

    def test_official_health_fields_do_not_require_firmware_extensions(self) -> None:
        payload = {"baseError": [], "hasError": False, "hasFatal": False, "hasWarning": False}
        with (
            patch.object(api, "_base_url", return_value=BASE),
            patch.object(api, "_request", return_value=(200, payload)),
        ):
            self.assertEqual(api.get_robot_health(), payload)

    def test_malformed_health_never_becomes_a_healthy_response(self) -> None:
        invalid = [None, [], "healthy", {}, {**health(), "baseError": {}},
                   {**health(), "baseError": ["alert"]},
                   {**health(), "hasError": 0}, {**health(), "hasFatal": None},
                   {**health(), "hasWarning": "false"},
                   {**health(), "hasLidarDisconnected": "false"}]
        for payload in invalid:
            with (
                self.subTest(payload=payload),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request", return_value=(200, payload)),
            ):
                with self.assertRaises(api.RobotApiError) as caught:
                    api.get_robot_health(expected_base_url=BASE)
                self.assertEqual(caught.exception.status_code, 502)

    def test_readers_keep_no_argument_calls_and_pin_the_request(self) -> None:
        for reader, path, payload in self.READERS:
            with (
                self.subTest(reader=reader.__name__),
                patch.object(api, "_base_url", return_value=BASE) as current,
                patch.object(api, "_request", return_value=(200, payload)) as request,
            ):
                self.assertEqual(reader(), payload)
                request.assert_called_once_with("GET", path, base_url=BASE)
                self.assertEqual(current.call_count, 2)

    def test_readers_reject_a_stale_expected_chassis_before_requesting(self) -> None:
        for reader, _, _ in self.READERS:
            with (
                self.subTest(reader=reader.__name__),
                patch.object(api, "_base_url", return_value=OTHER),
                patch.object(api, "_request") as request,
            ):
                with self.assertRaises(ValueError):
                    reader(expected_base_url=BASE)
                request.assert_not_called()

    def test_readers_discard_responses_if_chassis_changes_during_request(self) -> None:
        for reader, path, payload in self.READERS:
            for expected in (BASE, None):
                with (
                    self.subTest(reader=reader.__name__, expected=expected),
                    patch.object(api, "_base_url", side_effect=[BASE, OTHER]),
                    patch.object(api, "_request", return_value=(200, payload)) as request,
                ):
                    with self.assertRaises(ValueError):
                        reader(expected_base_url=expected)
                    request.assert_called_once_with("GET", path, base_url=BASE)

    def test_read_errors_propagate_without_empty_status_fallback(self) -> None:
        for reader, _, _ in self.READERS:
            with (
                self.subTest(reader=reader.__name__),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request", side_effect=api.RobotApiError("offline", 504)),
            ):
                with self.assertRaises(api.RobotApiError) as caught:
                    reader(expected_base_url=BASE)
                self.assertEqual(caught.exception.status_code, 504)


class RobotHealthClearTests(unittest.TestCase):
    PATH = "/api/core/system/v1/robot/health"
    HEALTHY = {"baseError": [], "hasError": False, "hasFatal": False, "hasWarning": False}

    def test_clear_uses_unique_error_codes_not_row_ids_then_verifies_health(self) -> None:
        before = health()
        before["baseError"] += [dict(before["baseError"][0]), {"id": 7, "errorCode": 33621761}]
        with (
            patch.object(api, "_base_url", return_value=BASE),
            patch.object(api, "_request", side_effect=[
                (200, before), (200, {}), (200, {}), (200, self.HEALTHY),
            ]) as request,
        ):
            self.assertEqual(api.clear_robot_health(expected_base_url=BASE, confirm=True), self.HEALTHY)
        self.assertEqual(request.call_args_list, [
            call("GET", self.PATH, base_url=BASE),
            call("DELETE", self.PATH + "/33621760", base_url=BASE),
            call("DELETE", self.PATH + "/33621761", base_url=BASE),
            call("GET", self.PATH, base_url=BASE),
        ])

    def test_remaining_health_faults_are_preserved(self) -> None:
        with (
            patch.object(api, "_base_url", return_value=BASE),
            patch.object(api, "_request", side_effect=[(200, health()), (200, {}), (200, health())]),
        ):
            self.assertEqual(api.clear_robot_health(expected_base_url=BASE, confirm=True), health())

    def test_empty_fault_list_does_not_send_a_delete(self) -> None:
        with (
            patch.object(api, "_base_url", return_value=BASE),
            patch.object(api, "_request", return_value=(200, self.HEALTHY)) as request,
        ):
            self.assertEqual(api.clear_robot_health(expected_base_url=BASE, confirm=True), self.HEALTHY)
        self.assertEqual(request.call_args_list, [call("GET", self.PATH, base_url=BASE)] * 2)

    def test_all_codes_are_validated_before_any_clear(self) -> None:
        for code in (None, True, "33621760", 0, -1, 1.5, {}, []):
            before = health()
            before["baseError"].append({"errorCode": code})
            with (
                self.subTest(code=code),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request", return_value=(200, before)) as request,
            ):
                with self.assertRaises(api.RobotApiError):
                    api.clear_robot_health(expected_base_url=BASE, confirm=True)
                request.assert_called_once_with("GET", self.PATH, base_url=BASE)

    def test_confirmation_and_selected_chassis_are_required(self) -> None:
        invalid = [{"confirm": value, "expected_base_url": BASE} for value in (None, False, 1, "true")]
        invalid += [{"confirm": True, "expected_base_url": value} for value in (None, "", "  ", OTHER)]
        for options in invalid:
            with (
                self.subTest(options=options),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request") as request,
            ):
                with self.assertRaises(ValueError):
                    api.clear_robot_health(**options)
                request.assert_not_called()

    def test_chassis_change_after_read_does_not_clear_anything(self) -> None:
        with (
            patch.object(api, "_base_url", side_effect=[BASE, BASE, OTHER]),
            patch.object(api, "_request", return_value=(200, health())) as request,
        ):
            with self.assertRaises(ValueError):
                api.clear_robot_health(expected_base_url=BASE, confirm=True)
        request.assert_called_once_with("GET", self.PATH, base_url=BASE)

    def test_failed_clear_or_verification_is_not_reported_as_success(self) -> None:
        scenarios = [
            [(200, health()), api.RobotApiError("cannot clear", 409)],
            [(200, health()), (200, {}), api.RobotApiError("offline", 504)],
            [(200, health()), (200, {}), (200, {})],
        ]
        for responses in scenarios:
            with (
                self.subTest(responses=responses),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request", side_effect=responses),
            ):
                with self.assertRaises(api.RobotApiError):
                    api.clear_robot_health(expected_base_url=BASE, confirm=True)


@unittest.skipIf(handlers is None, "HTTP handlers require Python 3.12 or older")
class RobotHealthHandlerTests(unittest.TestCase):
    ROUTES = (("health", "get_robot_health", health()), ("pose", "get_current_pose", POSE))

    def request(self, route: str, *, expected: str | None = BASE, logged_in: bool = True) -> dict:
        handler = handlers.QueryHandler.__new__(handlers.QueryHandler)
        handler.path = "/api/map/" + route
        if expected is not None:
            handler.path += "?" + urlencode({"expected_robot_base_url": expected})
        handler.command = "GET"
        handler.headers = {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        response = {}
        handler._send_json = lambda status, data: response.update(status=int(status), data=data)
        session = {"username": "viewer-test", "role": auth.ROLE_VIEWER} if logged_in else None
        with patch.object(auth, "session_from_cookie", return_value=session):
            handler.do_GET()
        return response

    def test_viewer_can_read_health_and_pose_without_write_permission(self) -> None:
        for route, name, payload in self.ROUTES:
            with self.subTest(route=route), patch.object(api, name, return_value=payload) as reader:
                self.assertEqual(self.request(route), {"status": 200, "data": payload})
                reader.assert_called_once_with(expected_base_url=BASE)

    def test_unauthenticated_requests_never_reach_the_chassis(self) -> None:
        for route, name, _ in self.ROUTES:
            with self.subTest(route=route), patch.object(api, name) as reader:
                self.assertEqual(self.request(route, logged_in=False)["status"], 401)
                reader.assert_not_called()

    def test_legacy_no_query_pose_request_still_works(self) -> None:
        with patch.object(api, "get_current_pose", return_value=POSE) as reader:
            self.assertEqual(self.request("pose", expected=None), {"status": 200, "data": POSE})
            reader.assert_called_once_with(expected_base_url=None)

    def test_backend_failures_are_not_returned_as_healthy_or_empty_pose(self) -> None:
        for route, name, _ in self.ROUTES:
            for code in (404, 502, 504):
                with self.subTest(route=route, code=code), patch.object(
                    api, name, side_effect=api.RobotApiError("firmware unavailable", code),
                ):
                    self.assertEqual(self.request(route), {
                        "status": code, "data": {"error": "firmware unavailable"},
                    })

    def test_http_chassis_change_rejects_both_early_and_late_responses(self) -> None:
        for route, _, payload in self.ROUTES:
            for bases in ([OTHER], [BASE, OTHER]):
                with (
                    self.subTest(route=route, bases=bases),
                    patch.object(api, "_base_url", side_effect=bases),
                    patch.object(api, "_request", return_value=(200, payload)) as request,
                ):
                    response = self.request(route)
                    self.assertEqual(response["status"], 400)
                    self.assertIn("error", response["data"])
                    self.assertEqual(request.call_count, len(bases) - 1)


@unittest.skipIf(handlers is None, "HTTP handlers require Python 3.12 or older")
class RobotHealthClearHandlerTests(unittest.TestCase):
    def request(self, *, payload=None, role=auth.ROLE_ADMIN, content_type="application/json") -> dict:
        handler = handlers.QueryHandler.__new__(handlers.QueryHandler)
        handler.path = "/api/map/health/clear"
        handler.command = "POST"
        raw = json.dumps(payload if payload is not None else {
            "expected_robot_base_url": BASE, "confirm": True,
        }).encode()
        handler.headers = {"Content-Type": content_type, "Content-Length": str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        response = {}
        handler._send_json = lambda status, data: response.update(status=int(status), data=data)
        session = {"username": "health-test", "role": role} if role is not None else None
        with patch.object(auth, "session_from_cookie", return_value=session):
            handler.do_POST()
        self.assertEqual(handler.rfile.tell(), len(raw))
        return response

    def test_admin_receives_verified_health_even_if_fault_remains(self) -> None:
        with patch.object(api, "clear_robot_health", return_value=health()) as clear:
            self.assertEqual(self.request(), {"status": 200, "data": health()})
            clear.assert_called_once_with(expected_base_url=BASE, confirm=True)

    def test_anonymous_and_viewer_cannot_clear_faults(self) -> None:
        for role, status in ((None, 401), (auth.ROLE_VIEWER, 403)):
            with self.subTest(role=role), patch.object(api, "clear_robot_health") as clear:
                self.assertEqual(self.request(role=role)["status"], status)
                clear.assert_not_called()

    def test_simple_cross_site_content_types_are_rejected(self) -> None:
        for content_type in ("", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            with self.subTest(content_type=content_type), patch.object(api, "clear_robot_health") as clear:
                self.assertEqual(self.request(content_type=content_type)["status"], 415)
                clear.assert_not_called()

    def test_missing_or_stale_expected_robot_never_reaches_chassis(self) -> None:
        for expected in (None, "", OTHER):
            with (
                self.subTest(expected=expected),
                patch.object(api, "_base_url", return_value=BASE),
                patch.object(api, "_request") as request,
            ):
                response = self.request(payload={"expected_robot_base_url": expected, "confirm": True})
                self.assertEqual(response["status"], 400)
                request.assert_not_called()

    def test_confirmation_requires_literal_true(self) -> None:
        for confirm in (None, False, 1, "true"):
            with self.subTest(confirm=confirm), patch.object(api, "_request") as request:
                self.assertEqual(self.request(payload={
                    "expected_robot_base_url": BASE, "confirm": confirm,
                })["status"], 400)
                request.assert_not_called()

    def test_firmware_failure_status_is_preserved(self) -> None:
        for status in (400, 404, 502, 504):
            with self.subTest(status=status), patch.object(
                api, "clear_robot_health", side_effect=api.RobotApiError("clear unavailable", status),
            ):
                self.assertEqual(self.request(), {"status": status, "data": {"error": "clear unavailable"}})


if __name__ == "__main__":
    unittest.main()
