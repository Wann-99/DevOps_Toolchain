"""Run on Linux with the desktop packages: python -m tests.test_browser_desktop."""

import base64
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode

from ksq.web import auth
from ksq.web.handlers import QueryHandler


def check():
    class QuietHandler(QueryHandler):
        def log_message(self, *_args):
            pass

    with tempfile.TemporaryDirectory(prefix="ksq-desktop-check-") as temporary:
        path = Path(temporary) / "host/host.sock"
        host = subprocess.Popen([sys.executable, "-m", "ksq.web.host_files", "serve", "--socket", str(path)])
        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        admin = auth.create_session({"username": "desktop-check", "role": auth.ROLE_ADMIN})
        viewer = auth.create_session({"username": "desktop-viewer", "role": auth.ROLE_VIEWER})
        authority = "%s:%s" % server.server_address

        def request(method, endpoint, payload=None, token=admin, headers=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=25)
            values = {"Cookie": auth.SESSION_COOKIE + "=" + token, "X-KSQ-Request": "1"}
            values.update(headers or {})
            try:
                connection.request(method, endpoint, json.dumps(payload) if payload is not None else None, values)
                response = connection.getresponse()
                body = response.read()
                if "application/json" in response.getheader("Content-Type", ""):
                    body = json.loads(body)
                return response.status, body
            finally:
                connection.close()

        try:
            deadline = time.monotonic() + 5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert path.exists()
            with patch.dict(os.environ, {"KSQ_HOST_FILES_SOCKET": str(path), "KSQ_DESKTOP_URL": ""}):
                assert request("GET", "/desktop/vnc.html", token="")[0] == 302
                assert request("GET", "/desktop/vnc.html", token=viewer)[0] == 403
                assert request("GET", "/desktop/vnc.html", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
                assert request("POST", "/desktop/vnc.html", {})[0] == 400
                assert request("GET", "/api/files/list")[1]["desktop_url"].startswith("/desktop/")
                status, terminal = request("POST", "/api/terminal/create", {})
                assert status == 200, terminal
                identifier = terminal["id"]
                children = Path(f"/proc/{host.pid}/task/{host.pid}/children").read_text().split()
                assert "tint2" in {Path("/proc", child, "comm").read_text().strip() for child in children}
                command = "stty -echo; xclock -digital & gui=$!; sleep 0.3; test -n \"$DISPLAY\" && test -r \"$XAUTHORITY\" && kill -0 $gui && printf 'gui-%s\\n' ready\r"
                assert request("POST", "/api/terminal/input", {"id": identifier, "data": command})[0] == 200
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    output = request("GET", "/api/terminal/output?" + urlencode({"id": identifier}))[1]
                    if b"gui-ready" in base64.b64decode(output["data"]):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("A GUI application failed to start from the web terminal")
                status, page = request("GET", "/desktop/vnc.html")
                assert status == 200 and b"noVNC" in page, (status, page)
                status, script = request("GET", "/desktop/core/rfb.js")
                assert status == 200 and b"RFB" in script
                status, stylesheet = request("GET", "/desktop/app/styles/base.css")
                assert status == 200 and b"KSQ desktop" in stylesheet
                handshake = {"Upgrade": "websocket", "Connection": "Upgrade",
                             "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
                             "Sec-WebSocket-Version": "13", "Sec-WebSocket-Protocol": "binary"}
                assert request("GET", "/desktop/websockify", headers=handshake)[0] == 403
                assert request("GET", "/desktop/websockify", headers=dict(handshake, Origin="https://example.invalid"))[0] == 403
                with socket.create_connection(server.server_address, timeout=5) as client:
                    headers = dict(handshake, Host=authority, Origin="http://" + authority,
                                   Cookie=auth.SESSION_COOKIE + "=" + admin)
                    client.sendall(("GET /desktop/websockify HTTP/1.1\r\n" +
                                    "".join(key + ": " + value + "\r\n" for key, value in headers.items()) + "\r\n").encode())
                    response = b""
                    while not response.endswith(b"\r\n\r\n"):
                        chunk = client.recv(1)
                        assert chunk, response
                        response += chunk
                    assert response.startswith(b"HTTP/1.1 101"), response
                    banner = b""
                    while b"RFB 003.008\n" not in banner:
                        chunk = client.recv(1024)
                        assert chunk and len(banner) < 1024, banner
                        banner += chunk
                    assert request("POST", "/api/auth/logout", {})[0] == 200
                    assert client.recv(1024) == b"", "Logout must revoke an already connected desktop"
                output_url = "/api/terminal/output?" + urlencode({"id": identifier})
                assert request("GET", output_url)[0] == 401
        except BaseException:
            log = path.with_name("desktop.log")
            if log.exists():
                print(log.read_text(errors="replace")[-6000:])
            raise
        finally:
            host.terminate()
            host.wait(timeout=10)
            server.shutdown()
            server.server_close()
            thread.join()
            auth.destroy_session(admin)
            auth.destroy_session(viewer)
    print("Browser desktop checks passed: real virtual display, noVNC assets, WebSocket/RFB, authentication, origin and logout.")


if __name__ == "__main__":
    check()
