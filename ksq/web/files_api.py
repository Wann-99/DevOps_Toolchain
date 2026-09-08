"""Administrator file access and Linux PTY sessions for the serving machine."""

from __future__ import annotations

import atexit
import base64
import codecs
import contextlib
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import pty
import pwd
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import zipfile

from ksq.web import auth, desktop

MAX_TRANSFER = 2 * 1024**3
PREVIEW_LIMIT = 256 * 1024
TERMINAL_BUFFER = 1024 * 1024
TERMINAL_IDLE = 120
_TERMINALS = {}
_LOCK = threading.Lock()


def local_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("文件路径无效。")
    return Path(value).expanduser().resolve(strict=True)


def desktop_url():
    value = os.environ.get("KSQ_DESKTOP_URL", "")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


def list_directory(value):
    path = local_path(value or str(Path.home()))
    entries = []
    with os.scandir(path) as scan:
        for entry in scan:
            if len(entries) >= 10000:
                raise ValueError("目录超过 10000 项，请打开更具体的目录。")
            try:
                info = entry.stat()
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
                entries.append({"name": entry.name, "kind": kind,
                                "size": info.st_size, "modified": info.st_mtime,
                                "symlink": entry.is_symlink()})
            except OSError:
                entries.append({"name": entry.name, "kind": "unavailable", "size": None, "modified": None})
    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
    return {"path": str(path), "parent": str(path.parent), "home": str(Path.home()),
            "entries": entries, "hostname": socket.gethostname(),
            "username": pwd.getpwuid(os.getuid()).pw_name,
            "environment": "宿主机" if os.environ.get("KSQ_HOST_FILES_AGENT") == "1" else "应用容器" if Path("/.dockerenv").exists() else "服务主机",
            "desktop_url": desktop_url() or (desktop.URL if os.environ.get("KSQ_HOST_FILES_AGENT") == "1" else ""),
            "desktop_error": desktop.unavailable() if os.environ.get("KSQ_HOST_FILES_AGENT") == "1" else ""}


@contextlib.contextmanager
def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("仅支持普通文件。")
        yield stream


def preview_file(value):
    path = local_path(value)
    with open_regular(path) as stream:
        raw = stream.read(PREVIEW_LIMIT + 1)
    if b"\x00" in raw:
        raise ValueError("二进制文件不支持文本预览，请下载后查看。")
    # Decode a possible partial final UTF-8 character only when truncating.
    try:
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(raw[:PREVIEW_LIMIT], final=len(raw) <= PREVIEW_LIMIT)
    except UnicodeDecodeError as error:
        raise ValueError("该文件不是 UTF-8 文本，请下载后查看。") from error
    return {"name": path.name, "text": text, "truncated": len(raw) > PREVIEW_LIMIT}


def upload_file(directory, name, source, length):
    root = local_path(directory)
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("上传文件名无效。")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("上传路径必须位于当前目录内。")
    if length < 0 or length > MAX_TRANSFER:
        raise ValueError("单个上传文件不能超过 2 GiB。")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, flags)
    temporary = ".ksq-upload-" + secrets.token_hex(12)
    created = False
    try:
        # dir_fd + O_NOFOLLOW prevent concurrent symlink changes from redirecting uploads.
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(fd, "wb") as output:
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("上传中断，未保存不完整文件。")
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        # Atomic publication, with no overwrite of a file created by another upload.
        os.link(temporary, relative.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)
    return {"ok": True, "name": name, "size": length}


def rename_entry(directory, name, new_name):
    for value in (name, new_name):
        if not isinstance(value, str) or not value.strip() or value in {".", ".."} or any(char in value for char in "/\\\x00"):
            raise ValueError("名称不能为空，也不能包含路径分隔符或使用 .、..。")
    root = local_path(directory)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise ValueError("仅支持重命名文件、文件夹和符号链接。")
        if name != new_name:
            rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
            if rename is None:
                raise ValueError("当前系统不支持无覆盖重命名。")
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            # RENAME_NOREPLACE is atomic; checking exists() before os.rename() could overwrite concurrent changes.
            if rename(directory_fd, os.fsencode(name), directory_fd, os.fsencode(new_name), 1) != 0:
                code = ctypes.get_errno()
                if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise ValueError("当前文件系统不支持无覆盖重命名。")
                raise OSError(code, os.strerror(code), new_name)
    finally:
        os.close(directory_fd)
    return {"ok": True, "name": new_name}


def zip_directory(path, output):
    total = 0
    count = 0

    def walk(directory, archive, prefix):
        nonlocal total, count
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > 100000:
                    raise ValueError("文件夹超过 100000 项，请分批下载。")
                if entry.is_symlink():
                    raise ValueError("文件夹含符号链接，请单独下载其目标，或选择不含链接的子目录。")
                name = prefix + "/" + entry.name
                if entry.is_dir(follow_symlinks=False):
                    archive.writestr(name + "/", b"")
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        walk(child, archive, name)
                    finally:
                        os.close(child)
                else:
                    fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(fd, "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise ValueError("文件夹含设备或其他特殊文件，无法打包。")
                        with archive.open(name, "w", force_zip64=True) as target:
                            while chunk := source.read(1024 * 1024):
                                total += len(chunk)
                                if total > MAX_TRANSFER:
                                    raise ValueError("文件夹超过 2 GiB，请分批下载。")
                                target.write(chunk)

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            prefix = path.name or "root"
            archive.writestr(prefix + "/", b"")
            walk(fd, archive, prefix)
    finally:
        os.close(fd)
    output.seek(0)


def send_download(handler, value, image=False):
    path = local_path(value)

    def send(stream, name, content_type, inline=False):
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
        handler.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(name, safe=""))
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        try:
            shutil.copyfileobj(stream, handler.wfile, 1024 * 1024)
        except OSError:
            # Headers already went out; closing marks an interrupted download.
            handler.close_connection = True

    if image:
        content_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower())
        if not content_type:
            raise ValueError("该格式不支持图片预览。")
        with open_regular(path) as stream:
            if os.fstat(stream.fileno()).st_size > 20 * 1024 * 1024:
                raise ValueError("图片超过 20 MiB，请下载后查看。")
            send(stream, path.name, content_type, True)
    elif path.is_dir():
        with tempfile.TemporaryFile() as archive:
            zip_directory(path, archive)
            send(archive, (path.name or "root") + ".zip", "application/zip")
    else:
        with open_regular(path) as stream:
            send(stream, path.name, "application/octet-stream")


def dimensions(payload):
    values = [payload.get("cols", 80), payload.get("rows", 24)]
    if any(type(value) is not int or not 2 <= value <= 500 for value in values):
        raise ValueError("终端行列必须是 2 至 500 的整数。")
    return values


class TerminalSession:
    def __init__(self, owner, payload, browser_desktop=True):
        self.owner = owner
        self.id = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.offset = 0
        self.last_seen = time.monotonic()
        self.closed = False
        cols, rows = dimensions(payload)
        cwd = local_path(payload.get("path") or str(Path.home()))
        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        env = dict(os.environ, TERM="xterm-256color")
        if browser_desktop:
            env = desktop.terminal_environment(env)
        language = env.get("LANG", "")
        # uutils 0.8 escapes Unicode under C.UTF-8; prefer the account's UTF-8 locale.
        if env.get("LC_ALL") in {"C", "POSIX", "C.UTF-8", "C.utf8"} and "utf" in language.lower():
            env["LC_ALL"] = language
        self.fd, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            # Acquire the controlling tty in a fresh interpreter, without preexec_fn in this threaded server.
            self.process = subprocess.Popen(
                [sys.executable, "-c", "import fcntl,os,sys,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],[sys.argv[1],'-i'])", shell],
                stdin=slave, stdout=slave, stderr=slave, cwd=cwd,
                start_new_session=True, close_fds=True,
                env=env,
            )
            os.set_blocking(self.fd, False)
        except BaseException:
            os.close(self.fd)
            raise
        finally:
            os.close(slave)
        threading.Thread(target=self._read, daemon=True, name="ksq-terminal").start()

    def _read(self):
        try:
            while not self.closed:
                if time.monotonic() - self.last_seen > TERMINAL_IDLE:
                    break
                if not select.select([self.fd], [], [], 0.25)[0]:
                    continue
                with self.lock:
                    if self.closed:
                        break
                    chunk = os.read(self.fd, 65536)
                    if not chunk:
                        break
                    self.buffer.extend(chunk)
                    overflow = max(0, len(self.buffer) - TERMINAL_BUFFER)
                    if overflow:
                        del self.buffer[:overflow]
                        self.offset += overflow
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            with contextlib.suppress(OSError):
                foreground = os.tcgetpgrp(self.fd)
                if foreground > 0:
                    os.killpg(foreground, signal.SIGHUP)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGHUP)
            # Let an interactive shell relay HUP to its background jobs first.
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    foreground = os.tcgetpgrp(self.fd)
                    if foreground > 0:
                        os.killpg(foreground, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            os.close(self.fd)

    def output(self, cursor):
        with self.lock:
            self.last_seen = time.monotonic()
            end = self.offset + len(self.buffer)
            if cursor < 0 or cursor > end:
                raise ValueError("终端输出位置无效。")
            data = bytes(self.buffer[max(0, cursor - self.offset):])
            cwd = None
            if not self.closed:
                with contextlib.suppress(OSError):
                    cwd = os.readlink(f"/proc/{self.process.pid}/cwd")
            return {"data": base64.b64encode(data).decode("ascii"), "cursor": end,
                    "closed": self.closed, "truncated": cursor < self.offset, "cwd": cwd}

    def input(self, payload):
        data = payload.get("data", "")
        if not isinstance(data, str) or len(data.encode("utf-8")) > 65536:
            raise ValueError("终端输入过长。")
        cols, rows = dimensions(payload)
        with self.lock:
            if self.closed:
                raise ValueError("终端已断开，请重新连接。")
            self.last_seen = time.monotonic()
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            pending = data.encode("utf-8")
            deadline = time.monotonic() + 2
            while pending:
                if time.monotonic() > deadline:
                    raise ValueError("终端输入繁忙，请中断当前程序后重试。")
                if select.select([], [self.fd], [], 0.1)[1]:
                    pending = pending[os.write(self.fd, pending):]
        return {"ok": True}


def close_terminals(owner=None):
    if os.environ.get("KSQ_HOST_FILES_SOCKET"):
        from ksq.web.host_files import close_terminals as close_host_terminals
        close_host_terminals(owner)
    with _LOCK:
        for identifier, terminal in list(_TERMINALS.items()):
            if owner is None or terminal.owner == owner:
                terminal.close()
                del _TERMINALS[identifier]


atexit.register(close_terminals)


def get_terminal(identifier, owner):
    with _LOCK:
        terminal = _TERMINALS.get(identifier)
        if terminal is None or terminal.owner != owner:
            raise FileNotFoundError("终端不存在或属于其他登录会话。")
        return terminal


def create_terminal(owner, payload, browser_desktop=True):
    with _LOCK:
        for identifier, terminal in list(_TERMINALS.items()):
            if terminal.closed:
                del _TERMINALS[identifier]
        if len(_TERMINALS) >= 8 or sum(item.owner == owner for item in _TERMINALS.values()) >= 2:
            raise ValueError("终端数量已达上限，请先断开已有终端。")
        terminal = TerminalSession(owner, payload, browser_desktop)
        _TERMINALS[terminal.id] = terminal
    return {"id": terminal.id}


def handle_request(handler, session):
    """Called after login validation; all file/terminal endpoints share this guard."""
    parsed = urlsplit(handler.path)
    if not parsed.path.startswith(("/api/files/", "/api/terminal/", "/desktop/")):
        return False
    # Close error paths instead of draining arbitrarily large untrusted uploads.
    handler.close_connection = True
    if session.get("role") != auth.ROLE_ADMIN:
        handler._send_json(403, {"error": "文件管理和终端仅管理员可用。"})
        return True
    if handler.headers.get("Sec-Fetch-Site") == "cross-site":
        handler._send_json(403, {"error": "拒绝跨站文件或终端请求。"})
        return True
    query = parse_qs(parsed.query)
    value = (query.get("path") or [""])[0]
    owner = auth.token_from_cookie(handler.headers.get("Cookie", ""))
    try:
        if parsed.path.startswith("/desktop/"):
            if handler.command != "GET":
                raise ValueError("桌面仅支持 GET 请求。")
            if handler.headers.get("Upgrade", "").lower() == "websocket" and os.environ.get("KSQ_HOST_FILES_AGENT") != "1":
                origin = urlsplit(handler.headers.get("Origin", ""))
                if origin.scheme not in {"http", "https"} or origin.netloc != handler.headers.get("Host"):
                    raise PermissionError("拒绝跨站桌面连接。")
        if handler.command != "GET":
            origin = handler.headers.get("Origin")
            if (handler.headers.get("X-KSQ-Request") != "1"
                    or (origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                    or urlsplit(origin).scheme not in {"http", "https"}))):
                raise PermissionError("拒绝跨站文件或终端操作。")
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("不支持分块请求体。")
        if os.environ.get("KSQ_HOST_FILES_SOCKET"):
            from ksq.web.host_files import forward
            forward(handler, owner)
            return True
        if parsed.path.startswith("/desktop/"):
            from ksq.web.host_files import forward
            forward(handler, owner, desktop_target=True)
            return True
        if Path("/.dockerenv").exists() and os.environ.get("KSQ_HOST_FILES_AGENT") != "1":
            handler._send_json(503, {"error": "尚未连接宿主机，请使用完整部署包并在宿主机执行 bash start.sh host-files start。"})
            return True
        if handler.command == "GET":
            if parsed.path == "/api/files/list":
                result = list_directory(value)
            elif parsed.path == "/api/files/preview":
                result = preview_file(value)
            elif parsed.path in {"/api/files/download", "/api/files/image"}:
                send_download(handler, value, parsed.path.endswith("/image"))
                return True
            elif parsed.path == "/api/terminal/output":
                terminal = get_terminal((query.get("id") or [""])[0], owner)
                result = terminal.output(int((query.get("cursor") or ["0"])[0]))
            else:
                raise FileNotFoundError("接口不存在。")
        else:
            length = int(handler.headers.get("Content-Length", "-1"))
            if parsed.path == "/api/files/upload":
                handler.connection.settimeout(120)
                result = upload_file(value, (query.get("name") or [""])[0], handler.rfile, length)
            else:
                if not 0 < length <= 128 * 1024:
                    raise ValueError("请求体大小无效。")
                payload = json.loads(handler.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是对象。")
                if parsed.path == "/api/files/rename":
                    result = rename_entry(payload.get("path"), payload.get("name"), payload.get("new_name"))
                elif parsed.path == "/api/terminal/create":
                    external = os.environ.get("KSQ_HOST_FILES_AGENT") == "1" and handler.headers.get("X-KSQ-Desktop-Mode") == "external"
                    result = create_terminal(owner, payload, browser_desktop=not external)
                elif parsed.path in {"/api/terminal/input", "/api/terminal/close"}:
                    identifier = payload.get("id")
                    if not isinstance(identifier, str):
                        raise ValueError("终端标识无效。")
                    terminal = get_terminal(identifier, owner)
                    if parsed.path.endswith("/close"):
                        terminal.close()
                        with _LOCK:
                            _TERMINALS.pop(terminal.id, None)
                        result = {"ok": True}
                    else:
                        result = terminal.input(payload)
                else:
                    raise FileNotFoundError("接口不存在。")
        handler._send_json(200, result)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except (OSError, ValueError, RecursionError) as error:
        status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, FileNotFoundError) else 409 if isinstance(error, FileExistsError) else 400
        message = "同名文件或文件夹已存在，未覆盖；请使用其他名称。" if status == 409 else str(error)
        handler._send_json(status, {"error": message})
    return True
