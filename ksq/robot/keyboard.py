"""Robot keyboard discovery, confirmation injection and container recreation."""

from __future__ import annotations

from typing import Dict, List, Tuple
import json
import subprocess
import time

from ksq.dashboard import settings as dashboard_settings
from ksq.robot.logs import LogServiceError, inspect_container, restart_services


ROBOT_SERVICE_ID = "0"


ROBOT_SERVICE_NAME = "robot_workspace_move_test"


_LIST_DEVICES_SCRIPT = r"""
import json
import re
from pathlib import Path

text = Path("/proc/bus/input/devices").read_text(errors="replace")
blocks = [b for b in text.strip().split("\n\n") if b.strip()]
devices = []
for block in blocks:
    name = ""
    handlers = ""
    phys = ""
    for line in block.splitlines():
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1].strip()
        elif line.startswith("P: Phys="):
            phys = line.split("=", 1)[1].strip()
    match = re.search(r"\bevent(\d+)\b", handlers)
    if match is None:
        continue
    path = f"/dev/input/event{match.group(1)}"
    is_kbd = "kbd" in handlers.split()
    devices.append(
        {
            "path": path,
            "name": name or path,
            "handlers": handlers,
            "phys": phys,
            "is_keyboard": is_kbd,
        }
    )
devices.sort(key=lambda row: int(re.search(r"(\d+)$", row["path"]).group(1)))
print(json.dumps({"ok": True, "devices": devices}, ensure_ascii=False))
"""


_INJECT_SCRIPT = r"""
import json
import os
import struct
import time

EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0
KEY_1 = 2
device = (os.environ.get("KSQ_KEYBOARD_DEVICE") or "").strip()


def emit_fd(fd, ev_type, code, value):
    now = time.time()
    sec = int(now)
    usec = int((now - sec) * 1_000_000)
    os.write(fd, struct.pack("llHHi", sec, usec, ev_type, code, value))


errors = []
if device:
    try:
        from evdev import InputDevice, ecodes

        dev = InputDevice(device)
        dev.write(ecodes.EV_KEY, ecodes.KEY_1, 1)
        dev.syn()
        time.sleep(0.05)
        dev.write(ecodes.EV_KEY, ecodes.KEY_1, 0)
        dev.syn()
        print(json.dumps({"ok": True, "method": "evdev_device", "device": device}))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as error:
        errors.append(f"evdev_device:{error}")
    try:
        fd = os.open(device, os.O_WRONLY)
        try:
            emit_fd(fd, EV_KEY, KEY_1, 1)
            emit_fd(fd, EV_SYN, SYN_REPORT, 0)
            time.sleep(0.05)
            emit_fd(fd, EV_KEY, KEY_1, 0)
            emit_fd(fd, EV_SYN, SYN_REPORT, 0)
        finally:
            os.close(fd)
        print(json.dumps({"ok": True, "method": "event_write", "device": device}))
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as error:
        errors.append(f"event_write:{error}")

try:
    from evdev import UInput, ecodes

    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_1]}, name="ksq-virtual-keyboard")
    time.sleep(0.2)
    ui.write(ecodes.EV_KEY, ecodes.KEY_1, 1)
    ui.syn()
    time.sleep(0.05)
    ui.write(ecodes.EV_KEY, ecodes.KEY_1, 0)
    ui.syn()
    ui.close()
    print(
        json.dumps(
            {
                "ok": True,
                "method": "uinput_fallback",
                "device": device,
                "errors": errors,
            }
        )
    )
except Exception as error:
    errors.append(f"uinput:{error}")
    print(json.dumps({"ok": False, "errors": errors}))
    raise SystemExit(1)
"""


def _recreate_robot_for_keyboard_env() -> Dict[str, object]:
    """Force-recreate robot container so env_file PNP_KEYBOARD_DEVICE reloads."""
    info = inspect_container(ROBOT_SERVICE_NAME)
    if not info.get("exists"):
        raise LogServiceError(
            str(info.get("message") or f"服务不存在：{ROBOT_SERVICE_NAME}"),
            503,
        )
    code, stdout, stderr = _run_docker_raw(
        [
            "inspect",
            "-f",
            "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}"
            "|{{index .Config.Labels \"com.docker.compose.project.config_files\"}}"
            "|{{index .Config.Labels \"com.docker.compose.project\"}}",
            ROBOT_SERVICE_NAME,
        ],
        10,
    )
    if code != 0:
        # Fallback: plain restart (env may not refresh).
        return restart_services([ROBOT_SERVICE_NAME])
    parts = (stdout or "").strip().split("|")
    working_dir = parts[0].strip() if parts else ""
    config_files = parts[1].strip() if len(parts) > 1 else ""
    project = parts[2].strip() if len(parts) > 2 else ""
    if not working_dir:
        return restart_services([ROBOT_SERVICE_NAME])
    compose_args = ["compose"]
    if project:
        compose_args.extend(["-p", project])
    if config_files:
        for item in config_files.split(","):
            path = item.strip()
            if path:
                compose_args.extend(["-f", path])
    else:
        compose_args.extend(["--project-directory", working_dir])
    compose_args.extend(
        [
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "robot_workspace_move_test",
        ]
    )
    # Run from host working dir via docker compose (daemon resolves host paths).
    try:
        completed = subprocess.run(
            ["docker", *compose_args],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            cwd=working_dir if working_dir else None,
        )
    except FileNotFoundError as error:
        raise LogServiceError("未找到 docker 命令。", 503) from error
    except subprocess.TimeoutExpired as error:
        raise LogServiceError("重建机器人容器超时。", 504) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise LogServiceError(f"重建机器人容器失败：{detail}", 502)
    return {
        "ok": True,
        "action": "force-recreate",
        "name": ROBOT_SERVICE_NAME,
        "message": "已按新键盘设备环境变量重建机器人容器",
    }


def _run_docker_raw(args: List[str], timeout_seconds: int) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "docker not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def list_keyboard_devices() -> Dict[str, object]:
    settings = dashboard_settings.load_dashboard_settings()
    info = inspect_container(ROBOT_SERVICE_NAME)
    devices: List[Dict[str, object]] = []
    list_error = ""
    if info.get("running"):
        try:
            completed = subprocess.run(
                [
                    "docker",
                    "exec",
                    ROBOT_SERVICE_NAME,
                    "python3",
                    "-c",
                    _LIST_DEVICES_SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode == 0:
                parsed = json.loads((completed.stdout or "").strip().splitlines()[-1])
                if isinstance(parsed, dict) and isinstance(parsed.get("devices"), list):
                    devices = [
                        row
                        for row in parsed["devices"]
                        if isinstance(row, dict)
                    ]
            else:
                list_error = (completed.stderr or completed.stdout or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            list_error = str(error)
    else:
        list_error = str(info.get("message") or "机器人服务未启动")
    public_settings = {
        "keyboard_device": settings.get("keyboard_device"),
        "mode": settings.get("mode"),
        "etm_base_url": settings.get("etm_base_url"),
        "auto_confirm": bool(settings.get("auto_confirm")),
        "feishu": dashboard_settings._public_feishu_settings(
            settings["feishu"] if isinstance(settings.get("feishu"), dict) else {}
        ),
    }
    return {
        "ok": True,
        "settings": public_settings,
        "devices": devices,
        "service_running": bool(info.get("running")),
        "list_error": list_error,
        "default_device": dashboard_settings._DEFAULT_KEYBOARD_DEVICE,
    }


def inject_confirm_key() -> Dict[str, object]:
    info = inspect_container(ROBOT_SERVICE_NAME)
    if not info.get("running"):
        raise LogServiceError(
            str(info.get("message") or f"服务未启动：{ROBOT_SERVICE_NAME}"),
            503,
        )
    device = str(dashboard_settings.load_dashboard_settings().get("keyboard_device") or dashboard_settings._DEFAULT_KEYBOARD_DEVICE)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                f"KSQ_KEYBOARD_DEVICE={device}",
                ROBOT_SERVICE_NAME,
                "python3",
                "-c",
                _INJECT_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as error:
        raise LogServiceError(
            "未找到 docker 命令，无法注入虚拟键盘。",
            503,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise LogServiceError("虚拟键盘注入超时。", 504) from error

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"exit={completed.returncode}"
        raise LogServiceError(f"虚拟键盘注入失败：{detail}", 502)

    method = "unknown"
    used_device = device
    try:
        parsed = json.loads(stdout.splitlines()[-1])
        if isinstance(parsed, dict):
            method = str(parsed.get("method") or "unknown")
            used_device = str(parsed.get("device") or device)
    except (json.JSONDecodeError, IndexError, TypeError):
        method = "unknown"

    return {
        "ok": True,
        "key": "1",
        "method": method,
        "device": used_device,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "message": f"已向 {used_device} 注入确认按键",
    }
