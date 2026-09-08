"""Mapping HTTP boundaries without a chassis connection."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import urlencode

from ksq.web import auth

try:
    from ksq.web import handlers

    QueryHandler = handlers.QueryHandler
except ModuleNotFoundError as error:
    if error.name != "cgi":
        raise
    QueryHandler = None


@unittest.skipIf(QueryHandler is None, "HTTP handlers require Python 3.12 or older")
class MappingHandlerTests(unittest.TestCase):
    BASE_URL = "http://192.0.2.10:1448"
    ROUTE = "/api/map/mapping"

    def setUp(self) -> None:
        self.get_status = self._mock(handlers.robot_mapping_api, "get_status")
        self.export_map = self._mock(handlers.robot_mapping_api, "export_map")
        self.execute = self._mock(handlers.robot_mapping_api, "execute")
        self.list_objects = self._mock(handlers.robot_mapping_objects, "list_objects")
        self.current_base = self._mock(handlers.robot_map_api, "require_current_base_url")
        self.current_base.return_value = self.BASE_URL

    def _mock(self, module, name: str) -> Mock:
        mocked = patch.object(module, name)
        result = mocked.start()
        self.addCleanup(mocked.stop)
        return result

    def _url(self, suffix: str = "") -> str:
        return self.ROUTE + suffix + "?" + urlencode(
            {"expected_robot_base_url": self.BASE_URL}
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: object = None,
        role: str | None = auth.ROLE_ADMIN,
        content_length: int | None = None,
    ) -> tuple[QueryHandler, dict]:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        handler = QueryHandler.__new__(QueryHandler)
        handler.path = path
        handler.command = method
        handler.headers = {
            "Content-Length": str(len(raw) if content_length is None else content_length),
            "Content-Type": "application/json",
        }
        handler.rfile = Mock(wraps=io.BytesIO(raw))
        handler.wfile = io.BytesIO()
        response = {"headers": {}}
        handler._send_json = lambda status, data: response.update(
            status=int(status), data=data
        )
        handler.send_response = lambda status: response.update(status=int(status))
        handler.send_header = lambda name, value: response["headers"].update(
            {name: value}
        )
        handler.end_headers = lambda: None
        session = None if role is None else {
            "username": "mapping-test", "display_name": "mapping-test", "role": role
        }
        with patch.object(auth, "session_from_cookie", return_value=session):
            getattr(handler, f"do_{method}")()
        self.assertIn("status", response)
        return handler, response

    def _assert_no_chassis_call(self) -> None:
        for mocked in (
            self.get_status, self.export_map, self.execute,
            self.list_objects, self.current_base,
        ):
            mocked.assert_not_called()

    def test_get_routes_require_expected_robot_query(self) -> None:
        for suffix in ("", "/objects", "/export"):
            for query in ("", "?expected_robot_base_url=", "?expected_robot_base_url=%20"):
                with self.subTest(suffix=suffix, query=query):
                    _, response = self._request("GET", self.ROUTE + suffix + query)
                    self.assertEqual(response["status"], 400)
                    self.assertIn("error", response["data"])
        self._assert_no_chassis_call()

    def test_status_forwards_expected_robot_and_snapshot(self) -> None:
        snapshot = {"phase": "paused", "mapping_enabled": False, "backups": []}
        self.get_status.return_value = snapshot
        _, response = self._request("GET", self._url(), role=auth.ROLE_VIEWER)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"], snapshot)
        self.get_status.assert_called_once_with(self.BASE_URL)

    def test_objects_checks_current_robot_before_and_after_reading(self) -> None:
        objects = {"objects": [{"id": 7, "type": "wall"}], "errors": {}}
        self.list_objects.return_value = objects
        _, response = self._request("GET", self._url("/objects"))
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"], objects)
        self.assertEqual(
            self.current_base.call_args_list, [call(self.BASE_URL), call(self.BASE_URL)]
        )
        self.list_objects.assert_called_once_with(self.BASE_URL)

    def test_objects_does_not_read_a_changed_robot(self) -> None:
        self.current_base.side_effect = handlers.RobotApiError("robot changed", 409)
        _, response = self._request("GET", self._url("/objects"))
        self.assertEqual(response["status"], 409)
        self.assertEqual(response["data"], {"error": "robot changed"})
        self.list_objects.assert_not_called()

    def test_objects_discards_result_when_robot_changes_during_read(self) -> None:
        self.current_base.side_effect = [
            self.BASE_URL, handlers.RobotApiError("robot changed during read", 409)
        ]
        self.list_objects.return_value = {
            "objects": [{"id": 7, "type": "wall", "name": "old robot object"}],
            "errors": {},
        }
        _, response = self._request("GET", self._url("/objects"))
        self.assertEqual(response["status"], 409)
        self.assertEqual(response["data"], {"error": "robot changed during read"})
        self.assertEqual(
            self.current_base.call_args_list, [call(self.BASE_URL), call(self.BASE_URL)]
        )
        self.list_objects.assert_called_once_with(self.BASE_URL)

    def test_export_preserves_binary_and_download_headers(self) -> None:
        binary = b"\x00STCM\xff\x80\r\n"
        self.export_map.return_value = binary
        handler, response = self._request("GET", self._url("/export"))
        self.assertEqual(response["status"], 200)
        self.assertEqual(handler.wfile.getvalue(), binary)
        self.assertEqual(response["headers"], {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="map.stcm"',
            "Content-Length": str(len(binary)),
            "Cache-Control": "no-store",
        })
        self.export_map.assert_called_once_with(self.BASE_URL)

    def test_unauthenticated_requests_do_not_reach_chassis(self) -> None:
        for suffix in ("", "/objects", "/export"):
            with self.subTest(method="GET", suffix=suffix):
                _, response = self._request("GET", self._url(suffix), role=None)
                self.assertEqual(response["status"], 401)
        handler, response = self._request("POST", self.ROUTE, {
            "command": "start", "expected_robot_base_url": self.BASE_URL,
        }, role=None)
        self.assertEqual(response["status"], 401)
        self.assertEqual(handler.rfile.read(), b"")
        self._assert_no_chassis_call()

    def test_post_requires_nonempty_expected_robot(self) -> None:
        for payload in (
            {"command": "start"},
            {"command": "start", "expected_robot_base_url": None},
            {"command": "start", "expected_robot_base_url": ""},
            {"command": "start", "expected_robot_base_url": " "},
            {"command": "start", "expected_robot_base_url": True},
        ):
            with self.subTest(payload=payload):
                _, response = self._request("POST", self.ROUTE, payload)
                self.assertEqual(response["status"], 400)
                self.assertIn("error", response["data"])
        self.execute.assert_not_called()

    def test_post_forwards_command_with_expected_robot(self) -> None:
        payload = {"command": "start", "expected_robot_base_url": self.BASE_URL}
        result = {"phase": "active", "mapping_enabled": True}
        self.execute.return_value = result
        handler, response = self._request("POST", self.ROUTE, payload)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"], result)
        self.execute.assert_called_once_with(payload)
        self.assertEqual(handler.rfile.read(), b"")

    def test_oversized_post_is_rejected_without_reading_body(self) -> None:
        handler, response = self._request(
            "POST", self.ROUTE, content_length=45 * 1024 * 1024 + 1
        )
        self.assertEqual(response["status"], 413)
        self.assertTrue(handler.close_connection)
        handler.rfile.read.assert_not_called()
        self._assert_no_chassis_call()

    def test_robot_error_status_survives_get_and_post(self) -> None:
        routes = (
            ("GET", self._url(), None, self.get_status),
            ("GET", self._url("/objects"), None, self.list_objects),
            ("GET", self._url("/export"), None, self.export_map),
            ("POST", self.ROUTE, {
                "command": "start", "expected_robot_base_url": self.BASE_URL,
            }, self.execute),
        )
        for method, url, payload, mocked in routes:
            for status in (409, 423, 504):
                with self.subTest(method=method, url=url, status=status):
                    mocked.side_effect = handlers.RobotApiError("chassis failure", status)
                    _, response = self._request(method, url, payload)
                    self.assertEqual(response["status"], status)
                    self.assertEqual(response["data"], {"error": "chassis failure"})


if __name__ == "__main__":
    unittest.main()
