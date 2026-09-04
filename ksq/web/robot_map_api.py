"""Client for the Hermes chassis (SLAMTEC Slamware) RESTful API.

Endpoints and payload shapes here are taken directly from the robot's own
live OpenAPI spec (``http://<robot_ip>:1448/js/spec.js``, firmware >= 4.6.0
serves an interactive Swagger UI at ``/index.html``) rather than the printed
manuals, since the spec matches the exact firmware running on the connected
chassis. The chassis and this tool share a fixed internal network, so the
base URL is configuration, not discovery — it is persisted in
:data:`ksq.constants.ROBOT_MAP_SETTINGS_FILE` and defaults to the factory
wired-port address.

Notable payload details confirmed from the live spec (differ from the plain
RESTful API PDF manual in a few places):

- POI pose is flat ``{"x", "y", "yaw"}`` (``Pose2D``), not a nested
  ``position`` object; ``type`` lives inside ``metadata``, not as a sibling
  of ``pose``.
- ``GET /api/core/slam/v1/localization/pose`` returns a flat ``Pose3D``:
  ``{"x", "y", "z", "yaw", "pitch", "roll"}``.
- ``GET /api/core/slam/v1/maps/explore`` returns a binary stream: a 36-byte
  little-endian header (origin_x: float, origin_y: float, width: uint32,
  height: uint32, resolution: float, 12 reserved bytes, data_len: uint32)
  followed by one grayscale byte per grid cell.
"""

from __future__ import annotations

import io
import ipaddress
import json
import math
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from copy import deepcopy
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ksq.constants import (
    DEFAULT_ROBOT_BASE_URL,
    ROBOT_MAP_POIS_FILE,
    ROBOT_MAP_SETTINGS_FILE,
)
from ksq.safe_io import safe_write_text

_REQUEST_TIMEOUT_SECONDS = 8

# Map telemetry is read-only and deliberately has a shorter timeout than the
# control/configuration calls below.  A stalled lidar request must not make the
# map page wait for the full eight seconds used by the legacy endpoints.  The
# value can be overridden in tests (and by an embedding service) without
# changing the public API.
_TELEMETRY_REQUEST_TIMEOUT_SECONDS = 1.5
_TELEMETRY_REFRESH_INTERVAL_SECONDS = 0.15
_TELEMETRY_FAILURE_BACKOFF_SECONDS = 0.5
_TELEMETRY_STALE_AFTER_SECONDS = 1.0
_TELEMETRY_WAIT_SECONDS = 0.25
_MAX_LASER_POINTS = 20000

# A process-wide cache keeps multiple map viewers from multiplying requests to
# the chassis.  Refreshing is demand-driven by GET /api/map/telemetry; no
# permanent worker is needed until the UI actually opens the map page.
_TELEMETRY_CONDITION = threading.Condition(threading.RLock())
_TELEMETRY_SNAPSHOT: Optional[Dict[str, object]] = None
_TELEMETRY_REFRESHING = False
_TELEMETRY_NEXT_REFRESH_MONOTONIC = 0.0
_TELEMETRY_SEQUENCE = 0
_TELEMETRY_GENERATION = 0
_TELEMETRY_EXECUTOR = ThreadPoolExecutor(
    max_workers=3, thread_name_prefix="robot-map-telemetry"
)
_POI_CACHE_LOCK = threading.RLock()
_ROBOT_CONNECTION_LOCK = threading.RLock()


class RobotApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class RobotConnectionSwitchRequired(RuntimeError):
    """The old chassis could not be stopped before changing endpoints."""


def _invalidate_telemetry_cache() -> None:
    """Drop cached sensor data after the configured robot address changes."""
    global _TELEMETRY_SNAPSHOT, _TELEMETRY_NEXT_REFRESH_MONOTONIC
    global _TELEMETRY_SEQUENCE, _TELEMETRY_GENERATION
    with _TELEMETRY_CONDITION:
        _TELEMETRY_SNAPSHOT = None
        _TELEMETRY_NEXT_REFRESH_MONOTONIC = 0.0
        _TELEMETRY_SEQUENCE = 0
        _TELEMETRY_GENERATION += 1
        _TELEMETRY_CONDITION.notify_all()


# --------------------------------------------------------------------------
# Connection settings (persisted locally; robot IP is fixed per deployment).
# --------------------------------------------------------------------------

def load_settings() -> Dict[str, object]:
    settings: Dict[str, object] = {"robot_base_url": DEFAULT_ROBOT_BASE_URL}
    if ROBOT_MAP_SETTINGS_FILE.is_file():
        try:
            payload = json.loads(ROBOT_MAP_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            base_url = str(payload.get("robot_base_url") or "").strip()
            if base_url:
                settings["robot_base_url"] = base_url.rstrip("/")
    return settings


def save_settings(payload: Dict[str, object]) -> Dict[str, object]:
    raw_base_url = payload.get("robot_base_url")
    if not isinstance(raw_base_url, str) or not raw_base_url.strip():
        raise ValueError("机器人地址不能为空。")
    base_url_input = raw_base_url.strip()
    try:
        parsed = urlsplit(base_url_input)
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError("机器人地址格式必须为 http://<IPv4>:<端口>。") from error
    if (
        parsed.scheme.lower() != "http"
        or address.version != 4
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in base_url_input
        or "#" in base_url_input
    ):
        raise ValueError("机器人地址格式必须为 http://<IPv4>:<端口>。")
    base_url = f"http://{address.compressed}:{port}"
    force_switch = payload.get("force_switch", False)
    if not isinstance(force_switch, bool):
        raise ValueError("force_switch 必须是布尔值。")
    settings = {"robot_base_url": base_url}
    with _ROBOT_CONNECTION_LOCK:
        current = _require_current_base_url_unlocked(
            payload.get("expected_robot_base_url")
        )
        if current != base_url:
            _request(
                "GET",
                "/api/core/system/v1/robot/info",
                timeout=3,
                base_url=base_url,
            )
            try:
                _cancel_current_action_for(current)
            except RobotApiError as error:
                if not force_switch:
                    raise RobotConnectionSwitchRequired(
                        "无法确认旧底盘已停止，请现场确认后再强制切换。"
                    ) from error
        safe_write_text(
            ROBOT_MAP_SETTINGS_FILE,
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        )
        _invalidate_telemetry_cache()
        return settings


def _base_url() -> str:
    return str(load_settings()["robot_base_url"])


def _require_current_base_url_unlocked(expected_base_url: object) -> str:
    expected = str(expected_base_url or "").strip().rstrip("/")
    current = _base_url()
    if expected and expected != current:
        raise ValueError("底盘连接已变更，请刷新地图后重试。")
    return current


def require_current_base_url(expected_base_url: object) -> str:
    """Resolve a chassis endpoint while rejecting stale browser writes."""
    with _ROBOT_CONNECTION_LOCK:
        return _require_current_base_url_unlocked(expected_base_url)


# --------------------------------------------------------------------------
# Low-level HTTP
# --------------------------------------------------------------------------

def _request(
    method: str,
    path: str,
    payload: Optional[Dict[str, object]] = None,
    *,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
    base_url: Optional[str] = None,
    accept: str = "application/json",
) -> Tuple[int, object]:
    request_base_url = base_url or _base_url()
    url = f"{request_base_url}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": accept}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = raw
        raise RobotApiError(
            f"机器人接口返回错误：{method} {path} → HTTP {error.code}"
            + (f"（{body}）" if body else ""),
            status_code=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise RobotApiError(
            f"无法连接机器人 {request_base_url}：{reason}", status_code=504
        ) from error
    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _request_bytes(
    method: str,
    path: str,
    *,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> bytes:
    url = f"{_base_url()}{path}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RobotApiError(
            f"机器人接口返回错误：{method} {path} → HTTP {error.code}",
            status_code=error.code,
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise RobotApiError(
            f"无法连接机器人 {_base_url()}：{reason}", status_code=504
        ) from error


# --------------------------------------------------------------------------
# System / connectivity
# --------------------------------------------------------------------------

def get_robot_info() -> Dict[str, object]:
    _, body = _request("GET", "/api/core/system/v1/robot/info")
    return body if isinstance(body, dict) else {}


def get_power_status() -> Dict[str, object]:
    _, body = _request("GET", "/api/core/system/v1/power/status")
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------
# Localization (live pose) and map image
# --------------------------------------------------------------------------

def get_current_pose() -> Dict[str, object]:
    """Live robot pose: {"x", "y", "z", "yaw", "pitch", "roll"} (meters/rad)."""
    _, body = _request("GET", "/api/core/slam/v1/localization/pose")
    return body if isinstance(body, dict) else {}


# --------------------------------------------------------------------------
# Read-only sensor telemetry
# --------------------------------------------------------------------------

def _finite_float(value: object) -> Optional[float]:
    """Return a finite JSON number as float; bool and malformed values fail."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _get_max_moving_speed_for(base_url: str) -> float:
    _, body = _request(
        "GET",
        "/api/core/system/v1/parameter?param=base.max_moving_speed",
        base_url=base_url,
        accept="text/plain",
    )
    if isinstance(body, dict):
        body = next(
            (body[key] for key in ("value", "data", "result") if key in body),
            None,
        )
    value = _finite_float(body)
    if value is None or value <= 0:
        raise RobotApiError("机器人返回的最大线速度无效。", status_code=502)
    return value


def get_max_moving_speed(*, expected_base_url: object = None) -> float:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        return _get_max_moving_speed_for(base_url)


def _normalize_pose_payload(
    raw: object,
    *,
    required: bool = False,
) -> Optional[Dict[str, object]]:
    """Normalize the flat Slamware Pose3D payload.

    The live spec uses flat ``x/y/z/yaw/pitch/roll`` fields.  Keeping the
    helper tolerant of omitted optional fields makes the collector useful with
    older firmware while still rejecting a frame with no usable x/y pose.
    """
    if not isinstance(raw, dict):
        if required:
            raise RobotApiError("机器人激光帧缺少观测位姿。", status_code=502)
        return None
    pose: Dict[str, object] = {}
    for name in ("x", "y", "z", "yaw", "pitch", "roll"):
        number = _finite_float(raw.get(name))
        if number is not None:
            pose[name] = number
    if required and ("x" not in pose or "y" not in pose):
        raise RobotApiError("机器人返回的观测位姿缺少 x/y。", status_code=502)
    return pose


def normalize_laser_scan(body: object) -> Dict[str, object]:
    """Validate and normalize one Slamware ``LaserScan`` response.

    Invalid frames are rejected before entering the shared cache.  Individual
    points marked ``valid=false`` are retained because the frontend may use
    their angular slots to render a scan envelope; callers should filter them
    when drawing obstacle hits.
    """
    if not isinstance(body, dict):
        raise RobotApiError("机器人激光接口返回格式异常。", status_code=502)
    pose = _normalize_pose_payload(body.get("pose"), required=True)
    raw_points = body.get("laser_points", [])
    if raw_points is None:
        raw_points = []
    if not isinstance(raw_points, list):
        raise RobotApiError("机器人激光帧的 laser_points 不是数组。", status_code=502)
    if len(raw_points) > _MAX_LASER_POINTS:
        raise RobotApiError("机器人激光点数量超过安全上限。", status_code=502)

    points: List[Dict[str, object]] = []
    for index, raw_point in enumerate(raw_points):
        if not isinstance(raw_point, dict):
            raise RobotApiError(
                f"机器人激光点[{index}] 格式异常。", status_code=502
            )
        distance = _finite_float(raw_point.get("distance"))
        angle = _finite_float(raw_point.get("angle"))
        if distance is None or distance < 0 or angle is None:
            raise RobotApiError(
                f"机器人激光点[{index}] 的距离/角度无效。", status_code=502
            )
        raw_valid = raw_point.get("valid", True)
        if isinstance(raw_valid, bool):
            valid = raw_valid
        elif raw_valid in (0, 1):
            # A few older gateways serialize booleans as 0/1.
            valid = bool(raw_valid)
        else:
            raise RobotApiError(
                f"机器人激光点[{index}] 的 valid 无效。", status_code=502
            )
        points.append(
            {"distance": distance, "angle": angle, "valid": valid}
        )
    return {"pose": pose or {}, "laser_points": points}


def get_laser_scan(
    timeout: Optional[float] = None,
) -> Dict[str, object]:
    """Fetch and normalize the current laser observation frame.

    ``LaserScan.pose`` is the pose at the instant the frame was observed and
    must be preferred when projecting points onto the map.  It is therefore
    kept separate from the independently sampled localization pose in the
    shared telemetry response.
    """
    request_timeout = (
        _TELEMETRY_REQUEST_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    _, body = _request(
        "GET",
        "/api/core/system/v1/laserscan",
        timeout=request_timeout,
    )
    return normalize_laser_scan(body)


def _quality_number(body: object) -> Optional[int]:
    if isinstance(body, bool):
        return None
    if isinstance(body, (int, float)):
        value = _finite_float(body)
        if value is None or value < 0 or value > 100 or not value.is_integer():
            return None
        return int(value)
    if isinstance(body, dict):
        # IntegerResponse is normally a bare JSON integer.  Accept common
        # gateway wrappers without exposing their transport-specific shape.
        for key in ("quality", "value", "data", "result"):
            if key in body:
                result = _quality_number(body[key])
                if result is not None:
                    return result
    return None


def get_localization_quality(
    timeout: Optional[float] = None,
) -> int:
    """Return Slamware localization quality in the documented 0..100 range."""
    request_timeout = (
        _TELEMETRY_REQUEST_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    _, body = _request(
        "GET",
        "/api/core/slam/v1/localization/quality",
        timeout=request_timeout,
    )
    quality = _quality_number(body)
    if quality is None:
        raise RobotApiError("机器人返回的定位质量无效。", status_code=502)
    return quality


def _sensor_info(points: object) -> Dict[str, object]:
    """Summarize the observed point envelope (not the physical lidar FOV)."""
    values = points if isinstance(points, list) else []
    valid_points = [
        item
        for item in values
        if isinstance(item, dict) and item.get("valid") is True
    ]
    distances = [
        float(item["distance"])
        for item in valid_points
        if isinstance(item.get("distance"), (int, float))
    ]
    angles = [
        float(item["angle"])
        for item in valid_points
        if isinstance(item.get("angle"), (int, float))
    ]
    observed_distances = [
        float(item["distance"])
        for item in values
        if isinstance(item, dict) and isinstance(item.get("distance"), (int, float))
    ]
    observed_angles = [
        float(item["angle"])
        for item in values
        if isinstance(item, dict) and isinstance(item.get("angle"), (int, float))
    ]
    return {
        "point_count": len(values),
        "valid_count": len(valid_points),
        "range_min": min(distances) if distances else None,
        "range_max": max(distances) if distances else None,
        "angle_min": min(angles) if angles else None,
        "angle_max": max(angles) if angles else None,
        "observed_range_max": max(observed_distances) if observed_distances else None,
        "observed_angle_min": min(observed_angles) if observed_angles else None,
        "observed_angle_max": max(observed_angles) if observed_angles else None,
    }


def _empty_telemetry_snapshot(
    received_at: float,
    monotonic_time: float,
    message: str = "尚未取得机器人实时激光数据。",
) -> Dict[str, object]:
    return {
        "seq": 0,
        "received_at": received_at,
        "age_ms": 0,
        "stale": True,
        "pose": None,
        "scan_pose": None,
        "localization_pose": None,
        "localization_quality": None,
        "laser_points": [],
        "sensor_info": _sensor_info([]),
        "partial": True,
        "errors": {"collector": message},
        "error": message,
        # Private fields are removed by _public_telemetry_snapshot().
        "_sample_monotonic": monotonic_time,
        "_scan_ok": False,
        "_next_refresh_monotonic": monotonic_time,
    }


def _public_telemetry_snapshot(
    snapshot: Dict[str, object],
    *,
    now: Optional[float] = None,
) -> Dict[str, object]:
    """Copy a cache entry and derive age/stale fields at response time."""
    monotonic_now = time.monotonic() if now is None else now
    raw_sample_time = snapshot.get("_sample_monotonic", monotonic_now)
    try:
        sample_time = float(raw_sample_time)
    except (TypeError, ValueError):
        sample_time = monotonic_now
    age_seconds = max(0.0, monotonic_now - sample_time)
    result = deepcopy(snapshot)
    for key in ("_sample_monotonic", "_scan_ok", "_next_refresh_monotonic"):
        result.pop(key, None)
    result["age_ms"] = int(round(age_seconds * 1000))
    result["stale"] = bool(
        result.get("stale")
        or not snapshot.get("_scan_ok", False)
        or age_seconds > _TELEMETRY_STALE_AFTER_SECONDS
    )
    return result


def _telemetry_error_payload(error: BaseException) -> Dict[str, object]:
    status_code = getattr(error, "status_code", None)
    result: Dict[str, object] = {"message": str(error) or error.__class__.__name__}
    if isinstance(status_code, int):
        result["status_code"] = status_code
    return result


def _fetch_telemetry_parts() -> Tuple[
    Dict[str, object], Dict[str, object]
]:
    """Fetch scan, pose and quality concurrently, isolating each failure."""
    futures = {
        "scan": _TELEMETRY_EXECUTOR.submit(
            get_laser_scan, _TELEMETRY_REQUEST_TIMEOUT_SECONDS
        ),
        "pose": _TELEMETRY_EXECUTOR.submit(
            get_current_pose_with_timeout, _TELEMETRY_REQUEST_TIMEOUT_SECONDS
        ),
        "quality": _TELEMETRY_EXECUTOR.submit(
            get_localization_quality, _TELEMETRY_REQUEST_TIMEOUT_SECONDS
        ),
    }
    results: Dict[str, object] = {}
    errors: Dict[str, object] = {}
    deadline = time.monotonic() + _TELEMETRY_REQUEST_TIMEOUT_SECONDS + 0.35
    for name, future in futures.items():
        remaining = max(0.01, deadline - time.monotonic())
        try:
            results[name] = future.result(timeout=remaining)
        except FutureTimeoutError:
            future.cancel()
            errors[name] = {
                "message": "读取机器人实时数据超时。",
                "status_code": 504,
            }
        except (RobotApiError, OSError, TimeoutError) as error:
            errors[name] = _telemetry_error_payload(error)
        except Exception as error:  # noqa: BLE001 - isolate malformed gateways
            errors[name] = _telemetry_error_payload(error)
    return results, errors


def get_current_pose_with_timeout(timeout: Optional[float] = None) -> Dict[str, object]:
    """Internal timeout-aware variant used by the telemetry collector."""
    request_timeout = (
        _TELEMETRY_REQUEST_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    _, body = _request(
        "GET",
        "/api/core/slam/v1/localization/pose",
        timeout=request_timeout,
    )
    if not isinstance(body, dict):
        raise RobotApiError("机器人返回的定位位姿格式异常。", status_code=502)
    pose = _normalize_pose_payload(body, required=True)
    return pose or {}


def _merge_telemetry_snapshot(
    previous: Optional[Dict[str, object]],
    results: Dict[str, object],
    errors: Dict[str, object],
    *,
    received_at: float,
    monotonic_time: float,
) -> Dict[str, object]:
    """Merge a refresh into the last good frame without hiding partial errors."""
    global _TELEMETRY_SEQUENCE

    if previous is None:
        snapshot = _empty_telemetry_snapshot(received_at, monotonic_time)
    else:
        snapshot = deepcopy(previous)

    scan = results.get("scan")
    scan_ok = bool(
        isinstance(scan, dict)
        and isinstance(scan.get("pose"), dict)
        and isinstance(scan.get("laser_points"), list)
    )
    if scan_ok:
        points = scan.get("laser_points")
        scan_pose = scan.get("pose")
        snapshot["laser_points"] = points if isinstance(points, list) else []
        snapshot["sensor_info"] = _sensor_info(snapshot["laser_points"])
        snapshot["scan_pose"] = scan_pose
        # Use the pose carried by the same laser frame for point projection.
        snapshot["pose"] = scan_pose
        snapshot["received_at"] = received_at
        snapshot["_sample_monotonic"] = monotonic_time
        snapshot["_scan_ok"] = True
        _TELEMETRY_SEQUENCE += 1
        snapshot["seq"] = _TELEMETRY_SEQUENCE

    pose = results.get("pose")
    if isinstance(pose, dict):
        snapshot["localization_pose"] = pose
        # When the scan request is the failed component, keep the robot icon
        # following the newest independent pose while the old point cloud is
        # marked stale.  A successful scan always wins above because its pose
        # is synchronized with the points being drawn.
        if not scan_ok:
            snapshot["pose"] = pose

    quality = results.get("quality")
    if isinstance(quality, int):
        snapshot["localization_quality"] = quality

    normalized_errors = {
        name: value
        for name, value in errors.items()
        if isinstance(value, dict)
    }
    snapshot["errors"] = normalized_errors
    snapshot["error"] = (
        "; ".join(
            str(value.get("message") or name)
            for name, value in normalized_errors.items()
        )
        if normalized_errors
        else None
    )
    # The laser frame is the critical component for obstacle rendering.  A
    # missing quality/auxiliary pose must be visible via ``partial``/``errors``
    # but should not hide a fresh laser frame from the map.
    snapshot["partial"] = bool(normalized_errors)
    snapshot["stale"] = not scan_ok
    snapshot["_next_refresh_monotonic"] = monotonic_time + (
        _TELEMETRY_FAILURE_BACKOFF_SECONDS
        if normalized_errors
        else _TELEMETRY_REFRESH_INTERVAL_SECONDS
    )
    return snapshot


def get_telemetry_snapshot(force: bool = False) -> Dict[str, object]:
    """Return a shared, normalized real-time map telemetry snapshot.

    Calls are single-flight: concurrent browser requests either reuse a recent
    frame or wait briefly for the in-progress refresh.  On chassis timeout or
    malformed data the previous frame is returned with ``stale=true`` and an
    ``errors`` mapping; no exception escapes to the HTTP handler.
    """
    global _TELEMETRY_SNAPSHOT, _TELEMETRY_REFRESHING
    global _TELEMETRY_NEXT_REFRESH_MONOTONIC, _TELEMETRY_SEQUENCE

    with _TELEMETRY_CONDITION:
        now = time.monotonic()
        if (
            _TELEMETRY_SNAPSHOT is not None
            and not force
            and now < _TELEMETRY_NEXT_REFRESH_MONOTONIC
        ):
            return _public_telemetry_snapshot(_TELEMETRY_SNAPSHOT, now=now)

        if _TELEMETRY_REFRESHING:
            deadline = now + _TELEMETRY_WAIT_SECONDS
            while _TELEMETRY_REFRESHING and time.monotonic() < deadline:
                _TELEMETRY_CONDITION.wait(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            if _TELEMETRY_SNAPSHOT is not None:
                return _public_telemetry_snapshot(_TELEMETRY_SNAPSHOT)
            return _public_telemetry_snapshot(
                _empty_telemetry_snapshot(time.time(), time.monotonic())
            )
        _TELEMETRY_REFRESHING = True
        previous = _TELEMETRY_SNAPSHOT
        generation = _TELEMETRY_GENERATION

    try:
        results, errors = _fetch_telemetry_parts()
        refreshed = _merge_telemetry_snapshot(
            previous,
            results,
            errors,
            received_at=time.time(),
            monotonic_time=time.monotonic(),
        )
    except Exception as error:  # noqa: BLE001 - cache failures as stale data
        now_wall = time.time()
        now_mono = time.monotonic()
        refreshed = _merge_telemetry_snapshot(
            previous,
            {},
            {"collector": _telemetry_error_payload(error)},
            received_at=now_wall,
            monotonic_time=now_mono,
        )

    with _TELEMETRY_CONDITION:
        # A settings update may have invalidated the cache while the three
        # requests were in flight.  Never publish a frame from the old robot.
        if generation != _TELEMETRY_GENERATION:
            _TELEMETRY_SNAPSHOT = None
            _TELEMETRY_NEXT_REFRESH_MONOTONIC = 0.0
            _TELEMETRY_SEQUENCE = 0
            _TELEMETRY_REFRESHING = False
            _TELEMETRY_CONDITION.notify_all()
            return _public_telemetry_snapshot(
                _empty_telemetry_snapshot(time.time(), time.monotonic())
            )
        _TELEMETRY_SNAPSHOT = refreshed
        try:
            _TELEMETRY_NEXT_REFRESH_MONOTONIC = float(
                refreshed.get("_next_refresh_monotonic", time.monotonic())
            )
        except (TypeError, ValueError):
            _TELEMETRY_NEXT_REFRESH_MONOTONIC = time.monotonic()
        _TELEMETRY_REFRESHING = False
        _TELEMETRY_CONDITION.notify_all()
        return _public_telemetry_snapshot(_TELEMETRY_SNAPSHOT)


# Name used by callers that want to make the endpoint intent explicit.
get_map_telemetry_snapshot = get_telemetry_snapshot


def get_home_pose() -> Optional[Dict[str, object]]:
    """Current charging-dock pose, or None if no dock is configured."""
    try:
        _, body = _request("GET", "/api/core/slam/v1/homepose")
    except RobotApiError as error:
        if error.status_code == 404:
            return None
        raise
    return _normalize_pose_payload(body, required=True)


_EXPLORE_HEADER_SIZE = 36

# Slamware 的 Bitmap8Bit 栅格是 signed int8（-128..127），官方显示规则是
# uint8(128 + cell)。Python bytes 会把同一位模式读成 0..255，因此查找表等价
# 于将无符号字节循环平移 128：占用趋近黑、未知为中灰、空闲趋近白。
_EXPLORE_GRAY_LUT = [(value + 128) & 0xFF for value in range(256)]


def get_map_image() -> Tuple[bytes, Dict[str, object]]:
    """Fetch the explored grid map and render it as a PNG.

    Response layout (little-endian), per the robot's own OpenAPI spec:
    bytes 0-3 origin_x (float), 4-7 origin_y (float), 8-11 width (uint32),
    12-15 height (uint32), 16-19 resolution meters/cell (float), 20-31
    reserved, 32-35 data_len (uint32), 36-end one grayscale byte per cell
    (占据栅格概率编码，见下方 _EXPLORE_GRAY_LUT 的注释)。
    """
    raw = _request_bytes("GET", "/api/core/slam/v1/maps/explore")
    if len(raw) < _EXPLORE_HEADER_SIZE:
        raise RobotApiError("地图数据长度异常，机器人可能尚未建图。", status_code=502)
    origin_x, origin_y, width, height, resolution = struct.unpack_from(
        "<ffIIf", raw, 0
    )
    (data_len,) = struct.unpack_from("<I", raw, 32)
    cells = raw[_EXPLORE_HEADER_SIZE : _EXPLORE_HEADER_SIZE + data_len]
    if width == 0 or height == 0 or len(cells) != width * height:
        raise RobotApiError(
            f"地图数据字节数与栅格数量不一致（期望 {width * height}，实际 {len(cells)}）。",
            status_code=502,
        )
    from PIL import Image  # 已是项目既有依赖（飞书截图渲染用）。

    image = Image.frombytes("L", (width, height), cells).point(_EXPLORE_GRAY_LUT)
    # 栅格数据第一行对应地图坐标 Y 最小（约定同 ROS occupancy grid），
    # 图片坐标系从上到下，需要垂直翻转才能让图片视觉朝向与世界坐标一致。
    image = image.transpose(Image.FLIP_TOP_BOTTOM)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    meta = {
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "resolution": resolution,
    }
    return buffer.getvalue(), meta


# --------------------------------------------------------------------------
# Motion actions
# --------------------------------------------------------------------------

def _finite_motion_value(value: object, name: str) -> float:
    """Validate a numeric motion argument before it reaches the chassis."""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是数字。") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字。")
    return number

def _create_action(
    action_name: str,
    options: Dict[str, object],
    *,
    base_url: Optional[str] = None,
) -> Dict[str, object]:
    _, body = _request(
        "POST",
        "/api/core/motion/v1/actions",
        {"action_name": f"slamtec.agent.actions.{action_name}", "options": options},
        base_url=base_url,
    )
    if not isinstance(body, dict):
        raise RobotApiError("机器人返回了非预期的动作响应。")
    return body


def _resolve_speed_ratio(
    speed_ratio: object,
    speed_mps: Optional[object],
    *,
    base_url: str,
) -> float:
    speed_value = _finite_motion_value(speed_ratio, "speed_ratio")
    # ponytail: cap app-issued navigation at the configured chassis maximum;
    # only raise this ceiling after an explicit field safety review.
    if speed_value < 0.1 or speed_value > 1:
        raise ValueError("speed_ratio 必须在 0.1~1 之间。")
    if speed_mps is None:
        return speed_value
    speed_mps_value = _finite_motion_value(speed_mps, "speed_mps")
    if speed_mps_value <= 0:
        raise ValueError("巡逻速度必须大于 0 m/s。")
    max_speed = _get_max_moving_speed_for(base_url)
    min_speed = max_speed * 0.1
    tolerance = 1e-9
    if (
        speed_mps_value < min_speed - tolerance
        or speed_mps_value > max_speed + tolerance
    ):
        raise ValueError(
            "巡逻速度必须在 "
            f"{min_speed:.3g}~{max_speed:.3g} m/s 之间。"
        )
    return min(1.0, max(0.1, speed_mps_value / max_speed))


def move_to(
    x: float,
    y: float,
    yaw: Optional[float] = None,
    precise: bool = True,
    speed_ratio: float = 0.8,
    mode: int = 0,
    *,
    speed_mps: Optional[float] = None,
    expected_base_url: object = None,
) -> Dict[str, object]:
    x_value = _finite_motion_value(x, "x")
    y_value = _finite_motion_value(y, "y")
    yaw_value = None if yaw is None else _finite_motion_value(yaw, "yaw")
    flags: List[str] = []
    if precise:
        flags.append("precise")
    if yaw_value is not None:
        flags.append("with_yaw")
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        speed_value = _resolve_speed_ratio(
            speed_ratio, speed_mps, base_url=base_url
        )
        move_options: Dict[str, object] = {
            "mode": mode,
            "flags": flags,
            "speed_ratio": speed_value,
        }
        if yaw_value is not None:
            move_options["yaw"] = yaw_value
        result = _create_action(
            "MoveToAction",
            {
                "target": {"x": x_value, "y": y_value, "z": 0},
                "move_options": move_options,
            },
            base_url=base_url,
        )
        return result


def series_move_to(
    targets: List[Dict[str, object]],
    speed_ratio: float = 0.8,
    *,
    speed_mps: Optional[float] = None,
    expected_base_url: object = None,
) -> Dict[str, object]:
    """Navigate a patrol queue as one continuous chassis action."""
    if not isinstance(targets, list) or not targets:
        raise ValueError("巡逻路线至少需要一个停留点。")
    normalized_targets: List[Dict[str, object]] = []
    for index, target in enumerate(targets, start=1):
        if not isinstance(target, dict):
            raise ValueError(f"第 {index} 个巡逻点格式无效。")
        normalized_targets.append(
            {
                "x": _finite_motion_value(target.get("x"), f"第 {index} 个点 x"),
                "y": _finite_motion_value(target.get("y"), f"第 {index} 个点 y"),
                "z": 0,
            }
        )
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        resolved_speed_ratio = _resolve_speed_ratio(
            speed_ratio, speed_mps, base_url=base_url
        )
        navigation_mode = 0
        try:
            _, raw_tracks = _request(
                "GET", "/api/core/artifact/v1/lines/tracks", base_url=base_url
            )
            tracks = (
                raw_tracks
                if isinstance(raw_tracks, list)
                else raw_tracks.get("lines", [])
                if isinstance(raw_tracks, dict)
                else []
            )
            if tracks:
                navigation_mode = 2
        except RobotApiError:
            # Missing/unsupported virtual tracks must not block free navigation.
            pass
        return _create_action(
            "SeriesMoveToAction",
            {
                "targets": normalized_targets,
                "move_options": {
                    "mode": navigation_mode,
                    "flags": [],
                    "acceptable_precision": 0.3,
                    "speed_ratio": resolved_speed_ratio,
                },
            },
            base_url=base_url,
        )


def go_home(
    dock: bool = True, *, expected_base_url: object = None
) -> Dict[str, object]:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        result = _create_action(
            "GoHomeAction",
            {
                "gohome_options": {
                    "flags": "dock" if dock else "no_dock",
                    "back_to_landing": True,
                    "charging_retry_count": 3,
                    "move_options": {"mode": 0},
                }
            },
            base_url=base_url,
        )
        return result


def recover_localization(
    max_recover_time_ms: int = 30000, *, expected_base_url: object = None
) -> Dict[str, object]:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        result = _create_action(
            "RecoverLocalizationAction",
            {
                "relocalization_options": {
                    "max_recover_time": max_recover_time_ms,
                    "recover_movement_type": "RotateOnly",
                }
            },
            base_url=base_url,
        )
        return result


def get_action_status(
    action_id: str, *, expected_base_url: object = None
) -> Dict[str, object]:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
    _, body = _request(
        "GET", f"/api/core/motion/v1/actions/{action_id}", base_url=base_url
    )
    return body if isinstance(body, dict) else {}


def get_current_action(*, expected_base_url: object = None) -> Dict[str, object]:
    """Return the chassis' current action from the read-only motion endpoint."""
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
    _, body = _request(
        "GET", "/api/core/motion/v1/actions/:current", base_url=base_url
    )
    return body if isinstance(body, dict) else {}


def get_remaining_path(*, expected_base_url: object = None) -> Dict[str, object]:
    """Return the remaining chassis-planned path for the active action."""
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
    _, body = _request(
        "GET", "/api/core/motion/v1/path", base_url=base_url
    )
    return body if isinstance(body, dict) else {"path_points": []}


def get_remaining_milestones(*, expected_base_url: object = None) -> Dict[str, object]:
    """Return the remaining targets for the active multi-point action."""
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
    _, body = _request(
        "GET", "/api/core/motion/v1/milestones", base_url=base_url
    )
    return body if isinstance(body, dict) else {"path_points": []}


def _cancel_current_action_for(base_url: str) -> None:
    try:
        _request("DELETE", "/api/core/motion/v1/actions/:current", base_url=base_url)
    except RobotApiError as error:
        if error.status_code != 404:
            raise


def cancel_current_action(*, expected_base_url: object = None) -> None:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        _cancel_current_action_for(base_url)


# --------------------------------------------------------------------------
# POI (stop points) — kept mirrored in a local cache so the map view can list
# them even when the robot is briefly unreachable; the robot side
# (/api/core/artifact/v1/pois) remains the source of truth.
# --------------------------------------------------------------------------

def _read_poi_caches() -> Dict[str, List[Dict[str, object]]]:
    if not ROBOT_MAP_POIS_FILE.is_file():
        return {}
    try:
        payload = json.loads(ROBOT_MAP_POIS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    # Legacy files were a bare list with no robot identity.  Reusing that list
    # after an address switch could send a different chassis to stale points.
    if not isinstance(payload, dict):
        return {}
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, dict):
        return {}
    caches = {
        endpoint: items
        for endpoint, items in endpoints.items()
        if isinstance(endpoint, str) and isinstance(items, list)
    }
    if payload.get("version") == 2:
        caches = {
            endpoint: _migrate_default_poi_order(items)
            for endpoint, items in caches.items()
        }
    return caches


def _migrate_default_poi_order(
    items: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Recover creation order encoded by this UI's legacy default names."""
    numbered: List[Tuple[int, Dict[str, object]]] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            return items
        name = str(item.get("name") or "")
        suffix = name[len("停留点") :] if name.startswith("停留点") else ""
        if not suffix.isdigit():
            return items
        sequence = int(suffix)
        if sequence in seen:
            return items
        seen.add(sequence)
        numbered.append((sequence, item))
    return [item for _, item in sorted(numbered, key=lambda entry: entry[0])]


def _merge_pois_in_cached_order(
    cached: List[Dict[str, object]], live: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    if not cached:
        # A fresh install has no local creation history.  The UI's default
        # names are the only stable order signal available from the chassis.
        return _migrate_default_poi_order(live)
    live_by_id = {
        str(item.get("id")): item
        for item in live
        if item.get("id") not in (None, "")
    }
    ordered: List[Dict[str, object]] = []
    seen: set[str] = set()
    for item in cached:
        poi_id = str(item.get("id") or "")
        if poi_id and poi_id in live_by_id and poi_id not in seen:
            ordered.append(live_by_id[poi_id])
            seen.add(poi_id)
    for item in live:
        poi_id = str(item.get("id") or "")
        if poi_id and poi_id in seen:
            continue
        ordered.append(item)
        if poi_id:
            seen.add(poi_id)
    return ordered


def _load_poi_cache(base_url: str) -> List[Dict[str, object]]:
    with _POI_CACHE_LOCK:
        return _read_poi_caches().get(base_url, [])


def _save_poi_cache(base_url: str, pois: List[Dict[str, object]]) -> None:
    with _POI_CACHE_LOCK:
        caches = _read_poi_caches()
        caches[base_url] = pois
        safe_write_text(
            ROBOT_MAP_POIS_FILE,
            json.dumps({"version": 3, "endpoints": caches}, ensure_ascii=False, indent=2)
            + "\n",
        )


def list_pois() -> List[Dict[str, object]]:
    """Prefer the robot's own POI list; fall back to the local cache if
    the robot is unreachable so the map view still shows something."""
    base_url = _base_url()
    cached = _load_poi_cache(base_url)
    try:
        _, body = _request(
            "GET", "/api/core/artifact/v1/pois", base_url=base_url
        )
    except RobotApiError:
        return cached
    items = body if isinstance(body, list) else []
    normalized = [_normalize_poi(item) for item in items if isinstance(item, dict)]
    ordered = _merge_pois_in_cached_order(cached, normalized)
    _save_poi_cache(base_url, ordered)
    return ordered


def _normalize_poi(raw: Dict[str, object]) -> Dict[str, object]:
    # PoseEntry (live spec): {"id", "pose": {"x","y","yaw"}, "metadata": {...}}.
    # display_name/type both live inside metadata — there is no top-level
    # "type" field, unlike the printed RESTful API manual's example payload.
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    pose = raw.get("pose") if isinstance(raw.get("pose"), dict) else {}
    return {
        "id": raw.get("id"),
        "name": metadata.get("display_name") or raw.get("id"),
        "type": metadata.get("type") or "",
        "x": pose.get("x", 0),
        "y": pose.get("y", 0),
        "yaw": pose.get("yaw", 0),
    }


def create_poi(
    name: str,
    x: float,
    y: float,
    yaw: float = 0,
    poi_type: str = "",
    *,
    expected_base_url: object = None,
) -> Dict[str, object]:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        poi_id = str(uuid.uuid4())
        metadata: Dict[str, object] = {"display_name": name}
        if poi_type:
            metadata["type"] = poi_type
        payload: Dict[str, object] = {
            "id": poi_id,
            "pose": {"x": x, "y": y, "yaw": yaw},
            "metadata": metadata,
        }
        # 添加 POI 成功仅返回 200，响应体未定义结构（多数情况下为空），因此以
        # 请求体本身作为权威结果，而不是尝试解析一个可能不存在的响应体。
        _request("POST", "/api/core/artifact/v1/pois", payload, base_url=base_url)
        poi = _normalize_poi(payload)
        with _POI_CACHE_LOCK:
            cache = _load_poi_cache(base_url)
            cache.append(poi)
            _save_poi_cache(base_url, cache)
        return poi


def delete_poi(poi_id: str, *, expected_base_url: object = None) -> None:
    with _ROBOT_CONNECTION_LOCK:
        base_url = _require_current_base_url_unlocked(expected_base_url)
        _request(
            "DELETE", f"/api/core/artifact/v1/pois/{poi_id}", base_url=base_url
        )
        with _POI_CACHE_LOCK:
            cache = [
                item
                for item in _load_poi_cache(base_url)
                if item.get("id") != poi_id
            ]
            _save_poi_cache(base_url, cache)


# --------------------------------------------------------------------------
# Zones (rectangle areas) and virtual walls/tracks (lines) — vector config
# elements. These are NOT baked into the /maps/explore raster bytes at all;
# RS draws them as an overlay on top of the map, so this tool has to fetch
# and draw them separately too.
# --------------------------------------------------------------------------

_RECTANGLE_AREA_USAGES: Tuple[str, ...] = (
    "forbidden_area",
    "dangerous_area",
    "elevator_area",
    "coverage_area",
    "maintenance_area",
    "sensor_disable_area",
    "restricted_area",
)
_LINE_USAGES: Tuple[str, ...] = ("walls", "tracks")


def get_zones() -> Dict[str, object]:
    """禁行/危险/电梯等矩形区域 + 虚拟墙/虚拟轨道线段，一次性拉全部。

    某个 usage 类型没有配置或机器人固件不支持时，该类型请求会报错——按类型
    单独 try/except，跳过失败的那一类，不影响其余类型正常显示。
    """
    areas: List[Dict[str, object]] = []
    for usage in _RECTANGLE_AREA_USAGES:
        try:
            _, body = _request(
                "GET", f"/api/core/artifact/v1/rectangle-areas/{usage}"
            )
        except RobotApiError:
            continue
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("usage", usage)
                    areas.append(item)

    lines: List[Dict[str, object]] = []
    for usage in _LINE_USAGES:
        try:
            _, body = _request("GET", f"/api/core/artifact/v1/lines/{usage}")
        except RobotApiError:
            continue
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    item = dict(item)
                    item["usage"] = usage
                    lines.append(item)

    return {"areas": areas, "lines": lines}


# --------------------------------------------------------------------------
# Events (obstacles, low battery, elevator, health alarms, ...)
# --------------------------------------------------------------------------

def get_events() -> List[Dict[str, object]]:
    _, body = _request("GET", "/api/platform/v1/events")
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("events"), list):
        return body["events"]
    return []
