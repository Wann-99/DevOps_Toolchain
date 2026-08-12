"""Docker container log helpers for the log-query view."""

from __future__ import annotations

import subprocess
from typing import Dict, List, Tuple

LOG_SERVICES: List[Dict[str, object]] = [
    {"id": "0", "name": "robot_workspace_move_test"},
    {"id": "1", "name": "percept"},
    {"id": "2", "name": "robotd"},
    {"id": "3", "name": "CAMID_0"},
]

_NAME_BY_ID = {str(item["id"]): str(item["name"]) for item in LOG_SERVICES}
_ALLOWED_NAMES = set(_NAME_BY_ID.values())
RESTARTABLE_SERVICES = frozenset(
    {"robot_workspace_move_test", "percept"}
)
_CONFIG_FILE_KINDS = frozenset(
    {"shelves", "tool_mapping", "pick_strategy", "unavailable"}
)
_DOCKER_RESTART_TIMEOUT_SECONDS = 120
_DOCKER_START_STOP_TIMEOUT_SECONDS = 120
_CONTROL_ACTIONS = frozenset({"start", "restart", "stop"})


class LogServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _run_docker(args: List[str], timeout_seconds: int) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise LogServiceError(
            "未找到 docker 命令，请在主机运行本服务或在容器内安装 docker CLI。",
            503,
        ) from error
    except subprocess.TimeoutExpired as error:
        raise LogServiceError("docker 命令超时。", 504) from error
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def resolve_service_name(service_id: str) -> str:
    name = _NAME_BY_ID.get(str(service_id).strip())
    if name is None:
        raise LogServiceError("未知服务编号。", 400)
    return name


def inspect_container(name: str) -> Dict[str, object]:
    if name not in _ALLOWED_NAMES:
        raise LogServiceError("容器名不在白名单中。", 400)
    code, stdout, stderr = _run_docker(
        ["inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name],
        8,
    )
    if code != 0:
        detail = (stderr or stdout).strip()
        if "No such object" in detail or "No such container" in detail:
            return {
                "name": name,
                "exists": False,
                "running": False,
                "status": "not_found",
                "message": f"服务未启动或不存在：{name}",
            }
        if "Cannot connect to the Docker daemon" in detail or "permission denied" in detail.lower():
            return {
                "name": name,
                "exists": False,
                "running": False,
                "status": "docker_unavailable",
                "message": f"无法连接 Docker：{detail}",
            }
        return {
            "name": name,
            "exists": False,
            "running": False,
            "status": "error",
            "message": detail or f"无法检查容器：{name}",
        }
    parts = stdout.strip().split("|", 1)
    status = parts[0] if parts else "unknown"
    running = len(parts) > 1 and parts[1].strip().lower() == "true"
    return {
        "name": name,
        "exists": True,
        "running": running,
        "status": status,
        "message": "" if running else f"服务未启动：{name}",
    }


def list_services() -> Dict[str, object]:
    services: List[Dict[str, object]] = []
    for item in LOG_SERVICES:
        info = inspect_container(str(item["name"]))
        services.append(
            {
                "id": item["id"],
                "name": item["name"],
                "exists": info["exists"],
                "running": info["running"],
                "status": info["status"],
                "message": info["message"],
            }
        )
    return {"services": services}


def fetch_logs(
    service_id: str, tail: int, since: str = ""
) -> Dict[str, object]:
    if tail < 1 or tail > 5000:
        raise LogServiceError("tail 必须在 1~5000 之间。", 400)
    name = resolve_service_name(service_id)
    info = inspect_container(name)
    if not info["running"]:
        raise LogServiceError(
            str(info["message"] or f"服务未启动：{name}"),
            503,
        )
    since_value = str(since or "").strip()
    args = ["logs", "--timestamps"]
    mode = "tail"
    if since_value:
        # Restrict since to timestamp-like values to avoid injection.
        if not (
            since_value.endswith("Z")
            or since_value.endswith("s")
            or since_value.endswith("m")
        ):
            raise LogServiceError("since 参数无效。", 400)
        args.extend(["--since", since_value])
        mode = "since"
    else:
        args.extend(["--tail", str(tail)])
    args.append(name)
    code, stdout, stderr = _run_docker(args, 20)
    if code != 0:
        detail = (stderr or stdout).strip()
        raise LogServiceError(detail or f"读取日志失败：{name}", 502)
    return {
        "name": name,
        "running": True,
        "status": info["status"],
        "tail": tail,
        "mode": mode,
        "since": since_value,
        "logs": stdout,
    }


def services_for_written_files(
    written_files: List[Dict[str, object]],
) -> List[Dict[str, str]]:
    need_robot = False
    need_percept = False
    for item in written_files:
        kind = str(item.get("kind") or "").strip()
        if kind == "knowledge":
            need_percept = True
        elif kind in _CONFIG_FILE_KINDS:
            need_robot = True
    services: List[Dict[str, str]] = []
    if need_robot:
        services.append(
            {
                "name": "robot_workspace_move_test",
                "reason": "配置文件（库位/工具/闭环/不可处理）已写回",
            }
        )
    if need_percept:
        services.append(
            {
                "name": "percept",
                "reason": "knowledge 已写回",
            }
        )
    return services


def restart_services(service_names: List[str]) -> Dict[str, object]:
    if not service_names:
        raise LogServiceError("未指定要重启的服务。", 400)
    unique_names: List[str] = []
    seen: set[str] = set()
    for raw_name in service_names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        if name not in RESTARTABLE_SERVICES:
            raise LogServiceError(
                f"不允许重启服务：{name}。仅支持 "
                + "、".join(sorted(RESTARTABLE_SERVICES)),
                400,
            )
        seen.add(name)
        unique_names.append(name)

    results: List[Dict[str, object]] = []
    for name in unique_names:
        result = _run_container_action(name, "restart")
        results.append(result)
    return {"ok": True, "services": results}


def _run_container_action(name: str, action: str) -> Dict[str, object]:
    if name not in _ALLOWED_NAMES:
        raise LogServiceError("容器名不在白名单中。", 400)
    if action not in _CONTROL_ACTIONS:
        raise LogServiceError(
            f"不支持的操作：{action}。仅支持 start / restart / stop。",
            400,
        )
    info = inspect_container(name)
    if action == "start":
        if info["running"]:
            return {
                "name": name,
                "ok": True,
                "action": action,
                "status": info.get("status") or "running",
                "running": True,
                "message": "服务已在运行",
            }
        if not info["exists"]:
            raise LogServiceError(
                str(info["message"] or f"服务不存在：{name}"),
                503,
            )
    elif action in {"stop", "restart"}:
        if not info["exists"]:
            raise LogServiceError(
                str(info["message"] or f"服务不存在：{name}"),
                503,
            )
        if action == "stop" and not info["running"]:
            return {
                "name": name,
                "ok": True,
                "action": action,
                "status": info.get("status") or "exited",
                "running": False,
                "message": "服务已停止",
            }

    timeout = (
        _DOCKER_RESTART_TIMEOUT_SECONDS
        if action == "restart"
        else _DOCKER_START_STOP_TIMEOUT_SECONDS
    )
    code, stdout, stderr = _run_docker([action, name], timeout)
    if code != 0:
        detail = (stderr or stdout).strip()
        raise LogServiceError(detail or f"{action} 失败：{name}", 502)
    after = inspect_container(name)
    return {
        "name": name,
        "ok": True,
        "action": action,
        "status": after.get("status") or "unknown",
        "running": bool(after.get("running")),
        "message": "",
    }


def control_service(service_id: str, action: str) -> Dict[str, object]:
    name = resolve_service_name(service_id)
    result = _run_container_action(name, str(action or "").strip().lower())
    return {"ok": True, "service": result}
