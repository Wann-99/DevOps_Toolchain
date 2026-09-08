"""Run with Python 3.12: python -m tests.test_host_files."""

import base64
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

from ksq.web import auth, files_api, host_files
from ksq.web.handlers import QueryHandler


def check():
    class QuietHandler(QueryHandler):
        def log_message(self, *_args):
            pass

    with tempfile.TemporaryDirectory(prefix="ksq-host-check-") as temporary:
        root = Path(temporary)
        socket_path = root / ("long-deployment-path-" * 6) / "bridge/host.sock"
        host = subprocess.Popen([sys.executable, "-m", "ksq.web.host_files", "serve",
                                 "--socket", str(socket_path)], stdout=subprocess.DEVNULL)
        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        admin = auth.create_session({"username": "host-check", "role": auth.ROLE_ADMIN})
        other = auth.create_session({"username": "host-other", "role": auth.ROLE_ADMIN})
        viewer = auth.create_session({"username": "host-viewer", "role": auth.ROLE_VIEWER})

        def request(method, endpoint, payload=None, token=admin, headers=None):
            body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
            values = {"Cookie": auth.SESSION_COOKIE + "=" + token, "X-KSQ-Request": "1"}
            values.update(headers or {})
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(method, endpoint, body, values)
                response = connection.getresponse()
                data = response.read()
                if "application/json" in response.getheader("Content-Type", ""):
                    data = json.loads(data)
                return response.status, data
            finally:
                connection.close()

        def file_url(action, **values):
            return "/api/files/" + action + "?" + urlencode(values)

        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and host.poll() is None and time.monotonic() < deadline:
                time.sleep(0.03)
            assert socket_path.exists() and host.poll() is None
            assert host_files.control(socket_path, "status")["user"] == pwd.getpwuid(os.getuid()).pw_name
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
            direct = host_files.HostConnection(socket_path)
            direct.request("GET", "/api/files/list")
            assert direct.getresponse().status == 403
            direct.close()
            with patch.dict(os.environ, {"KSQ_HOST_FILES_SOCKET": str(socket_path), "KSQ_DESKTOP_URL": "http://localhost:6080/vnc.html"}), \
                    patch.object(files_api, "list_directory", side_effect=AssertionError("Must list on host")), \
                    patch.object(files_api, "rename_entry", side_effect=AssertionError("Must rename on host")), \
                    patch.object(files_api, "TerminalSession", side_effect=AssertionError("Must spawn on host")):
                listing = file_url("list", path=str(root))
                assert request("GET", listing, token="")[0] == 401
                assert request("GET", listing, token=viewer)[0] == 403
                assert request("GET", listing, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
                code, data = request("GET", listing)
                assert code == 200 and data["environment"] == "宿主机"
                assert data["home"] == str(Path.home())
                assert data["desktop_url"] == "http://localhost:6080/vnc.html"
                assert data["desktop_error"] == ""
                destination = file_url("upload", path=str(root), name="sub/中文.txt")
                content = "宿主机文件\n".encode()
                assert request("POST", destination, content, headers={"Origin": "https://example.invalid"})[0] == 403
                assert request("POST", destination, content, headers={"X-KSQ-Request": ""})[0] == 403
                assert request("POST", destination, content)[0] == 200
                assert request("POST", destination, b"overwrite")[0] == 409
                assert (root / "sub/中文.txt").read_bytes() == content
                assert request("POST", file_url("upload", path=str(root), name="../escape"), b"x")[0] == 400
                assert request("GET", file_url("preview", path=str(root / "sub/中文.txt")))[1]["text"] == content.decode()
                assert request("GET", file_url("download", path=str(root / "sub/中文.txt")))[1] == content
                code, archive = request("GET", file_url("download", path=str(root / "sub")))
                assert code == 200
                with zipfile.ZipFile(io.BytesIO(archive)) as source:
                    assert source.read("sub/中文.txt") == content

                rename = {"path": str(root / "sub"), "name": "中文.txt", "new_name": "重命名.txt"}
                assert request("POST", "/api/files/rename", rename, token=viewer)[0] == 403
                assert request("POST", "/api/files/rename", rename, headers={"Origin": "https://example.invalid"})[0] == 403
                assert request("POST", "/api/files/rename", rename, headers={"X-KSQ-Request": ""})[0] == 403
                assert request("POST", "/api/files/rename", rename)[0] == 200
                assert (root / "sub/重命名.txt").read_bytes() == content and not (root / "sub/中文.txt").exists()
                assert request("POST", "/api/files/rename", dict(rename, name="重命名.txt", new_name="中文.txt"))[0] == 200

                code, data = request("POST", "/api/terminal/create", {})
                assert code == 200
                identifier = data["id"]
                output_url = "/api/terminal/output?" + urlencode({"id": identifier})
                assert request("GET", output_url, token=other)[0] == 404
                assert request("GET", output_url)[1]["cwd"] == str(Path.home())
                command = "stty -echo; cd " + shlex.quote(str(root)) + "; ls --color=never sub\r"
                assert request("POST", "/api/terminal/input", {"id": identifier, "data": command})[0] == 200
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    code, data = request("GET", output_url)
                    if "中文.txt".encode() in base64.b64decode(data["data"]):
                        break
                    time.sleep(0.03)
                else:
                    raise AssertionError("Host terminal did not list the Unicode filename")
                assert data["cwd"] == str(root)
                assert request("POST", "/api/auth/logout", b"")[0] == 200
                direct = host_files.HostConnection(socket_path)
                direct.request("GET", output_url, headers={"Cookie": auth.SESSION_COOKIE + "=" + host_files.owner_key(admin)})
                assert direct.getresponse().status == 404
                direct.close()
                host.terminate()
                host.wait(timeout=5)
                code, data = request("GET", listing, token=other)
                assert code == 503 and "宿主机连接不可用" in data["error"]
        finally:
            if host.poll() is None:
                host.terminate()
                host.wait(timeout=5)
            for token in (admin, other, viewer):
                auth.destroy_session(token)
            server.shutdown()
            server.server_close()
            thread.join()
    print("Host bridge checks passed: private socket, real host files/PTY, transfers, Unicode, auth, isolation, logout, no container fallback.")


if __name__ == "__main__":
    check()
