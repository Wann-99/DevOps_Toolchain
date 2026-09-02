"""Docker container log helpers for the log-query view."""

from __future__ import annotations

import atexit
import json
import re
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

LOG_SERVICES: List[Dict[str, object]] = [
    {"id": "0", "name": "robot_workspace_move_test"},
    {"id": "1", "name": "percept"},
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
_CORRUPT_LOG_MARKER = "Error grabbing logs: invalid character"
_DOCKER_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$"
)
_DOCKER_ERROR_PREFIXES = (
    "Error response from daemon:",
    "Cannot connect to the Docker daemon",
    "permission denied while trying to connect",
)
_FOLLOW_BUFFER_LINES = 5000
_INITIAL_FOLLOW_TAIL = 2500
# ponytail: 损坏历史的回退窗口与内存上限一致；需要跨损坏段无损回放时改用
# Docker logging driver/API，CLI 无法同时绕过旧损坏段并保证无限历史。
_CORRUPT_RESUME_TAIL = _FOLLOW_BUFFER_LINES
_FOLLOW_RETRY_DELAYS = (1.0, 2.0, 5.0)
_SSE_HEARTBEAT_SECONDS = 15.0
_FOLLOWERS_LOCK = threading.Lock()
_FOLLOWERS: Dict[str, Dict[str, object]] = {}


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_follower_state(name: str) -> Dict[str, object]:
    lock = threading.RLock()
    return {
        "name": name,
        "lock": lock,
        "condition": threading.Condition(lock),
        "entries": deque(maxlen=_FOLLOW_BUFFER_LINES),
        "generation": 0,
        "sequence": 0,
        "process": None,
        "supervisor": None,
        "stop_event": threading.Event(),
        "source": "starting",
        "status": "starting",
        "running": False,
        "error": "",
        "notice": "",
        "notice_version": 0,
        "state_version": 0,
        "degraded": False,
        "last_docker_timestamp": "",
        "initial_wait_done": False,
    }


def _notify_state(state: Dict[str, object], **changes: object) -> None:
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        changed = False
        for key, value in changes.items():
            if state.get(key) != value:
                state[key] = value
                changed = True
        if changed:
            state["state_version"] = int(state.get("state_version") or 0) + 1
            condition.notify_all()  # type: ignore[attr-defined]


def _publish_notice(state: Dict[str, object], message: str) -> None:
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        state["notice"] = str(message or "")
        state["notice_version"] = int(state.get("notice_version") or 0) + 1
        state["degraded"] = True
        state["state_version"] = int(state.get("state_version") or 0) + 1
        condition.notify_all()  # type: ignore[attr-defined]


def _split_docker_log_line(line: str) -> Tuple[str, str, str]:
    match = _DOCKER_TIMESTAMP_RE.match(line)
    if match is not None:
        docker_timestamp = match.group(1)
        display_line = match.group(2)
        return docker_timestamp, display_line, line
    received_at = _utc_now_iso()
    return received_at, line, f"{received_at} {line}"


def _append_log_line(
    state: Dict[str, object],
    raw_line: str,
    replay_after: str = "",
) -> Tuple[bool, bool]:
    raw = str(raw_line or "").rstrip("\r\n")
    corrupt = "\x00" in raw or _CORRUPT_LOG_MARKER in raw
    cleaned = raw.replace("\x00", "")
    if _CORRUPT_LOG_MARKER in cleaned:
        return False, True
    if any(cleaned.startswith(prefix) for prefix in _DOCKER_ERROR_PREFIXES):
        _notify_state(state, error=cleaned, status="reconnecting", running=False)
        return False, corrupt
    docker_timestamp, display_line, parser_line = _split_docker_log_line(cleaned)
    if replay_after and docker_timestamp <= replay_after:
        return False, corrupt
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        sequence = int(state.get("sequence") or 0) + 1
        entry = {
            "generation": int(state.get("generation") or 0),
            "sequence": sequence,
            "docker_timestamp": docker_timestamp,
            "display": display_line,
            "parser": parser_line,
        }
        state["sequence"] = sequence
        state["last_docker_timestamp"] = docker_timestamp
        state["initial_wait_done"] = True
        entries = state["entries"]
        entries.append(entry)  # type: ignore[attr-defined]
        condition.notify_all()  # type: ignore[attr-defined]
    return True, corrupt


def _follow_command(name: str, source: str, checkpoint: str = "") -> List[str]:
    if source == "attach":
        return ["docker", "attach", "--no-stdin", "--sig-proxy=false", name]
    command = ["docker", "logs", "--follow"]
    if source == "logs":
        command.extend(["--tail", str(_INITIAL_FOLLOW_TAIL)])
    elif source == "resume" and checkpoint:
        command.extend(["--since", checkpoint])
    elif source == "resume_tail" and checkpoint:
        command.extend(["--tail", str(_CORRUPT_RESUME_TAIL)])
    else:
        command.extend(["--tail", "0"])
    command.extend(["--timestamps", name])
    return command


def _start_follow_process(command: List[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as error:
        raise LogServiceError(
            "未找到 docker 命令，请在主机运行本服务或在容器内安装 docker CLI。",
            503,
        ) from error
    except OSError as error:
        raise LogServiceError(f"无法启动实时日志读取：{error}", 503) from error


def _consume_follow_process(
    state: Dict[str, object],
    process: subprocess.Popen,
    replay_after: str = "",
    detect_corruption: bool = True,
) -> Tuple[int, bool, int]:
    corrupt = False
    appended = 0
    stream = process.stdout
    if stream is not None:
        for raw_line in stream:
            was_appended, line_corrupt = _append_log_line(
                state, str(raw_line or ""), replay_after=replay_after
            )
            if was_appended:
                appended += 1
            if line_corrupt and detect_corruption:
                corrupt = True
                try:
                    process.terminate()
                except OSError:
                    pass
                break
    return_code = process.wait()
    return return_code, corrupt, appended


def _next_corruption_source(source: str) -> str:
    if source == "resume":
        return "resume_tail"
    if source in {"logs", "resume_tail"}:
        return "tail0"
    return "attach"


def _next_follow_source(
    source: str,
    return_code: int,
    corrupt: bool,
    appended: int,
    has_checkpoint: bool,
) -> str:
    if corrupt:
        return _next_corruption_source(source)
    if (
        source in {"tail0", "resume", "resume_tail"}
        and return_code != 0
        and appended == 0
    ):
        return "attach"
    if source in {"logs", "tail0", "resume", "resume_tail"} and has_checkpoint:
        return "resume"
    return source


def _follow_supervisor(name: str, state: Dict[str, object]) -> None:
    stop_event = state["stop_event"]
    source = "logs"
    retry_index = 0
    while not stop_event.is_set():  # type: ignore[attr-defined]
        try:
            info = inspect_container(name)
        except LogServiceError as error:
            _notify_state(
                state,
                running=False,
                status="docker_unavailable",
                error=str(error),
                source=source,
            )
            delay = _FOLLOW_RETRY_DELAYS[min(retry_index, len(_FOLLOW_RETRY_DELAYS) - 1)]
            retry_index += 1
            stop_event.wait(delay)  # type: ignore[attr-defined]
            continue
        if not info.get("running"):
            _notify_state(
                state,
                running=False,
                status=str(info.get("status") or "stopped"),
                error=str(info.get("message") or f"服务未启动：{name}"),
                source=source,
            )
            delay = _FOLLOW_RETRY_DELAYS[min(retry_index, len(_FOLLOW_RETRY_DELAYS) - 1)]
            retry_index += 1
            stop_event.wait(delay)  # type: ignore[attr-defined]
            continue

        condition = state["condition"]
        with condition:  # type: ignore[attr-defined]
            checkpoint = str(state.get("last_docker_timestamp") or "")
            state["generation"] = int(state.get("generation") or 0) + 1
            generation = int(state["generation"])
        command_source = source
        replay_after = checkpoint if source in {"resume", "resume_tail"} else ""
        command = _follow_command(name, command_source, checkpoint)
        try:
            process = _start_follow_process(command)
        except LogServiceError as error:
            _notify_state(
                state,
                running=False,
                status="reconnecting",
                error=str(error),
                source=command_source,
            )
            delay = _FOLLOW_RETRY_DELAYS[min(retry_index, len(_FOLLOW_RETRY_DELAYS) - 1)]
            retry_index += 1
            stop_event.wait(delay)  # type: ignore[attr-defined]
            continue
        _notify_state(
            state,
            process=process,
            running=True,
            status="streaming",
            error="",
            source=command_source,
            generation=generation,
        )
        return_code, corrupt, appended = _consume_follow_process(
            state,
            process,
            replay_after=replay_after,
            detect_corruption=command_source != "attach",
        )
        with condition:  # type: ignore[attr-defined]
            if state.get("process") is process:
                state["process"] = None
        if stop_event.is_set():  # type: ignore[attr-defined]
            break
        if corrupt:
            source = _next_follow_source(
                command_source,
                return_code,
                corrupt,
                appended,
                bool(state.get("last_docker_timestamp")),
            )
            _publish_notice(
                state,
                "历史日志包含 NUL/损坏片段，已跳过并继续实时获取新日志。",
            )
            retry_index = 0
            continue
        source = _next_follow_source(
            command_source,
            return_code,
            corrupt,
            appended,
            bool(checkpoint or state.get("last_docker_timestamp")),
        )
        if source == "attach" and command_source != "attach":
            _publish_notice(
                state,
                "docker logs 实时读取仍不可用，已降级为 docker attach 持续获取新日志。",
            )
        if appended:
            retry_index = 0
        else:
            retry_index += 1
        _notify_state(
            state,
            running=False,
            status="reconnecting",
            error=f"实时日志进程退出（code={return_code}），正在重连。",
            source=source,
        )
        delay = _FOLLOW_RETRY_DELAYS[min(retry_index, len(_FOLLOW_RETRY_DELAYS) - 1)]
        stop_event.wait(delay)  # type: ignore[attr-defined]
    _notify_state(state, running=False, status="stopped", process=None)


def _ensure_log_follower(name: str) -> Dict[str, object]:
    with _FOLLOWERS_LOCK:
        state = _FOLLOWERS.get(name)
        if state is None:
            state = _new_follower_state(name)
            _FOLLOWERS[name] = state
        supervisor = state.get("supervisor")
        if isinstance(supervisor, threading.Thread) and supervisor.is_alive():
            return state
        stop_event = state.get("stop_event")
        if not isinstance(stop_event, threading.Event) or stop_event.is_set():
            state["stop_event"] = threading.Event()
        supervisor = threading.Thread(
            target=_follow_supervisor,
            args=(name, state),
            name=f"ksq-log-supervisor-{name}",
            daemon=True,
        )
        state["supervisor"] = supervisor
        supervisor.start()
        return state


def _read_followed_logs(
    name: str,
    tail: int,
    cursor: int = -1,
) -> Tuple[str, int, str]:
    state = _ensure_log_follower(name)
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        entries = list(state["entries"])  # type: ignore[arg-type]
        sequence = int(state.get("sequence") or 0)
        error = str(state.get("error") or "")
    if cursor >= 0:
        lines = [
            str(entry.get("parser") or "")
            for entry in entries
            if int(entry.get("sequence") or 0) > cursor
        ]
        return "\n".join(lines[-tail:]), sequence, error
    lines = [str(entry.get("parser") or "") for entry in entries[-tail:]]
    return "\n".join(lines), sequence, error


def _stop_log_followers() -> None:
    with _FOLLOWERS_LOCK:
        states = list(_FOLLOWERS.values())
    for state in states:
        stop_event = state.get("stop_event")
        if isinstance(stop_event, threading.Event):
            stop_event.set()
        process = state.get("process")
        if isinstance(process, subprocess.Popen) and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        condition = state.get("condition")
        if isinstance(condition, threading.Condition):
            with condition:
                condition.notify_all()


atexit.register(_stop_log_followers)


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


def _validate_tail(tail: int) -> None:
    if tail < 1 or tail > _FOLLOW_BUFFER_LINES:
        raise LogServiceError(
            f"tail 必须在 1~{_FOLLOW_BUFFER_LINES} 之间。", 400
        )


def _parse_stream_event_id(raw_value: str) -> Tuple[Optional[int], Optional[int]]:
    value = str(raw_value or "").strip()
    if not value:
        return None, None
    parts = value.split(":", 1)
    if len(parts) != 2:
        return None, None
    try:
        generation = int(parts[0])
        sequence = int(parts[1])
    except ValueError:
        return None, None
    if generation < 0 or sequence < 0:
        return None, None
    return generation, sequence


def _entry_event_id(entry: Dict[str, object]) -> str:
    return (
        f"{int(entry.get('generation') or 0)}:"
        f"{int(entry.get('sequence') or 0)}"
    )


def _state_payload(state: Dict[str, object]) -> Dict[str, object]:
    return {
        "name": str(state.get("name") or ""),
        "running": bool(state.get("running")),
        "status": str(state.get("status") or ""),
        "source": str(state.get("source") or ""),
        "degraded": bool(state.get("degraded")),
        "error": str(state.get("error") or ""),
        "generation": int(state.get("generation") or 0),
        "sequence": int(state.get("sequence") or 0),
    }


def _snapshot_payload(
    state: Dict[str, object], entries: List[Dict[str, object]]
) -> Dict[str, object]:
    payload = _state_payload(state)
    payload.update(
        {
            "lines": [str(entry.get("display") or "") for entry in entries],
            "notice": str(state.get("notice") or ""),
        }
    )
    return payload


def _line_payload(entry: Dict[str, object]) -> Dict[str, object]:
    return {
        "line": str(entry.get("display") or ""),
        "docker_timestamp": str(entry.get("docker_timestamp") or ""),
        "generation": int(entry.get("generation") or 0),
        "sequence": int(entry.get("sequence") or 0),
    }


def encode_sse_event(event: Dict[str, object]) -> bytes:
    event_id = str(event.get("id") or "")
    event_name = str(event.get("event") or "message")
    data = json.dumps(
        event.get("data") or {}, ensure_ascii=False, separators=(",", ":")
    )
    frame = ""
    if event_id:
        frame += f"id: {event_id}\n"
    frame += f"event: {event_name}\n"
    frame += f"data: {data}\n\n"
    return frame.encode("utf-8")


def encode_http_chunk(payload: bytes) -> bytes:
    body = bytes(payload)
    return f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n"


def _wait_for_initial_entries(state: Dict[str, object], timeout: float = 0.35) -> None:
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        if state.get("initial_wait_done") or state["entries"] or state.get("error"):
            state["initial_wait_done"] = True
            return
        condition.wait(timeout)  # type: ignore[attr-defined]
        state["initial_wait_done"] = True


def stream_log_events(
    service_id: str,
    tail: int,
    last_event_id: str = "",
    heartbeat_seconds: float = _SSE_HEARTBEAT_SECONDS,
) -> Iterator[Dict[str, object]]:
    _validate_tail(tail)
    name = resolve_service_name(service_id)
    state = _ensure_log_follower(name)
    _wait_for_initial_entries(state)
    requested_generation, requested_sequence = _parse_stream_event_id(last_event_id)

    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        entries = list(state["entries"])  # type: ignore[arg-type]
        current_generation = int(state.get("generation") or 0)
        current_sequence = int(state.get("sequence") or 0)
        oldest_sequence = (
            int(entries[0].get("sequence") or 0) if entries else current_sequence
        )
        cursor_valid = bool(
            requested_sequence is not None
            and requested_generation is not None
            and requested_generation <= current_generation
            and requested_sequence <= current_sequence
            and requested_sequence >= max(0, oldest_sequence - 1)
        )
        if cursor_valid:
            initial_entries = [
                entry
                for entry in entries
                if int(entry.get("sequence") or 0) > int(requested_sequence or 0)
            ]
            cursor_sequence = int(requested_sequence or 0)
        else:
            initial_entries = entries[-tail:]
            cursor_sequence = current_sequence
        seen_state_version = int(state.get("state_version") or 0)
        seen_notice_version = int(state.get("notice_version") or 0)

    if cursor_valid:
        if initial_entries:
            for entry in initial_entries:
                cursor_sequence = int(entry.get("sequence") or cursor_sequence)
                yield {
                    "event": "line",
                    "id": _entry_event_id(entry),
                    "data": _line_payload(entry),
                }
        else:
            yield {
                "event": "state",
                "id": f"{current_generation}:{current_sequence}",
                "data": _state_payload(state),
            }
    else:
        yield {
            "event": "snapshot",
            "id": f"{current_generation}:{current_sequence}",
            "data": _snapshot_payload(state, initial_entries),
        }

    while True:
        with condition:  # type: ignore[attr-defined]
            has_pending_change = bool(
                int(state.get("sequence") or 0) > cursor_sequence
                or int(state.get("state_version") or 0) != seen_state_version
                or int(state.get("notice_version") or 0) != seen_notice_version
            )
            if not has_pending_change:
                condition.wait(max(0.01, heartbeat_seconds))  # type: ignore[attr-defined]
            entries = list(state["entries"])  # type: ignore[arg-type]
            current_sequence = int(state.get("sequence") or 0)
            current_generation = int(state.get("generation") or 0)
            current_state_version = int(state.get("state_version") or 0)
            current_notice_version = int(state.get("notice_version") or 0)
            oldest_sequence = (
                int(entries[0].get("sequence") or 0)
                if entries
                else current_sequence
            )
            state_changed = current_state_version != seen_state_version
            notice_changed = current_notice_version != seen_notice_version
            if cursor_sequence < max(0, oldest_sequence - 1):
                snapshot_entries = entries[-tail:]
                pending_entries: List[Dict[str, object]] = []
                needs_snapshot = True
                cursor_sequence = current_sequence
            else:
                snapshot_entries = []
                pending_entries = [
                    entry
                    for entry in entries
                    if int(entry.get("sequence") or 0) > cursor_sequence
                ]
                needs_snapshot = False
            state_payload = _state_payload(state) if state_changed else None
            notice = str(state.get("notice") or "") if notice_changed else ""
            seen_state_version = current_state_version
            seen_notice_version = current_notice_version

        emitted = False
        if needs_snapshot:
            emitted = True
            yield {
                "event": "snapshot",
                "id": f"{current_generation}:{current_sequence}",
                "data": _snapshot_payload(state, snapshot_entries),
            }
        else:
            for entry in pending_entries:
                emitted = True
                cursor_sequence = int(entry.get("sequence") or cursor_sequence)
                yield {
                    "event": "line",
                    "id": _entry_event_id(entry),
                    "data": _line_payload(entry),
                }
        if state_payload is not None:
            emitted = True
            yield {
                "event": "state",
                "id": f"{current_generation}:{current_sequence}",
                "data": state_payload,
            }
        if notice_changed and notice:
            emitted = True
            yield {
                "event": "notice",
                "id": f"{current_generation}:{current_sequence}",
                "data": {"message": notice, "degraded": True},
            }
        if not emitted:
            yield {
                "event": "heartbeat",
                "id": f"{current_generation}:{current_sequence}",
                "data": {"at": _utc_now_iso()},
            }


def fetch_logs(
    service_id: str, tail: int, since: str = "", stream_cursor: str = ""
) -> Dict[str, object]:
    _validate_tail(tail)
    name = resolve_service_name(service_id)
    state = _ensure_log_follower(name)
    _wait_for_initial_entries(state)
    cursor_value = str(stream_cursor or "").strip()
    cursor = -1
    if cursor_value:
        try:
            cursor = int(cursor_value)
        except ValueError as error:
            raise LogServiceError("cursor 参数无效。", 400) from error
        if cursor < 0:
            raise LogServiceError("cursor 参数无效。", 400)
    followed, next_cursor, follow_error = _read_followed_logs(
        name, tail, cursor=cursor
    )
    condition = state["condition"]
    with condition:  # type: ignore[attr-defined]
        notice = str(state.get("notice") or "")
        degraded = bool(state.get("degraded"))
        source = str(state.get("source") or "")
        running = bool(state.get("running"))
        follower_status = str(state.get("status") or "")
        has_entries = bool(state["entries"])
    if (
        not running
        and not has_entries
        and follower_status not in {"starting", "reconnecting"}
    ):
        raise LogServiceError(
            follow_error or f"服务未启动：{name}",
            503,
        )
    return {
        "name": name,
        "running": running,
        "status": follower_status,
        "tail": tail,
        "mode": "stream",
        "since": str(since or ""),
        "logs": followed,
        "stream_cursor": str(next_cursor),
        "streaming_fallback": source in {"tail0", "attach"},
        "degraded": degraded,
        "notice": notice,
        "error": follow_error,
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
