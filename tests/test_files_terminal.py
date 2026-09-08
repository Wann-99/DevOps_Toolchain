"""Run with Python 3.12: python -m tests.test_files_terminal."""

import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import time
from urllib.parse import urlencode
import zipfile

from ksq.web import auth, files_api
from ksq.web.handlers import QueryHandler


def check():
    class QuietHandler(QueryHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    admin = auth.create_session({"username": "file-check", "role": auth.ROLE_ADMIN})
    other = auth.create_session({"username": "file-check", "role": auth.ROLE_ADMIN})
    viewer = auth.create_session({"username": "viewer-check", "role": auth.ROLE_VIEWER})

    def request(method, endpoint, payload=None, token=admin, headers=None):
        body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        request_headers = {"Cookie": auth.SESSION_COOKIE + "=" + token, "X-KSQ-Request": "1"}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(method, endpoint, body=body, headers=request_headers)
            response = connection.getresponse()
            if endpoint.startswith(("/api/files/", "/api/terminal/")):
                assert response.getheader("Cache-Control") == "no-store"
            content = response.read()
            if "application/json" in response.getheader("Content-Type", ""):
                content = json.loads(content)
            return response.status, content
        finally:
            connection.close()

    def url(action, **params):
        return "/api/files/" + action + "?" + urlencode(params)

    def expect_failure(function, exception):
        try:
            function()
        except exception:
            return
        raise AssertionError("Expected " + exception.__name__)

    try:
        with tempfile.TemporaryDirectory(prefix="ksq-files-check-") as directory:
            root = Path(directory)
            target = url("upload", path=directory, name="folder/sub/中文.txt")
            content = "测试 UTF-8\n<script>alert(1)</script>".encode()
            assert request("GET", url("list", path=directory), token="")[0] == 401
            assert request("GET", url("list", path=directory), token=viewer)[0] == 403
            assert request("POST", target, content, token=viewer)[0] == 403
            assert request("POST", target, content, headers={"X-KSQ-Request": ""})[0] == 403
            assert request("POST", target, content, headers={"Origin": "https://example.invalid"})[0] == 403
            assert request("GET", url("list", path=directory), headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert request("POST", target, content)[0] == 200
            assert request("POST", target, b"replacement")[0] == 409
            uploaded = root / "folder/sub/中文.txt"
            assert uploaded.read_bytes() == content
            assert request("GET", url("preview", path=str(uploaded)))[1]["text"] == content.decode()
            assert request("GET", url("download", path=str(uploaded)))[1] == content
            (root / "folder/empty").mkdir()
            code, body = request("GET", url("download", path=str(root / "folder")))
            assert code == 200
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                assert archive.read("folder/sub/中文.txt") == content
                assert "folder/empty/" in archive.namelist()
            code, listing = request("GET", url("list", path=directory))
            assert code == 200 and listing["path"] == directory
            assert listing["entries"][0]["name"] == "folder"
            for name in ["../escape", "/escape", "a/../../escape", "a\\b", "a//b", "a/./b"]:
                assert request("POST", url("upload", path=directory, name=name), b"x")[0] == 400
            assert request("POST", url("upload", path=directory, name="zero"), b"")[0] == 200
            assert (root / "zero").stat().st_size == 0
            expect_failure(lambda: files_api.upload_file(directory, "partial", io.BytesIO(b"x"), 2), ValueError)
            assert not (root / "partial").exists()
            assert not list(root.glob(".ksq-upload-*"))
            (root / "alias").symlink_to(root / "folder", target_is_directory=True)
            assert request("POST", url("upload", path=directory, name="alias/redirect"), b"x")[0] == 400
            assert not (root / "folder/redirect").exists()
            assert request("GET", url("download", path=directory))[0] == 400
            os.mkfifo(root / "fifo")
            assert request("GET", url("preview", path=str(root / "fifo")))[0] == 400
            assert request("GET", url("image", path=str(uploaded)))[0] == 400
            large = root / "large.txt"
            large.write_bytes(b"a" * files_api.PREVIEW_LIMIT + b"tail")
            assert request("GET", url("preview", path=str(large)))[1]["truncated"]

            rename = "/api/files/rename"
            rename_root = root / "rename"
            rename_root.mkdir()
            original = rename_root / "原文件.txt"
            original.write_bytes(content)
            payload = {"path": str(rename_root), "name": original.name, "new_name": "空 格'文件.txt"}
            assert request("POST", rename, payload, token="")[0] == 401
            assert request("POST", rename, payload, token=viewer)[0] == 403
            assert request("POST", rename, payload, headers={"X-KSQ-Request": ""})[0] == 403
            assert request("POST", rename, payload, headers={"Origin": "https://example.invalid"})[0] == 403
            assert request("POST", rename, payload, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
            for invalid in [None, 1, "", " ", ".", "..", "/escape", "../escape", "a/b", "a\\b", "nul\x00"]:
                assert request("POST", rename, dict(payload, new_name=invalid))[0] == 400
                assert request("POST", rename, dict(payload, name=invalid))[0] == 400
            assert original.read_bytes() == content
            assert request("POST", rename, dict(payload, new_name=original.name))[0] == 200
            assert request("POST", rename, payload)[0] == 200
            assert not original.exists() and (rename_root / payload["new_name"]).read_bytes() == content
            assert request("POST", rename, payload)[0] == 404
            occupied = rename_root / "occupied"
            occupied.write_bytes(b"keep")
            conflict = dict(payload, name=payload["new_name"], new_name=occupied.name)
            assert request("POST", rename, conflict)[0] == 409
            assert occupied.read_bytes() == b"keep" and (rename_root / payload["new_name"]).read_bytes() == content
            folder = rename_root / "folder"
            folder.mkdir()
            (folder / "child.txt").write_bytes(content)
            assert request("POST", rename, dict(payload, name="folder", new_name="中文目录"))[0] == 200
            assert (rename_root / "中文目录/child.txt").read_bytes() == content and not folder.exists()
            folder.mkdir()
            assert request("POST", rename, dict(payload, name="中文目录", new_name="folder"))[0] == 409
            assert not list(folder.iterdir()) and (rename_root / "中文目录/child.txt").read_bytes() == content
            (rename_root / "link").symlink_to(occupied)
            assert request("POST", rename, dict(payload, name="link", new_name="renamed-link"))[0] == 200
            assert (rename_root / "renamed-link").is_symlink() and occupied.read_bytes() == b"keep"
            (rename_root / "broken").symlink_to(rename_root / "missing")
            assert request("POST", rename, dict(payload, name="occupied", new_name="broken"))[0] == 409
            assert (rename_root / "broken").is_symlink() and occupied.read_bytes() == b"keep"
            barrier = threading.Barrier(2)

            def race(name):
                (rename_root / name).write_text(name)
                barrier.wait(timeout=5)
                return request("POST", rename, dict(payload, name=name, new_name="winner"))[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(race, ("first", "second"))) == [200, 409]
            winner = (rename_root / "winner").read_text()
            loser = "second" if winner == "first" else "first"
            assert not (rename_root / winner).exists() and (rename_root / loser).read_text() == loser

            create = "/api/terminal/create"
            assert request("POST", create, {"path": directory}, token=viewer)[0] == 403
            assert request("POST", create, {"path": directory}, headers={"X-KSQ-Request": ""})[0] == 403
            assert request("POST", create, {"path": directory, "rows": True})[0] == 400
            code, result = request("POST", create, {"path": directory, "cols": 91, "rows": 31})
            assert code == 200
            identifier = result["id"]
            terminal = files_api.get_terminal(identifier, admin)
            assert terminal.output(0)["cwd"] == directory
            assert request("GET", "/api/terminal/output?" + urlencode({"id": identifier}), token=other)[0] == 404
            assert request("POST", "/api/terminal/input", {"id": identifier, "data": "echo blocked\n"}, token=other)[0] == 404

            def input_text(data):
                assert request("POST", "/api/terminal/input", {"id": identifier, "data": data, "cols": 91, "rows": 31})[0] == 200

            cursor = 0

            def wait_for(marker):
                nonlocal cursor
                deadline = time.monotonic() + 5
                collected = b""
                while time.monotonic() < deadline:
                    code, output = request("GET", "/api/terminal/output?" + urlencode({"id": identifier, "cursor": cursor}))
                    assert code == 200
                    cursor = output["cursor"]
                    collected += base64.b64decode(output["data"])
                    if marker in collected:
                        return
                    time.sleep(0.03)
                raise AssertionError("Terminal did not emit expected output")

            input_text("stty -echo\r")
            time.sleep(0.1)
            input_text("printf 'pty-%s\\n' ready; stty size\r")
            wait_for(b"31 91")
            input_text("ls --color=never folder/sub\r")
            wait_for("中文.txt".encode())
            input_text("cd folder; printf 'cwd:%s\\n' \"$PWD\"\r")
            wait_for(("cwd:" + directory + "/folder").encode())
            assert terminal.output(cursor)["cwd"] == str(root / "folder")
            unusual_directory = root / "folder" / "空 格'目录"
            unusual_directory.mkdir()
            input_text("cd " + shlex.quote(str(unusual_directory)) + "; printf 'changed-%s\\n' directory\r")
            wait_for(b"changed-directory")
            assert terminal.output(cursor)["cwd"] == str(unusual_directory)
            input_text("sleep 30\r")
            time.sleep(0.1)
            input_text("\x03")
            input_text("printf 'interrupt-%s\\n' ready\r")
            wait_for(b"interrupt-ready")
            with terminal.lock:
                terminal.buffer = bytearray(b"retained")
                terminal.offset = 100
            assert terminal.output(0)["truncated"]
            input_text("exit\r")
            deadline = time.monotonic() + 5
            while not terminal.closed and time.monotonic() < deadline:
                time.sleep(0.03)
            assert terminal.closed and terminal.process.poll() is not None
            assert terminal.output(0)["cwd"] is None
            code, result = request("POST", create, {"path": directory})
            assert code == 200
            terminal = files_api.get_terminal(result["id"], admin)
            terminal.input({"data": "sleep 60 & echo $! > background.pid\r"})
            deadline = time.monotonic() + 5
            while not (root / "background.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.03)
            background_pid = int((root / "background.pid").read_text())
            assert request("POST", "/api/auth/logout", b"")[0] == 200
            assert terminal.closed and terminal.process.poll() is not None
            process_stat = Path("/proc") / str(background_pid) / "stat"
            deadline = time.monotonic() + 5
            while process_stat.exists() and time.monotonic() < deadline:
                if process_stat.read_text().split(")", 1)[1].strip().startswith("Z"):
                    break
                time.sleep(0.03)
            assert not process_stat.exists() or process_stat.read_text().split(")", 1)[1].strip().startswith("Z")
            expect_failure(lambda: files_api.get_terminal(result["id"], admin), FileNotFoundError)
    finally:
        files_api.close_terminals()
        for token in (admin, other, viewer):
            auth.destroy_session(token)
        server.shutdown()
        server.server_close()
        thread.join()
    print("File and PTY checks passed: transfers, previews, atomic renames, access control, isolation, interactive input, cleanup.")


if __name__ == "__main__":
    check()
