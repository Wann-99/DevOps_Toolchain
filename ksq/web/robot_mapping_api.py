"""Single-floor mapping controls using the chassis OpenAPI contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ksq.safe_io import safe_write_bytes, safe_write_text
from ksq.web import robot_map_api as robot

MAX_STCM_BYTES = 32 * 1024 * 1024
_MAPPING_PATH = "/api/core/slam/v1/mapping/:enable"
_LOOP_PATH = "/api/core/slam/v1/loopclosure/:enable"
_STCM_PATH = "/api/core/slam/v1/maps/stcm"
_MOVE_ACTION = "slamtec.agent.actions.MoveByAction"
_SPEED_PARAMS = {"linear_speed": "base.max_moving_speed", "angular_speed": "base.max_angular_speed"}
_DIRECTIONS = {"forward": 0, "backward": 1, "right": 2, "left": 3}
_DRIVE_LEASE_SECONDS = 1.0
_DRIVE_TIMEOUT_SECONDS = 0.3
_DRIVE_LEASES: dict[str, dict] = {}
_TELEOP_CAPABILITIES: dict[str, dict] = {}
# ponytail: share the existing process-wide connection lock; use per-robot locks
# only if the application supports simultaneous connections in the future.
_KNOWN_MAPPING: dict[str, bool | None] = {}


def _directory(base_url: str) -> Path:
    key = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
    return robot.ROBOT_MAP_SETTINGS_FILE.parent / "robot_mapping_backups" / key


def _load_state(base_url: str) -> dict:
    path = _directory(base_url) / "state.json"
    if not path.exists():
        return {"name": "当前地图", "phase": "idle", "dirty": False, "map_write_uncertain": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(state, dict)
            or state.get("robot_base_url") != base_url
            or not isinstance(state.get("name"), str)
            or state.get("phase") not in {"idle", "active", "paused", "finished", "saved", "uncertain"}
            or not isinstance(state.get("dirty"), bool)
            or not isinstance(state.get("map_write_uncertain", False), bool)
        ):
            raise ValueError("invalid mapping state")
        state.setdefault("map_write_uncertain", False)
        return state
    except (OSError, UnicodeError, ValueError) as error:
        raise robot.RobotApiError("本地建图记录无法读取，请检查文件后重试。", 500) from error


def _save_state(base_url: str, state: dict) -> None:
    safe_write_text(
        _directory(base_url) / "state.json",
        json.dumps({**state, "robot_base_url": base_url}, ensure_ascii=False) + "\n",
        backup=False,
    )


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("地图名称须为 1 至 64 个字符。")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("地图名称不能包含控制字符。")
    return value.strip()


def _backup_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("备份编号无效。")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError("备份编号无效。") from error
    if parsed.hex != value:
        raise ValueError("备份编号无效。")
    return value


def _backup_metadata(base_url: str, backup_id: object) -> dict:
    identifier = _backup_id(backup_id)
    directory = _directory(base_url)
    try:
        entry = json.loads((directory / f"{identifier}.json").read_text(encoding="utf-8"))
        if (
            not isinstance(entry, dict)
            or entry.get("id") != identifier
            or entry.get("robot_base_url") != base_url
            or not isinstance(entry.get("size"), int)
            or not 0 < entry["size"] <= MAX_STCM_BYTES
            or not isinstance(entry.get("sha256"), str)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("created_at"), str)
        ):
            raise ValueError("invalid backup metadata")
        return entry
    except FileNotFoundError as error:
        raise ValueError("该底盘的备份不存在，请刷新后重试。") from error
    except (OSError, UnicodeError, ValueError) as error:
        raise robot.RobotApiError("备份记录损坏，未执行地图替换。", 500) from error


def _list_backups(base_url: str) -> list[dict]:
    directory = _directory(base_url)
    if not directory.exists():
        return []
    entries = []
    for path in directory.glob("*.json"):
        if path.name == "state.json":
            continue
        entry = _backup_metadata(base_url, path.stem)
        entries.append({key: entry[key] for key in ("id", "name", "created_at", "size")})
    return sorted(entries, key=lambda entry: entry["created_at"], reverse=True)


def _stcm_request(method: str, base_url: str, payload: bytes | None = None) -> bytes:
    request = urllib.request.Request(
        f"{base_url}{_STCM_PATH}",
        data=payload,
        headers={"Accept": "application/octet-stream", "Content-Type": "application/octet-stream"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            length_header = response.headers.get("Content-Length")
            try:
                expected_length = None if length_header is None else int(length_header)
            except (TypeError, ValueError) as error:
                raise robot.RobotApiError("底盘 STCM 响应长度无效，未确认传输完成。") from error
            if expected_length is not None and not 0 <= expected_length <= MAX_STCM_BYTES:
                raise robot.RobotApiError("底盘 STCM 响应长度无效或超过 32 MiB 限制。", 413)
            raw = response.read(MAX_STCM_BYTES + 1)
            if expected_length is not None and len(raw) != expected_length:
                raise robot.RobotApiError("底盘 STCM 下载不完整，未确认传输完成。")
    except urllib.error.HTTPError as error:
        raise robot.RobotApiError(f"底盘 STCM 接口返回 HTTP {error.code}。", error.code) from error
    except http.client.HTTPException as error:
        raise robot.RobotApiError("底盘 STCM 响应不完整，未确认传输完成。") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise robot.RobotApiError("底盘 STCM 传输失败或超时，未确认完成。", 504) from error
    if len(raw) > MAX_STCM_BYTES:
        raise robot.RobotApiError("底盘 STCM 超过 32 MiB 限制。", 413)
    if method == "GET" and not raw:
        raise robot.RobotApiError("底盘返回空 STCM，未创建备份。")
    return raw


def _create_backup(base_url: str, name: str) -> dict:
    raw = _stcm_request("GET", base_url)
    identifier = uuid.uuid4().hex
    entry = {
        "id": identifier, "name": name, "robot_base_url": base_url,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }
    directory = _directory(base_url)
    safe_write_bytes(directory / f"{identifier}.stcm", raw, backup=False)
    safe_write_text(directory / f"{identifier}.json", json.dumps(entry, ensure_ascii=False) + "\n", backup=False)
    return entry


def _read_backup(base_url: str, identifier: object) -> tuple[bytes, dict]:
    entry = _backup_metadata(base_url, identifier)
    try:
        with (_directory(base_url) / f"{entry['id']}.stcm").open("rb") as stream:
            raw = stream.read(MAX_STCM_BYTES + 1)
    except OSError as error:
        raise robot.RobotApiError("备份地图无法读取，未替换当前地图。", 500) from error
    if len(raw) != entry["size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise robot.RobotApiError("备份地图校验失败，未替换当前地图。", 500)
    return raw, entry


def _read_flag(path: str, base_url: str) -> bool:
    _, value = robot._request(
        "GET", path, base_url=base_url,
        timeout=robot._TELEMETRY_REQUEST_TIMEOUT_SECONDS,
    )
    if type(value) is not bool:
        raise robot.RobotApiError("底盘返回的建图开关状态格式无效。")
    if path == _MAPPING_PATH:
        _KNOWN_MAPPING[base_url] = value
    return value


def _set_flag(path: str, enabled: bool, base_url: str) -> None:
    if path == _MAPPING_PATH:
        _KNOWN_MAPPING[base_url] = None
    _, accepted = robot._request("PUT", path, {"enable": enabled}, base_url=base_url)
    if accepted is not True:
        raise robot.RobotApiError("底盘未确认建图开关修改成功。")
    if _read_flag(path, base_url) is not enabled:
        raise robot.RobotApiError("底盘建图开关回读与请求不一致，请刷新后重试。")


def _teleop_capability(base_url: str) -> dict:
    cached = _TELEOP_CAPABILITIES.get(base_url)
    if cached and cached["expires"] > time.monotonic():
        return cached
    try:
        _, factories = robot._request(
            "GET", "/api/core/motion/v1/action-factories", base_url=base_url,
            timeout=robot._TELEMETRY_REQUEST_TIMEOUT_SECONDS,
        )
        if not isinstance(factories, list) or any(not isinstance(item, dict) for item in factories):
            raise robot.RobotApiError("底盘动作能力列表格式无效。")
        action_name = next((
            item.get("action_name") for item in factories
            if item.get("action_name") in (_MOVE_ACTION, "agent.actions.MoveByAction")
        ), None)
        supported = action_name is not None
        reason = "" if supported else "当前底盘未提供 MoveByAction 遥控动作。"
    except robot.RobotApiError as error:
        supported, action_name, reason = False, None, f"无法确认底盘遥控能力：{error}"
    result = {"supported": supported, "action_name": action_name, "reason": reason, "expires": time.monotonic() + (60 if supported else 5)}
    _TELEOP_CAPABILITIES[base_url] = result
    return result


def _read_speed(param: str, base_url: str) -> float:
    if param == "base.max_moving_speed":
        return robot._get_max_moving_speed_for(base_url, timeout=robot._TELEMETRY_REQUEST_TIMEOUT_SECONDS)
    _, raw = robot._request(
        "GET", f"/api/core/system/v1/parameter?param={param}", base_url=base_url,
        accept="text/plain", timeout=robot._TELEMETRY_REQUEST_TIMEOUT_SECONDS,
    )
    if isinstance(raw, dict):
        raw = next((raw[key] for key in ("value", "data", "result") if key in raw), None)
    value = robot._finite_float(raw)
    if value is None or value <= 0:
        raise robot.RobotApiError("底盘速度上限无效，未启用遥控。")
    return value


def _set_speed(param: str, value: float, base_url: str) -> None:
    if math.isclose(_read_speed(param, base_url), value, rel_tol=1e-6, abs_tol=1e-9):
        return
    _, accepted = robot._request(
        "PUT", "/api/core/system/v1/parameter", {"param": param, "value": str(value)},
        base_url=base_url, timeout=robot._TELEMETRY_REQUEST_TIMEOUT_SECONDS,
    )
    # The observed limit gates motion; an HTTP acknowledgement alone does not.
    for attempt in range(3):
        actual = _read_speed(param, base_url)
        if math.isclose(actual, value, rel_tol=1e-6, abs_tol=1e-9):
            return
        if attempt < 2:
            time.sleep(0.1)
    label, unit = ("线速度", "m/s") if param == "base.max_moving_speed" else ("角速度", "rad/s")
    reply = "true" if accepted is True else "false" if accepted is False else "非布尔响应"
    raise robot.RobotApiError(
        f"{label}上限未生效：请求 {value:.6g} {unit}，读回 {actual:.6g} {unit}（固件返回 {reply}）。"
    )


def revoke_drive(base_url: str) -> None:
    """Called under the connection lock before stopping or switching robots."""
    _DRIVE_LEASES.pop(base_url, None)


def restore_drive_speeds(base_url: str) -> bool:
    state = _load_state(base_url)
    originals = state.get("drive_speed_restore")
    if originals is None:
        return False
    if not isinstance(originals, dict) or set(originals) != set(_SPEED_PARAMS.values()) or any(
        robot._finite_float(value) is None or float(value) <= 0 for value in originals.values()
    ):
        raise robot.RobotApiError("原速度记录损坏，无法确认速度已恢复。", 500)
    errors = []
    for param, value in originals.items():
        try:
            _set_speed(param, float(value), base_url)
        except robot.RobotApiError as error:
            errors.append(str(error))
    if errors:
        raise robot.RobotApiError("原速度恢复未确认，请再次停止后重试：" + "；".join(errors))
    state.pop("drive_speed_restore")
    _save_state(base_url, state)
    return True


def _drive_start(payload: dict, base_url: str) -> dict:
    speeds = {name: robot._finite_motion_value(payload.get(name), name) for name in _SPEED_PARAMS}
    if not 0 < speeds["linear_speed"] <= 0.4 or not 0 < speeds["angular_speed"] <= 0.6:
        raise ValueError("遥控线速度须大于 0 且不超过 0.4 m/s，角速度须大于 0 且不超过 0.6 rad/s。")
    if base_url in _DRIVE_LEASES:
        raise robot.RobotApiError("已有遥控会话，请先释放控制或停止。", 409)
    snapshot = _status(base_url)
    if (
        type(snapshot["mapping_enabled"]) is not bool or snapshot["map_write_uncertain"]
        or snapshot["phase"] not in {"idle", "active", "paused", "finished", "saved"}
    ):
        raise robot.RobotApiError("底盘或地图状态尚未确认，暂不能启用遥控。", 409)
    if not snapshot["teleop_supported"]:
        raise robot.RobotApiError(snapshot["teleop_reason"], 409)
    if snapshot.get("drive_restore_pending"):
        raise robot.RobotApiError("上次遥控的原速度尚未恢复，请先停止。", 409)
    _require_stationary(base_url)
    originals = {param: _read_speed(param, base_url) for param in _SPEED_PARAMS.values()}
    if any(speeds[name] > originals[param] for name, param in _SPEED_PARAMS.items()):
        raise ValueError(
            "遥控不能提高底盘原有速度上限："
            f"线速度至多 {originals['base.max_moving_speed']:.3g} m/s，"
            f"角速度至多 {originals['base.max_angular_speed']:.3g} rad/s。"
        )
    state = _load_state(base_url)
    state["drive_speed_restore"] = originals
    _save_state(base_url, state)
    try:
        for name, param in _SPEED_PARAMS.items():
            _set_speed(param, speeds[name], base_url)
    except Exception:
        restore_drive_speeds(base_url)
        raise
    token = uuid.uuid4().hex
    _DRIVE_LEASES[base_url] = {
        "token": token, "expires": time.monotonic() + _DRIVE_LEASE_SECONDS,
        "action_name": snapshot["teleop_action_name"],
        "status": {**snapshot, "drive_restore_pending": True, "teleop_restore_pending": True},
        "observed_at": time.monotonic(),
    }
    return {"robot_base_url": base_url, "drive_token": token}


def _drive_command(payload: dict, base_url: str) -> dict:
    lease = _DRIVE_LEASES.get(base_url)
    token = payload.get("drive_token")
    if not lease or not isinstance(token, str) or token != lease["token"]:
        raise robot.RobotApiError("遥控令牌已失效，请重新启用遥控。", 409)
    if payload["command"] == "drive-stop":
        revoke_drive(base_url)
        robot._cancel_current_action_for(base_url)
        restore_drive_speeds(base_url)
        return {"robot_base_url": base_url, "stopped": True}
    if lease["expires"] <= time.monotonic():
        raise robot.RobotApiError("遥控会话已过期，请停止后重新启用。", 409)
    direction = payload.get("direction")
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise ValueError("遥控方向必须为 forward、backward、left 或 right。")
    try:
        _, action = robot._request(
            "POST", "/api/core/motion/v1/actions",
            {"action_name": lease["action_name"], "options": {"direction": _DIRECTIONS[direction], "duration": 200}},
            base_url=base_url, timeout=_DRIVE_TIMEOUT_SECONDS,
        )
        if not isinstance(action, dict) or type(action.get("action_id")) is not int:
            raise robot.RobotApiError("底盘未返回有效遥控动作。")
    except Exception:
        lease["expires"] = 0
        raise
    lease["expires"] = time.monotonic() + _DRIVE_LEASE_SECONDS
    return {"robot_base_url": base_url, "action": action}


def require_navigation_allowed(base_url: str) -> None:
    """Block known mapping sessions at the shared legacy navigation entry."""
    with robot._ROBOT_CONNECTION_LOCK:
        state = _load_state(base_url)
        if state["map_write_uncertain"]:
            raise robot.RobotApiError("地图替换结果尚未确认，请先恢复备份或重新导入、清空地图。", 409)
        if "drive_speed_restore" in state:
            raise robot.RobotApiError("遥控原速度尚未恢复，请先停止后再执行导航。", 409)
        if (
            state["phase"] in {"active", "paused", "uncertain"}
            or (base_url in _KNOWN_MAPPING and _KNOWN_MAPPING[base_url] is not False)
        ):
            raise robot.RobotApiError("建图会话尚未结束，请先结束采集再执行导航或巡逻。", 409)


def _require_idle(base_url: str, state: dict) -> None:
    if "drive_speed_restore" in state:
        raise robot.RobotApiError("遥控原速度尚未恢复，请先停止后再修改地图。", 409)
    if _read_flag(_MAPPING_PATH, base_url) or state["phase"] in {"active", "paused", "uncertain"}:
        raise robot.RobotApiError("请先结束建图采集，再修改或上传地图。", 409)
    _require_stationary(base_url)


def _require_stationary(base_url: str) -> None:
    try:
        current = robot.get_current_action(expected_base_url=base_url)
    except robot.RobotApiError as error:
        if error.status_code == 404:
            return
        raise
    state = current.get("state", current)
    if not isinstance(state, dict) or state.get("status") != 4:
        raise robot.RobotApiError("底盘已有动作或状态无法确认，请先停止后再遥控或操作地图。", 409)


def _invalidate(base_url: str) -> None:
    robot._PATROL_TRACK_PLANS.pop(base_url, None)
    robot._invalidate_telemetry_cache()


def _status(base_url: str) -> dict:
    lease = _DRIVE_LEASES.get(base_url)
    if lease:
        active = lease["expires"] > time.monotonic()
        return {
            **lease["status"], "drive_active": active,
            "teleop_reason": "" if active else "遥控已过期，请先停止并恢复原速度。",
            "status_cached": True, "status_age_seconds": max(0, time.monotonic() - lease["observed_at"]),
        }
    state = _load_state(base_url)
    original = dict(state)
    errors = {}
    flags: dict[str, bool | None] = {}
    for key, path in (("mapping", _MAPPING_PATH), ("loop_closure", _LOOP_PATH)):
        try:
            flags[key] = _read_flag(path, base_url)
        except robot.RobotApiError as error:
            flags[key] = None
            errors[key] = str(error)
    if flags["mapping"] is True:
        state.update(phase="active", dirty=True)
    elif flags["mapping"] is False and state["phase"] in {"active", "uncertain"}:
        state.update(phase="finished", dirty=True)
    if state != original:
        _save_state(base_url, state)
    phase = state["phase"] if flags["mapping"] is not None else "unavailable"
    teleop = _teleop_capability(base_url)
    restore_pending = "drive_speed_restore" in state
    return {
        **state, "robot_base_url": base_url, "phase": phase,
        "mapping_enabled": flags["mapping"],
        "loop_closure_enabled": flags["loop_closure"],
        "capability_errors": errors,
        "loop_closure_supported": flags["loop_closure"] is not None,
        "teleop_supported": teleop["supported"],
        "teleop_action_name": teleop["action_name"],
        "teleop_reason": "遥控原速度尚未恢复，请先停止后重试。" if restore_pending else teleop["reason"],
        "drive_active": False, "drive_restore_pending": restore_pending,
        "teleop_restore_pending": restore_pending,
        "backups": _list_backups(base_url),
    }


def get_status(expected_base_url: object = None) -> dict:
    with robot._ROBOT_CONNECTION_LOCK:
        return _status(robot.require_current_base_url(expected_base_url))


def export_map(expected_base_url: object = None) -> bytes:
    with robot._ROBOT_CONNECTION_LOCK:
        base_url = robot.require_current_base_url(expected_base_url)
    raw = _stcm_request("GET", base_url)
    with robot._ROBOT_CONNECTION_LOCK:
        robot.require_current_base_url(base_url)
    return raw


def _confirmed(payload: dict) -> None:
    if payload.get("confirm") is not True:
        raise ValueError("此操作需要明确确认（confirm=true）。")


def _import_bytes(payload: dict) -> bytes:
    filename = payload.get("filename")
    content = payload.get("content_base64")
    if (
        not isinstance(filename, str) or len(filename) > 128
        or "/" in filename or "\\" in filename or not filename.lower().endswith(".stcm")
    ):
        raise ValueError("请选择 .stcm 地图文件。")
    if not isinstance(content, str) or not content:
        raise ValueError("导入地图不能为空。")
    if len(content) > ((MAX_STCM_BYTES + 2) // 3) * 4:
        raise ValueError("导入地图不能超过 32 MiB。")
    try:
        raw = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("导入地图编码无效。") from error
    if not raw or len(raw) > MAX_STCM_BYTES:
        raise ValueError("导入地图须大于 0 字节且不能超过 32 MiB。")
    return raw


def _require_single_floor(base_url: str) -> None:
    _, floors = robot._request("GET", "/api/multi-floor/map/v1/floors", base_url=base_url)
    if (
        not isinstance(floors, list) or len(floors) != 1
        or not isinstance(floors[0], dict)
        or not isinstance(floors[0].get("floor"), str)
        or not floors[0]["floor"].strip()
    ):
        raise robot.RobotApiError("未确认当前底盘只有一个楼层，已拒绝上传以保护其他楼层地图。", 409)


def execute(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("建图请求必须为对象。")
    expected = payload.get("expected_robot_base_url")
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("建图操作必须指定当前底盘地址。")
    command = payload.get("command")
    if not isinstance(command, str) or command not in {
        "start", "pause", "resume", "finish", "new", "clear", "rename",
        "loop-closure", "upload", "import", "backup", "restore", "delete-backup",
        "stop", "drive-start", "drive-stop", "move", "deploy", "delete-object",
    }:
        raise ValueError("未知建图操作。")
    with robot._ROBOT_CONNECTION_LOCK:
        # The lease is pinned at acquisition and revoked by save_settings.
        # Pulses never read disk or poll firmware flags between 200 ms actions.
        if command in {"move", "drive-stop"}:
            return _drive_command(payload, expected.strip().rstrip("/"))
        base_url = robot.require_current_base_url(expected)
        if command == "stop":
            revoke_drive(base_url)
            robot._cancel_current_action_for(base_url)
            restore_error = None
            try:
                restore_drive_speeds(base_url)
            except Exception as error:
                restore_error = str(error)
            try:
                result = _status(base_url)
            except Exception as error:  # A status failure must not hide an accepted stop.
                result = {
                    "robot_base_url": base_url, "phase": "unavailable",
                    "mapping_enabled": None, "loop_closure_enabled": None,
                    "capability_errors": {"status": str(error)}, "backups": [],
                    "teleop_supported": False, "teleop_reason": "底盘状态无法确认。",
                }
            if restore_error:
                result["drive_restore_pending"] = True
                result["teleop_restore_pending"] = True
                result["teleop_reason"] = "遥控原速度尚未恢复，请先停止后重试。"
                result.setdefault("capability_errors", {})["drive_restore"] = restore_error
            return {**result, "stopped": True}
        if base_url in _DRIVE_LEASES and command not in {"pause", "finish"}:
            raise robot.RobotApiError("遥控会话尚未释放，请先停止后再操作。", 409)
        if command == "drive-start":
            return _drive_start(payload, base_url)
        state = _load_state(base_url)
        restored_drive = False
        stopped_drive = False
        if command in {"pause", "finish"} and (base_url in _DRIVE_LEASES or "drive_speed_restore" in state):
            revoke_drive(base_url)
            robot._cancel_current_action_for(base_url)
            restored_drive = restore_drive_speeds(base_url)
            stopped_drive = True
            state = _load_state(base_url)
        if "drive_speed_restore" in state and command in {"start", "resume"}:
            raise robot.RobotApiError("遥控原速度尚未恢复，请先停止后继续采集。", 409)
        if state["map_write_uncertain"] and command in {"start", "resume", "upload"}:
            raise robot.RobotApiError("地图替换结果尚未确认，请先恢复备份或重新导入、清空地图。", 409)
        if command in {"new", "clear", "import", "restore", "delete-backup", "delete-object", "upload"}:
            _confirmed(payload)
        if command == "rename":
            state["name"] = _name(payload.get("name"))
        elif command == "backup":
            _require_stationary(base_url)
            _create_backup(base_url, _name(payload.get("name", state["name"])))
        elif command == "delete-backup":
            entry = _backup_metadata(base_url, payload.get("backup_id"))
            directory = _directory(base_url)
            (directory / f"{entry['id']}.stcm").unlink(missing_ok=True)
            (directory / f"{entry['id']}.json").unlink()
        elif command == "loop-closure":
            enabled = payload.get("enable")
            if type(enabled) is not bool:
                raise ValueError("enable 必须是布尔值。")
            _set_flag(_LOOP_PATH, enabled, base_url)
        elif command in {"start", "resume", "pause", "finish"}:
            enabled = _read_flag(_MAPPING_PATH, base_url)
            if command == "start" and (enabled or state["phase"] == "paused"):
                raise robot.RobotApiError("已有建图会话，请使用继续或结束采集。", 409)
            if command == "resume" and (enabled or state["phase"] != "paused"):
                raise robot.RobotApiError("只有已暂停的建图会话可以继续。", 409)
            if command == "pause" and not enabled:
                raise robot.RobotApiError("底盘未在采集，不能暂停。", 409)
            if command == "finish" and not enabled and not restored_drive and state["phase"] not in {"paused", "active", "uncertain"}:
                raise robot.RobotApiError("当前没有需要结束的建图会话。", 409)
            if command in {"start", "resume"}:
                _require_stationary(base_url)
            elif not stopped_drive:
                robot._cancel_current_action_for(base_url)
            # Persist uncertainty before the request so a timeout or process
            # restart cannot accidentally unlock navigation after enabling SLAM.
            state.update(phase="uncertain", dirty=True)
            _save_state(base_url, state)
            _set_flag(_MAPPING_PATH, command in {"start", "resume"}, base_url)
            state["phase"] = {"start": "active", "resume": "active", "pause": "paused", "finish": "finished"}[command]
            _invalidate(base_url)
        else:
            _require_idle(base_url, state)
            if command in {"new", "clear", "import", "restore"}:
                name = _name(payload.get("name", state["name"]))
                raw = None
                if command == "import":
                    raw = _import_bytes(payload)
                elif command == "restore":
                    raw, entry = _read_backup(base_url, payload.get("backup_id"))
                    name = entry["name"]
                backup = payload.get("backup", True)
                if type(backup) is not bool:
                    raise ValueError("backup 必须是布尔值。")
                if backup:
                    _create_backup(base_url, state["name"])
                state.update(phase="finished", dirty=True, map_write_uncertain=True)
                _save_state(base_url, state)
                _invalidate(base_url)
                if raw is None:
                    robot._request("DELETE", "/api/core/slam/v1/maps", base_url=base_url)
                else:
                    _stcm_request("PUT", base_url, raw)
                state.update(name=name, phase="idle", dirty=True, map_write_uncertain=False)
                _invalidate(base_url)
            elif command == "upload":
                _require_single_floor(base_url)
                if "name" in payload:
                    state["name"] = _name(payload["name"])
                state.update(phase="finished", dirty=True)
                _save_state(base_url, state)
                # PUT core/maps/stcm is only a runtime import. This separate
                # official endpoint is the operation that actually persists it.
                _, result = robot._request("POST", "/api/multi-floor/map/v1/stcm/:save", base_url=base_url)
                if result is False:
                    raise robot.RobotApiError("固件拒绝保存地图，当前地图仍待上传。")
                state.update(phase="saved", dirty=False)
            elif command in {"deploy", "delete-object"}:
                from ksq.web import robot_mapping_objects

                if command == "deploy":
                    kind = robot_mapping_objects._kind(payload)
                    robot_mapping_objects._validated(payload, kind)
                operation = robot_mapping_objects.save_object if command == "deploy" else robot_mapping_objects.delete_object
                state.update(phase="finished", dirty=True)
                _save_state(base_url, state)
                _invalidate(base_url)
                operation(payload, base_url)
        _save_state(base_url, state)
        return _status(base_url)
