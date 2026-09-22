"""HTTP/streaming contracts across the FastAPI transport, without devices."""

import asyncio
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from starlette.requests import ClientDisconnect
from websockets.asyncio.server import unix_serve

from ksq.dashboard import service as dashboard, settings
from ksq.data import storage
from ksq.order import service as orders
from ksq.order.broker import OrderBrokerError
from ksq.robot import logs
from ksq.web import auth, files_api, host_files
from ksq.web.app import create_app
from ksq.web.asgi import ResourceStream
from ksq.web.routes import data


class ASGIContracts(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())  # Lifespan is tested separately with mocks.
        self.addCleanup(self.client.close)
        self.admin = auth.create_session({"username": "asgi-admin", "role": auth.ROLE_ADMIN})
        self.viewer = auth.create_session({"username": "asgi-viewer", "role": auth.ROLE_VIEWER})
        self.addCleanup(auth.destroy_session, self.admin)
        self.addCleanup(auth.destroy_session, self.viewer)

    def headers(self, token=None):
        return {"Cookie": auth.SESSION_COOKIE + "=" + (token or self.admin)}

    def test_health_login_and_unknown_endpoint_contracts(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/order/tasks").status_code, 401)
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")
        response = self.client.get("/api/unknown", headers=self.headers())
        self.assertEqual((response.status_code, response.json()), (404, {"error": "Endpoint not found"}))
        response = self.client.post("/api/auth/login", content="{")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        with patch.object(auth, "verify_credentials", return_value={"username": "asgi-login", "role": auth.ROLE_VIEWER}):
            response = self.client.post("/api/auth/login", json={"username": "asgi-login", "password": "fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.addCleanup(auth.destroy_session, self.client.cookies.get(auth.SESSION_COOKIE))
        self.assertEqual(self.client.get("/api/auth/me").json()["role"], auth.ROLE_VIEWER)
        with patch.object(files_api, "close_terminals"):
            self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_order_routing_permissions_and_errors(self):
        with patch.object(settings, "resolve_dashboard_mode", return_value="prod"), patch.object(orders, "list_tasks", return_value={"tasks": []}) as query:
            response = self.client.get("/api/order/tasks?mode=test&page=2", headers=self.headers(self.viewer))
            self.assertEqual(response.json(), {"tasks": []})
            self.assertEqual(query.call_args.kwargs["mode"], "prod")
            self.assertEqual(query.call_args.kwargs["page"], "2")
        self.assertEqual(self.client.put("/api/order/config", json={}, headers=self.headers(self.viewer)).status_code, 403)
        with patch.object(settings, "resolve_dashboard_mode", return_value="prod"):
            response = self.client.post("/api/order/tasks/task-1/manual-claim", json={}, headers=self.headers())
            self.assertEqual(response.status_code, 403)
        with patch.object(orders, "get_task_detail", side_effect=OrderBrokerError("denied", 403, {"client_secret": "fixture"})):
            response = self.client.get("/api/order/tasks/task-1", headers=self.headers())
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["upstream"]["client_secret"], "***")
        self.assertEqual(self.client.get("/api/order/tasks/a%2Fb", headers=self.headers()).status_code, 400)

    def test_multipart_repeated_files_and_permission_guard(self):
        def import_files(form):
            self.assertEqual([item.filename for item in form["files"]], ["one.json", "two.json"])
            self.assertEqual([item.file.read() for item in form["files"]], [b"{}", b"[]"])
            return {"ok": True}
        files = [("files", ("one.json", b"{}")), ("files", ("two.json", b"[]"))]
        with patch.object(data, "import_uploaded_files", side_effect=import_files) as upload:
            response = self.client.post("/api/import", files=files, headers=self.headers(self.viewer))
            self.assertEqual(response.status_code, 403)
            upload.assert_not_called()
            response = self.client.post("/api/import", files=files, headers=self.headers())
            self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))

    def test_viewer_page_access_and_write_boundaries(self):
        from ksq.web.routes import map as map_routes, mapping, orders as order_routes, test_orders
        for view in ("dashboard", "load", "query", "order", "order-ops", "test-order", "map", "arm", "logs", "files", "settings"):
            self.assertEqual(self.client.get("/", params={"view": view}, headers=self.headers(self.viewer)).status_code, 200)
        with patch.object(mapping.robot_mapping_api, "execute") as execute:
            for routes in (map_routes.ROUTES, mapping.ROUTES):
                for method in ("POST", "PUT"):
                    for path in routes.get(method, ()):
                        with self.subTest(method=method, path=path):
                            self.assertEqual(self.client.request(method, path, json={}, headers=self.headers(self.viewer)).status_code, 403)
            execute.assert_not_called()
            execute.return_value = {"ok": True}
            response = self.client.post("/api/map/mapping", json={"expected_robot_base_url": "http://192.0.2.1:1448", "command": "start"}, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            execute.assert_called_once()
        with patch.object(mapping.robot_mapping_api, "get_status", return_value={"phase": "idle"}):
            response = self.client.get("/api/map/mapping", params={"expected_robot_base_url": "http://192.0.2.1:1448"}, headers=self.headers(self.viewer))
            self.assertEqual(response.status_code, 200)
        with patch.object(dashboard, "save_dashboard_settings", return_value={"ok": True}) as save:
            response = self.client.post("/api/dashboard/keyboard", json={"mode": "prod"}, headers=self.headers(self.viewer))
            self.assertEqual(response.status_code, 403)
            save.assert_not_called()
            response = self.client.post("/api/dashboard/keyboard", json={"auto_confirm": True, "restart_robot": False}, headers=self.headers(self.viewer))
            self.assertEqual(response.status_code, 200)
        for path in ("/api/edit/save", "/api/edit/persist", "/api/import"):
            self.assertEqual(self.client.post(path, json={}, headers=self.headers(self.viewer)).status_code, 403)
        with patch.object(order_routes.order_api, "update_business_config", return_value={"ok": True}), \
                patch.object(test_orders.state, "require_full_data_source"), \
                patch.object(test_orders.test_order_api, "update_config", return_value={"ok": True}):
            for path in ("/api/order/business-config", "/api/test-order/config"):
                self.assertEqual(self.client.put(path, json={}, headers=self.headers(self.viewer)).status_code, 200)

    def test_sse_frames_and_generator_cleanup(self):
        closed = threading.Event()
        def events(*_args):
            try:
                yield {"event": "log", "id": "one", "data": {"text": "hello"}}
                yield {"event": "heartbeat", "data": {"ok": True}}
            finally:
                closed.set()
        with patch.object(logs, "stream_log_events", side_effect=events):
            response = self.client.get("/api/logs/stream?service=0&tail=50", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("hello", response.text)
        self.assertIn("event: heartbeat", response.text)
        self.assertTrue(closed.is_set())

    def test_background_owners_start_and_stop_once(self):
        with (
            patch.object(storage, "start_data_cleanup") as start_storage,
            patch.object(storage, "stop_data_cleanup") as stop_storage,
            patch.object(dashboard, "start_dashboard_monitor") as start_monitor,
            patch.object(dashboard, "stop_dashboard_monitor") as stop_monitor,
            patch.object(files_api, "close_terminals") as close_terminals,
            patch.object(logs, "_stop_log_followers") as stop_logs,
        ):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
            for operation in (start_storage, stop_storage, start_monitor, stop_monitor, close_terminals, stop_logs):
                operation.assert_called_once_with()

    def test_requests_and_unhandled_errors_are_logged(self):
        with self.assertNoLogs("ksq.http", level="INFO"):
            self.assertEqual(self.client.get("/api/health").status_code, 200)
            with patch.object(dashboard, "get_dashboard_monitor_snapshot", return_value={"ok": True}):
                response = self.client.get("/api/dashboard/status", headers=self.headers())
                self.assertEqual(response.status_code, 200)
        with self.assertLogs("ksq.http", level="DEBUG") as messages:
            self.client.get("/api/health?private_query=fixture")
        self.assertIn("path=/api/health status=200", "\n".join(messages.output))
        self.assertNotIn("private_query", "\n".join(messages.output))
        with patch.object(files_api, "close_terminals"), self.assertLogs("ksq.http", level="INFO") as messages:
            response = self.client.post("/api/auth/logout", headers=self.headers(self.viewer))
            self.assertEqual(response.status_code, 200)
        self.assertIn("method=POST path=/api/auth/logout status=200", "\n".join(messages.output))
        with self.assertLogs("ksq.http", level="WARNING") as messages:
            self.assertEqual(self.client.get("/api/dashboard/status").status_code, 401)
        self.assertIn("path=/api/dashboard/status status=401", "\n".join(messages.output))
        with patch.object(orders, "list_tasks", side_effect=RuntimeError("fixture failure")):
            with self.assertLogs("ksq.http", level="ERROR") as messages:
                with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                    self.client.get("/api/order/tasks", headers=self.headers())
        self.assertIn("RuntimeError: fixture failure", "\n".join(messages.output))

    def test_stream_closes_resource_when_client_disconnects(self):
        resource = io.BytesIO(b"payload")
        response = ResourceStream(iter([b"payload"]), resource.close)
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("disconnected")
        async def receive():
            return {"type": "http.disconnect"}
        with self.assertRaises(ClientDisconnect):
            asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        self.assertTrue(resource.closed)

    def test_file_upload_and_download_keep_binary_contents(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"KSQ_HOST_FILES_AGENT": "1", "KSQ_HOST_FILES_SOCKET": ""}):
            target = Path(directory) / "sample.bin"
            contents = b"\x00\xffbinary\n" * 1024
            response = self.client.post("/api/files/upload", params={"path": directory, "name": target.name}, content=contents,
                                        headers={**self.headers(), "X-KSQ-Request": "1"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(target.read_bytes(), contents)
            response = self.client.get("/api/files/download", params={"path": str(target)}, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, contents)
            self.assertIn("attachment", response.headers["content-disposition"])

    def test_malformed_multipart_returns_the_existing_error_shape(self):
        response = self.client.post("/api/import", content=b"invalid", headers={**self.headers(), "Content-Type": "multipart/form-data"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_websocket_permissions(self):
        for token, origin in ((self.viewer, "https://other.invalid"), (self.admin, "https://other.invalid")):
            with self.subTest(token_role=token == self.admin):
                with self.assertRaises(WebSocketDenialResponse) as raised:
                    with self.client.websocket_connect("/desktop/websockify", headers={**self.headers(token), "Origin": origin}):
                        pass
                self.assertEqual(raised.exception.status_code, 403)

    def test_unix_websocket_binary_text_and_logout_revocation(self):
        ready, stop = threading.Event(), threading.Event()
        failures, requests = [], []
        with tempfile.TemporaryDirectory(prefix="ksq-ws-") as directory:
            socket_path = str(Path(directory) / "host.sock")
            async def echo(websocket):
                requests.append(websocket.request)
                await websocket.send(b"RFB fixture")
                try:
                    async for message in websocket:
                        await websocket.send(message)
                except Exception:
                    pass
            async def serve():
                try:
                    async with unix_serve(echo, socket_path, subprotocols=["binary"]):
                        ready.set()
                        await asyncio.to_thread(stop.wait)
                except BaseException as error:
                    failures.append(error)
                    ready.set()
            thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertFalse(failures)
                with patch.dict(os.environ, {"KSQ_HOST_FILES_SOCKET": socket_path}), patch.object(files_api, "close_terminals"):
                    with self.client.websocket_connect("/desktop/websockify", headers={**self.headers(self.viewer), "Origin": "http://testserver"}, subprotocols=["binary"]) as websocket:
                        self.assertEqual(websocket.accepted_subprotocol, "binary")
                        self.assertEqual(websocket.receive_bytes(), b"RFB fixture")
                        websocket.send_bytes(b"\x00\xff")
                        self.assertEqual(websocket.receive_bytes(), b"\x00\xff")
                        websocket.send_text("message")
                        self.assertEqual(websocket.receive_text(), "message")
                        self.assertEqual(self.client.post("/api/auth/logout", headers=self.headers(self.viewer)).status_code, 200)
                        with self.assertRaises(WebSocketDisconnect) as raised:
                            websocket.receive_bytes()
                        self.assertEqual(raised.exception.code, 1008)
                self.assertEqual(requests[0].path, "/desktop/websockify")
                self.assertEqual(requests[0].headers["Cookie"], auth.SESSION_COOKIE + "=" + host_files.owner_key(self.viewer))
            finally:
                stop.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
