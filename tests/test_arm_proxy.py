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
        self.arm_keys = {owner: auth.arm_session_token(owner) for owner in (self.admin, self.viewer)}
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
                elif self.path == "/logout":
                    content = b'{}'
                    headers = [("Set-Cookie", "device_session=; Path=/; Max-Age=0")]
                elif self.path == "/check":
                    content = b'{}'
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

    def url(self, path, token=None):
        prefix, _, tail = path.lstrip("/").partition("/")
        return "/" + prefix + "/" + self.arm_keys[token or self.admin] + "/" + tail

    def test_login_role_and_cross_site_guards_precede_upstream(self):
        self.assertEqual(self.client.get("/arm/").status_code, 401)
        self.assertEqual(self.client.get("/arm/", headers=self.headers(**{"Sec-Fetch-Site": "cross-site"})).status_code, 403)
        self.assertEqual(self.client.post("/arm-api/command", headers=self.headers(), json={}).status_code, 401)
        self.assertEqual(self.client.post(self.url("/arm-api/command"), headers=self.headers(Origin="http://elsewhere"), json={}).status_code, 403)
        self.assertFalse(self.connections)
        self.assertEqual(self.client.get("/arm/", headers=self.headers(self.viewer)).status_code, 200)

    def test_web_resources_redirects_and_device_identity(self):
        response = self.client.get("/arm", headers=self.headers(), follow_redirects=False)
        self.assertEqual(response.headers["location"], self.url("/arm/"))
        response = self.client.get("/arm/", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.url('/arm/__ksq_bridge.js'), response.text)
        self.assertIn('src="' + self.url('/arm/js/app.js') + '"', response.text)
        response = self.client.get(self.url("/arm/js/app.js"), headers=self.headers())
        self.assertIn('"http://192.168.11.18"+":8090"', response.text)
        self.assertIn('"' + self.url('/arm/png/arm.png') + '"', response.text)
        self.assertEqual(self.connections, [("192.168.11.18", 80)] * 2)
        response = self.client.get(self.url("/arm/redirect"), headers=self.headers(), follow_redirects=False)
        self.assertEqual(response.headers["location"], self.url("/arm-api/login"))
        self.assertEqual(self.client.get(self.url("/arm/external"), headers=self.headers()).status_code, 502)

    def test_api_methods_raw_paths_binary_and_cookie_isolation(self):
        payload = b"\x00\xffmultipart-or-binary\r\n"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = self.client.request(method, self.url("/arm-api/file%20name?x=a%2Fb"), content=payload,
                                           headers={"Origin": "null", "Content-Type": "application/octet-stream"})
            self.assertEqual(response.status_code, 200)
            command, path, headers, actual = self.seen[-1]
            self.assertEqual((command, path, actual), (method, "/file%20name?x=a%2Fb", payload))
            self.assertEqual(headers.get("Origin"), "http://192.168.11.18")
            self.assertNotIn(self.admin, headers.get("Cookie", ""))
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.connections, [("192.168.11.18", 8090)] * 4)
        headers = self.headers()
        headers["Cookie"] += "; unrelated=private"
        self.client.get(self.url("/arm-api/check"), headers=headers)
        self.assertIn("device_session=fixture", self.seen[-1][2].get("Cookie"))
        self.assertNotIn("unrelated", self.seen[-1][2].get("Cookie"))
        self.assertNotIn(self.admin, self.seen[-1][2].get("Cookie"))
        self.client.post(self.url("/arm-api/logout"), headers={"Origin": "null"})
        self.client.get(self.url("/arm-api/check"), headers={"Origin": "null"})
        self.assertNotIn("device_session", self.seen[-1][2].get("Cookie", ""))
        self.client.get(self.url("/arm-api/check", self.viewer), headers={"Origin": "null"})
        self.assertIsNone(self.seen[-1][2].get("Cookie"))
        response = self.client.get(self.url("/arm/download"), headers=self.headers())
        self.assertEqual(response.content, b"\x00\xffbinary\n" * 10000)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_disconnection_and_upload_limit(self):
        with patch.object(arm, "HTTPConnection", side_effect=OSError("fixture unreachable")):
            response = self.client.get("/arm/", headers=self.headers())
            self.assertEqual(response.status_code, 502)
            self.assertIn("无法连接机械臂", response.text)
        with patch.object(arm, "MAX_UPLOAD", 3):
            response = self.client.post(self.url("/arm-api/upload"), content=b"1234", headers={"Origin": "null"})
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
                for headers in (self.headers(self.viewer, Origin="https://elsewhere"), self.headers(Origin="https://elsewhere")):
                    with self.assertRaises(WebSocketDenialResponse):
                        with self.client.websocket_connect(self.url("/arm-ws/events?channel=one"), headers=headers):
                            pass
                upstream.assert_not_called()
                with self.client.websocket_connect(self.url("/arm-ws/events?channel=one", self.viewer), headers={"Origin": "null"}, subprotocols=["binary"]) as ws:
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

    def test_opaque_frame_capability_is_scoped_revocable_and_cors_limited(self):
        key = self.arm_keys[self.admin]
        self.assertNotEqual(key, self.admin)
        response = self.client.get(self.url("/arm/"), headers={"Origin": "null"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "null")
        policy = response.headers["content-security-policy"]
        self.assertIn("sandbox allow-scripts", policy)
        self.assertNotIn("allow-same-origin", policy)
        self.assertNotIn("/api/", policy)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.client.options(self.url("/arm-api/upload"), headers={"Origin": "null"}).status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me", headers={"Cookie": auth.SESSION_COOKIE + "=" + key}).status_code, 401)
        for path in ("/arm-api/", "/arm-api/invalid/command", "/arm/中文/"):
            self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 401)
        owner = auth.session_from_arm_token(key)
        self.assertIsNotNone(owner)
        self.client.post(self.url("/arm/__ksq_storage"), json={"device-state": "fixture"}, headers={"Origin": "null"})
        self.assertEqual(auth.session_from_arm_token(key)["arm_storage"], {"device-state": "fixture"})
        self.assertEqual(auth.session_from_arm_token(self.arm_keys[self.viewer])["arm_storage"], {})
        bridge = self.client.get(self.url("/arm/__ksq_bridge.js"), headers={"Origin": "null"})
        self.assertIn('"device-state": "fixture"', bridge.text)
        for content in (b'not-json', b'[]', b'{"bad": 1}'):
            self.assertEqual(self.client.post(self.url("/arm/__ksq_storage"), content=content,
                                             headers={"Origin": "null"}).status_code, 400)
        self.assertEqual(self.client.post(self.url("/arm/__ksq_storage"), content=b'X' * 65537,
                                         headers={"Origin": "null"}).status_code, 413)
        with patch.object(auth.time, "time", return_value=owner["expires"] + 1):
            self.assertIsNone(auth.session_from_arm_token(key))
        auth.destroy_session(self.admin)
        self.assertEqual(self.client.get(self.url("/arm/"), headers={"Origin": "null"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
