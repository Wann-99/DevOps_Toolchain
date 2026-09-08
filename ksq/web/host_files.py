"""Host file/PTY bridge over a private Unix socket, using only the stdlib."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import pwd
import select
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from ksq.web import auth, desktop, files_api

_DESKTOP_CONNECTIONS = threading.BoundedSemaphore(8)


class UpgradeResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        super().__init__(sock, *args, **kwargs)
        self.fp.close()
        # Do not read ahead into the first WebSocket frame while parsing HTTP headers.
        self.fp = sock.makefile("rb", buffering=0)


class HostConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            path = Path(self.path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # Linux Unix socket names have a short limit; deployment paths may be long.
                self.sock.connect(f"/proc/self/fd/{directory}/{path.name}")
            finally:
                os.close(directory)
        except BaseException:
            self.sock.close()
            raise


def owner_key(owner):
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def control(path, action, owner=None):
    connection = HostConnection(path, timeout=5)
    try:
        connection.request("POST", "/" + action, json.dumps({"owner": owner}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        if response.status != 200:
            raise OSError(data.get("error", "宿主机连接失败。"))
        return data
    finally:
        connection.close()


def close_terminals(owner=None):
    path = os.environ.get("KSQ_HOST_FILES_SOCKET")
    if path:
        with contextlib.suppress(OSError, http.client.HTTPException):
            control(path, "close-terminals", owner_key(owner) if owner is not None else None)


def forward(handler, owner, desktop_target=False):
    connection = HostConnection(desktop.connection_path() if desktop_target else os.environ["KSQ_HOST_FILES_SOCKET"])
    upgrade = handler.path.startswith("/desktop/") and handler.headers.get("Upgrade", "").lower() == "websocket"
    if upgrade and not _DESKTOP_CONNECTIONS.acquire(blocking=False):
        handler._send_json(503, {"error": "桌面连接已达上限，请关闭其他桌面标签页。"})
        return
    sent_headers = False
    try:
        connection.putrequest(handler.command, handler.path[len("/desktop"):] if desktop_target else handler.path)
        connection.putheader("Cookie", auth.SESSION_COOKIE + "=" + owner_key(owner))
        connection.putheader("X-KSQ-Request", "1")
        if files_api.desktop_url():
            connection.putheader("X-KSQ-Desktop-Mode", "external")
        if upgrade:
            connection.response_class = UpgradeResponse
            connection.putheader("Connection", "Upgrade")
            connection.putheader("Upgrade", "websocket")
            for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Protocol"):
                if handler.headers.get(name):
                    connection.putheader(name, handler.headers[name])
        remaining = 0
        if handler.command == "POST":
            remaining = int(handler.headers.get("Content-Length", "-1"))
            limit = files_api.MAX_TRANSFER if handler.path.split("?", 1)[0] == "/api/files/upload" else 128 * 1024
            if not 0 <= remaining <= limit:
                raise ValueError("请求体大小无效。")
            connection.putheader("Content-Length", str(remaining))
            connection.putheader("Content-Type", handler.headers.get("Content-Type", "application/json"))
            handler.connection.settimeout(120)
        connection.endheaders()
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("上传中断，未保存不完整文件。")
            connection.send(chunk)
            remaining -= len(chunk)
        response = connection.getresponse()
        if response.status == 200 and urlsplit(handler.path).path == "/api/files/list":
            data = json.loads(response.read())
            data["desktop_url"] = files_api.desktop_url() or data.get("desktop_url", "")
            if files_api.desktop_url():
                data["desktop_error"] = ""
            sent_headers = True
            handler._send_json(200, data)
            return
        handler.send_response(response.status)
        for name in ("Content-Type", "Content-Length", "Content-Disposition", "Cache-Control",
                     "Content-Security-Policy", "X-Content-Type-Options", "Upgrade", "Connection",
                     "Sec-WebSocket-Accept", "Sec-WebSocket-Protocol"):
            value = response.getheader(name)
            if value is not None:
                handler.send_header(name, value)
        if handler.path.startswith("/desktop/"):
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Frame-Options", "SAMEORIGIN")
        handler.end_headers()
        sent_headers = True
        if response.status == 101:
            handler.wfile.flush()
            client, upstream = handler.connection, connection.sock
            client.settimeout(10)
            upstream.settimeout(10)
            while True:
                if not desktop_target:
                    session = auth.get_session(owner)
                    if session is None or session.get("role") != auth.ROLE_ADMIN:
                        break
                readable, _, _ = select.select([client, upstream], [], [], 1)
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    (upstream if source is client else client).sendall(chunk)
            return
        if upgrade and response.fp is not None:
            response.fp = io.BufferedReader(response.fp)
        while chunk := response.read(1024 * 1024):
            handler.wfile.write(chunk)
    except (OSError, http.client.HTTPException):
        if not sent_headers:
            handler._send_json(503, {"error": "宿主机连接不可用，请在宿主机部署目录执行 bash start.sh host-files start。"})
        handler.close_connection = True
    finally:
        connection.close()
        if upgrade:
            _DESKTOP_CONNECTIONS.release()


class HostHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path in {"/status", "/shutdown", "/close-terminals"}:
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("请求体大小无效。")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or (data.get("owner") is not None and not isinstance(data["owner"], str)):
                    raise ValueError("请求体格式无效。")
            except (ValueError, TypeError):
                self._send_json(400, {"error": "请求体格式无效。"})
                return
            if self.path == "/close-terminals":
                files_api.close_terminals(data.get("owner"))
            self._send_json(200, {"pid": os.getpid(), "user": pwd.getpwuid(os.getuid()).pw_name,
                                  "home": str(Path.home()), "hostname": socket.gethostname()})
            if self.path == "/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.do_GET()

    def do_GET(self):
        owner = auth.token_from_cookie(self.headers.get("Cookie", ""))
        if len(owner) != 64 or any(char not in "0123456789abcdef" for char in owner):
            self._send_json(403, {"error": "需要通过已登录的文件管理页面访问。"})
            return
        if not files_api.handle_request(self, {"role": auth.ROLE_ADMIN}):
            self._send_json(404, {"error": "接口不存在。"})

    def log_message(self, *_args):
        pass


class HostServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def server_bind(self):
        path = Path(self.server_address)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.socket.bind(f"/proc/self/fd/{directory}/{path.name}")
        finally:
            os.close(directory)


def serve(path):
    os.environ.pop("KSQ_HOST_FILES_SOCKET", None)
    os.environ["KSQ_HOST_FILES_AGENT"] = "1"
    if Path("/.dockerenv").exists():
        raise RuntimeError("宿主机连接进程必须在宿主机启动，不能在应用容器内启动。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("宿主机连接目录必须属于运行账号，且权限为 700。")
    lock_path = path.parent / "agent.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError("连接路径已存在且不是套接字。")
            path.unlink()
        os.umask(0o077)
        desktop.configure(path.parent)
        with HostServer(str(path), HostHandler) as server:
            path.chmod(0o600)
            def stop(_signal, _frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            try:
                server.serve_forever(poll_interval=0.1)
            finally:
                files_api.close_terminals()
                desktop.close()
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="宿主机文件与终端连接")
    parser.add_argument("action", choices=("start", "stop", "status", "serve"))
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    path = Path(args.socket).expanduser().resolve()
    if args.action == "serve":
        serve(path)
        return
    if args.action == "status":
        print(json.dumps(control(path, "status"), ensure_ascii=False))
        return
    if path.exists():
        try:
            control(path, "shutdown")
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        else:
            deadline = time.monotonic() + 10
            while path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if path.exists():
                raise RuntimeError("旧宿主机连接未退出，请检查 host-files/agent.log。")
    if args.action == "stop":
        print("宿主机连接已停止。")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The package is importable as a zip; the host does not need the web runtime or cgi.
    source = str(Path(__file__).resolve().parents[2])
    command = [sys.executable, "-c",
               "import sys;sys.path.insert(0,sys.argv.pop(1));from ksq.web.host_files import main;main()",
               source, "serve", "--socket", str(path)]
    # ponytail: start with deployment; use the serve command under systemd for boot startup.
    with (path.parent / "agent.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        try:
            data = control(path, "status")
            print("宿主机连接已启动：" + data["hostname"] + " / " + data["user"] + " / " + data["home"])
            return
        except (OSError, http.client.HTTPException):
            time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    raise RuntimeError("宿主机连接启动失败，请检查 host-files/agent.log。")


if __name__ == "__main__":
    main()
