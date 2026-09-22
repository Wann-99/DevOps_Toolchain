"""Exercise the fixed proxy against local HTTP/WebSocket devices, never hardware."""

import asyncio
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from ksq.web import auth
from ksq.web.app import create_app
from ksq.web.routes import arm


class ArmProxyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app())
        self.addCleanup(self.client.close)
        self.admin = auth.create_session({"username": "arm-check", "role": auth.ROLE_ADMIN})
        self.viewer = auth.create_session({"username": "arm-viewer", "role": auth.ROLE_VIEWER})
        self.addCleanup(auth.destroy_session, self.admin)
        self.addCleanup(auth.destroy_session, self.viewer)
        self.seen = []
        seen = self.seen

        class Device(BaseHTTPRequestHandler):
            def do_GET(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append((self.command, self.path, self.headers, body))
                status, mime, headers = 200, "application/json", []
                if self.path == "/":
                    mime = "text/html; charset=utf-8"
                    content = b'<html><head><script src="/js/app.js"></script></head><body>device</body></html>'
                elif self.path == "/js/app.js":
                    mime = "application/javascript"
                    content = b'const base=window.location.href.slice(0,window.location.href.indexOf("/#"))+":8090"; const image="/png/arm.png";'
                elif self.path == "/redirect":
                    status, content = 302, b""
                    headers = [("Location", "http://192.168.11.18:8090/login")]
                elif self.path == "/external":
                    status, content = 302, b""
                    headers = [("Location", "http://example.invalid/private")]
                elif self.path == "/download":
                    content, mime = b"\x00\xffbinary\n" * 10000, "application/octet-stream"
                else:
                    content = json.dumps({"path": self.path}).encode()
                    headers = [("Set-Cookie", "device_session=fixture; Path=/; HttpOnly"),
                               ("Set-Cookie", "ksq_session=device-fixture; Path=/")]
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(content)

            do_POST = do_GET
            do_PUT = do_GET
            do_PATCH = do_GET
            do_DELETE = do_GET

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Device)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.connections = []

        def connection(host, port, timeout):
            self.connections.append((host, port))
            return HTTPConnection(*self.server.server_address, timeout=timeout)

        self.patch = patch.object(arm, "HTTPConnection", side_effect=connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def headers(self, token=None, **extra):
        return {"Cookie": auth.SESSION_COOKIE + "=" + (token or self.admin), **extra}

    def test_login_role_and_cross_site_guards_precede_upstream(self):
        self.assertEqual(self.client.get("/arm/").status_code, 401)
        self.assertEqual(self.client.get("/arm/", headers=self.headers(**{"Sec-Fetch-Site": "cross-site"})).status_code, 403)
        self.assertEqual(self.client.post("/arm-api/command", headers=self.headers(), json={}).status_code, 403)
        self.assertEqual(self.client.post("/arm-api/command", headers=self.headers(Origin="http://elsewhere"), json={}).status_code, 403)
        self.assertFalse(self.connections)
        self.assertEqual(self.client.get("/arm/", headers=self.headers(self.viewer)).status_code, 200)

    def test_web_resources_redirects_and_device_identity(self):
        response = self.client.get("/arm", headers=self.headers(), follow_redirects=False)
        self.assertEqual(response.headers["location"], "/arm/")
        response = self.client.get("/arm/", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn('/arm/__ksq_bridge.js', response.text)
        self.assertIn('src="/arm/js/app.js"', response.text)
        response = self.client.get("/arm/js/app.js", headers=self.headers())
        self.assertIn('"http://192.168.11.18"+":8090"', response.text)
        self.assertIn('"/arm/png/arm.png"', response.text)
        self.assertEqual(self.connections, [("192.168.11.18", 80)] * 2)
        response = self.client.get("/arm/redirect", headers=self.headers(), follow_redirects=False)
        self.assertEqual(response.headers["location"], "/arm-api/login")
        self.assertEqual(self.client.get("/arm/external", headers=self.headers()).status_code, 502)

    def test_api_methods_raw_paths_binary_and_cookie_isolation(self):
        payload = b"\x00\xffmultipart-or-binary\r\n"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.client.request(method, "/arm-api/file%20name?x=a%2Fb", content=payload,
                                           headers=self.headers(Origin="http://testserver", **{"Content-Type": "application/octet-stream"}))
            self.assertEqual(response.status_code, 200)
            command, path, headers, actual = self.seen[-1]
            self.assertEqual((command, path, actual), (method, "/file%20name?x=a%2Fb", payload))
            self.assertEqual(headers.get("Origin"), "http://192.168.11.18")
            self.assertNotIn(self.admin, headers.get("Cookie", ""))
            self.assertIn("ksq_arm_device_session=fixture", response.headers["set-cookie"])
            self.assertTrue(all(v.startswith("ksq_arm_") for v in response.headers.get_list("set-cookie")))
        self.assertEqual(self.connections, [("192.168.11.18", 8090)] * 4)
        headers = self.headers()
        headers["Cookie"] += "; unrelated=private; ksq_arm_device_session=fixture"
        self.client.get("/arm-api/check", headers=headers)
        self.assertEqual(self.seen[-1][2].get("Cookie"), "device_session=fixture")
        response = self.client.get("/arm/download", headers=self.headers())
        self.assertEqual(response.content, b"\x00\xffbinary\n" * 10000)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_disconnection_and_upload_limit(self):
        with patch.object(arm, "HTTPConnection", side_effect=OSError("fixture unreachable")):
            response = self.client.get("/arm/", headers=self.headers())
            self.assertEqual(response.status_code, 502)
            self.assertIn("无法连接机械臂", response.text)
        with patch.object(arm, "MAX_UPLOAD", 3):
            response = self.client.post("/arm-api/upload", content=b"1234", headers=self.headers(Origin="http://testserver"))
            self.assertEqual(response.status_code, 413)
        self.assertFalse(self.seen)

    def test_websocket_guards_echo_and_session_revocation(self):
        ready, stop = threading.Event(), threading.Event()
        port, seen, failures = [], [], []

        async def echo(ws):
            seen.append(ws.request)
            try:
                async for message in ws:
                    await ws.send(message)
            except Exception:
                pass

        async def server():
            try:
                async with serve(echo, "127.0.0.1", 0, subprotocols=["binary"]) as service:
                    port.append(service.sockets[0].getsockname()[1])
                    ready.set()
                    await asyncio.to_thread(stop.wait)
            except BaseException as error:
                failures.append(error)
                ready.set()

        thread = threading.Thread(target=lambda: asyncio.run(server()), daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertFalse(failures)
            def local_connect(uri, **kwargs):
                self.assertEqual(uri, "ws://192.168.11.18:8060/events?channel=one")
                return connect(f"ws://127.0.0.1:{port[0]}/events?channel=one", **kwargs)
            with patch.object(arm, "connect", side_effect=local_connect) as upstream:
                for headers in (self.headers(self.viewer, Origin="https://elsewhere"), self.headers(Origin="https://elsewhere"), self.headers()):
                    with self.assertRaises(WebSocketDenialResponse):
                        with self.client.websocket_connect("/arm-ws/events?channel=one", headers=headers):
                            pass
                upstream.assert_not_called()
                with self.client.websocket_connect("/arm-ws/events?channel=one", headers=self.headers(self.viewer, Origin="http://testserver"), subprotocols=["binary"]) as ws:
                    self.assertEqual(ws.accepted_subprotocol, "binary")
                    ws.send_bytes(b"\x00\xff")
                    self.assertEqual(ws.receive_bytes(), b"\x00\xff")
                    ws.send_text("fixture")
                    self.assertEqual(ws.receive_text(), "fixture")
                    auth.destroy_session(self.viewer)
                    with self.assertRaises(WebSocketDisconnect) as raised:
                        ws.receive_text()
                    self.assertEqual(raised.exception.code, 1008)
            self.assertNotIn(self.admin, str(seen[0].headers))
            self.assertEqual(seen[0].headers["Origin"], "http://192.168.11.18")
        finally:
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
