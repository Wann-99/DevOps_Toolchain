"""A host-owned virtual desktop, exposed only through the authenticated web app."""

from __future__ import annotations

import atexit
import contextlib
import os
from pathlib import Path
import pkgutil
import secrets
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

URL = "/desktop/vnc.html?autoconnect=1&resize=scale&path=desktop/websockify"
_ROOT = None
_RUNTIME = None
_PROCESSES = []
_ENV = {}
_LOCK = threading.RLock()


def configure(directory):
    global _ROOT
    _ROOT = Path(directory)


def unavailable():
    if _ROOT is None:
        return "浏览器桌面需要通过宿主机连接进程启动。"
    commands = ("Xtigervnc", "xauth", "openbox", "tint2", "dbus-daemon", "websockify")
    if any(not shutil.which(command) for command in commands) or not Path("/usr/share/novnc/vnc.html").is_file():
        return "宿主机未安装图形组件，请在部署目录执行 bash start.sh desktop install，然后重新连接终端。"
    return ""


def close():
    global _RUNTIME
    with _LOCK:
        for process in reversed(_PROCESSES):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        _PROCESSES.clear()
        _ENV.clear()
        if _RUNTIME is not None:
            _RUNTIME.cleanup()
            _RUNTIME = None


atexit.register(close)


def prepare_session(directory):
    # Resources must also load when the host agent imports directly from the .bin zip.
    theme = directory / "share/themes/KSQ/openbox-3"
    theme.mkdir(parents=True)
    for name, target in (("themerc", theme / "themerc"), ("tint2rc", directory / "tint2rc")):
        target.write_bytes(pkgutil.get_data("ksq.web", "desktop_assets/" + name))
    namespace = {"ob": "http://openbox.org/3.4/rc"}
    ET.register_namespace("", namespace["ob"])
    config = ET.parse("/etc/xdg/openbox/rc.xml")
    config.find("ob:theme/ob:name", namespace).text = "KSQ"
    config.find("ob:desktops/ob:number", namespace).text = "1"
    for size in config.findall("ob:theme/ob:font/ob:size", namespace):
        size.text = "11"
    titlebar = config.find("ob:mouse/ob:context[@name='Titlebar']", namespace)
    for binding in list(titlebar):
        if binding.get("button") in ("Up", "Down"):
            titlebar.remove(binding)
    for menu in config.findall("ob:mouse/ob:context/ob:mousebind/ob:action/ob:menu", namespace):
        if menu.text == "root-menu":
            menu.text = "client-list-combined-menu"
    config.write(directory / "openbox.xml", encoding="utf-8", xml_declaration=True)
    web = directory / "web"
    shutil.copytree("/usr/share/novnc", web)
    with (web / "app/styles/base.css").open("ab") as stylesheet:
        stylesheet.write(pkgutil.get_data("ksq.web", "desktop_assets/desktop.css"))


def start():
    global _RUNTIME
    with _LOCK:
        error = unavailable()
        if error:
            raise ValueError(error)
        if _ENV and all(process.poll() is None for process in _PROCESSES):
            return dict(_ENV)
        close()
        # ponytail: one virtual desktop per deployment account; admins share that account's desktop.
        _RUNTIME = tempfile.TemporaryDirectory(prefix="ksq-desktop-")
        directory = Path(_RUNTIME.name)
        authority = directory / "Xauthority"
        authority.touch(mode=0o600)
        cookie = secrets.token_hex(16)

        def authorize(display):
            subprocess.run(["xauth", "-f", str(authority)], input=f"add {display} . {cookie}\n",
                           text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)

        try:
            prepare_session(directory)
            with (_ROOT / "desktop.log").open("ab") as log:
                def launch(command, **kwargs):
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                               start_new_session=True, **kwargs)
                    _PROCESSES.append(process)
                    return process

                authorize(":0")
                read_fd, write_fd = os.pipe()
                try:
                    launch(["Xtigervnc", "-displayfd", str(write_fd), "-auth", str(authority),
                            "-geometry", "1440x900", "-depth", "24", "-nolisten", "tcp",
                            "-rfbport", "-1", "-rfbunixpath", str(directory / "vnc.sock"),
                            "-rfbunixmode", "0600", "-SecurityTypes", "None", "-AlwaysShared"],
                           pass_fds=(write_fd,))
                    os.close(write_fd)
                    write_fd = -1
                    if not select.select([read_fd], [], [], 15)[0]:
                        raise ValueError("虚拟桌面启动超时，请检查 host-files/desktop.log。")
                    number = os.read(read_fd, 32).decode().strip()
                    if not number.isdecimal():
                        raise ValueError("虚拟桌面启动失败，请检查 host-files/desktop.log。")
                finally:
                    os.close(read_fd)
                    if write_fd >= 0:
                        os.close(write_fd)
                display = ":" + number
                authorize(display)
                env = dict(os.environ, DISPLAY=display, XAUTHORITY=str(authority),
                           XDG_RUNTIME_DIR=str(directory), XDG_SESSION_TYPE="x11",
                           DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(directory / "bus"))
                for key in ("WAYLAND_DISPLAY", "SESSION_MANAGER", "DBUS_SESSION_BUS_PID"):
                    env.pop(key, None)
                launch(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                        "--address=" + env["DBUS_SESSION_BUS_ADDRESS"]], env=env)
                if shutil.which("xsetroot"):
                    subprocess.run(["xsetroot", "-solid", "#dce5e5", "-cursor_name", "left_ptr"], env=env,
                                   stdout=log, stderr=log, timeout=5, check=True)
                data_dirs = str(directory / "share") + ":" + env.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
                launch(["openbox", "--sm-disable", "--config-file", str(directory / "openbox.xml")],
                       env=dict(env, XDG_DATA_DIRS=data_dirs))
                launch(["tint2", "-c", str(directory / "tint2rc")], env=env)
                source = str(Path(__file__).resolve().parents[2])
                launch(["/usr/bin/python3", "-c",
                        "import sys;sys.path.insert(0,sys.argv.pop(1));from ksq.web.desktop import serve_web;serve_web(sys.argv[1])",
                        source, str(directory)], env=env)
                deadline = time.monotonic() + 10
                while not (directory / "web.sock").exists() or not (directory / "bus").exists():
                    if time.monotonic() > deadline or any(process.poll() is not None for process in _PROCESSES):
                        raise ValueError("浏览器桌面启动失败，请检查 host-files/desktop.log。")
                    time.sleep(0.05)
                _ENV.update(env)
                return dict(_ENV)
        except (OSError, subprocess.SubprocessError) as error:
            close()
            raise ValueError("图形组件启动失败，请检查 host-files/desktop.log。") from error
        except BaseException:
            close()
            raise


def terminal_environment(env):
    if _ROOT is None or unavailable():
        return env
    graphical = start()
    for key in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS"):
        env[key] = graphical[key]
    for key in ("WAYLAND_DISPLAY", "SESSION_MANAGER", "DBUS_SESSION_BUS_PID"):
        env.pop(key, None)
    return env


def connection_path():
    start()
    return Path(_RUNTIME.name) / "web.sock"


def serve_web(directory):
    from websockify.websocketproxy import LibProxyServer

    class UnixSocket(socket.socket):
        def setsockopt(self, level, option, value):
            # Ubuntu 22.04's websockify 0.10 assumes TCP even for a Unix listener.
            if level == socket.SOL_TCP and option == socket.TCP_NODELAY:
                return
            super().setsockopt(level, option, value)

    class UnixProxy(LibProxyServer):
        address_family = socket.AF_UNIX
        daemon_threads = True
        unix_listen = True

        def server_bind(self):
            self.socket.bind(self.server_address[0])
            self.server_name, self.server_port = "localhost", 0

        def get_request(self):
            client, _address = super().get_request()
            return UnixSocket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=client.detach()), ("localhost", 0)

    os.umask(0o077)
    with UnixProxy(listen_host=str(Path(directory) / "web.sock"),
                   unix_target=str(Path(directory) / "vnc.sock"), web=str(Path(directory) / "web")) as server:
        server.serve_forever()
