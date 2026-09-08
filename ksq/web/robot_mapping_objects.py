"""Mapping object edits using the connected chassis' documented endpoints.

The caller holds the connection lock and checks that mapping and motion are idle.
Coordinates are metres, yaw is radians, and rectangle x/y denotes its centre.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import uuid

from ksq.web import robot_map_api as api


_PATHS = {
    "poi": "/api/core/artifact/v1/pois",
    "dock": "/api/core/slam/v1/homedocks",
    "wall": "/api/core/artifact/v1/lines/walls",
    "track": "/api/core/artifact/v1/lines/tracks",
    "forbidden": "/api/core/artifact/v1/rectangle-areas/forbidden_area",
    "danger": "/api/core/artifact/v1/rectangle-areas/dangerous_area",
    "maintenance": "/api/core/artifact/v1/rectangle-areas/maintenance_area",
    "pose": "/api/core/slam/v1/localization/pose",
    "origin": "/api/core/slam/v1/maps/origin",
}
_LINES = {"wall", "track"}
_AREAS = {"forbidden", "danger", "maintenance"}
_MAX_OBJECTS = 10000


def _kind(payload: object) -> str:
    if (not isinstance(payload, dict) or not isinstance(payload.get("type"), str)
            or payload["type"] not in _PATHS):
        raise ValueError("不支持的部署对象类型。")
    return payload["type"]


def _number(value: object, field: str) -> float:
    return api._finite_motion_value(value, field)


def _id(value: object, kind: str) -> object:
    if kind in _LINES | _AREAS:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
            value = int(value)
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("部署对象编号无效。")
    elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("部署对象编号无效。")
    return value


def _raw_id(raw: dict, kind: str) -> object:
    return _id(raw.get("id", raw.get("lineid") if kind in _LINES else None), kind)


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("对象名称须为 1 至 128 个字符。")
    if any(ord(char) < 32 for char in value):
        raise ValueError("对象名称不能包含控制字符。")
    return value.strip()


def _metadata(raw: dict) -> dict:
    value = raw.get("metadata", {})
    if not isinstance(value, dict):
        raise api.RobotApiError("底盘返回的对象元数据格式无效。")
    return deepcopy(value)


def _read(kind: str, base_url: str, *, timeout: float = api._REQUEST_TIMEOUT_SECONDS) -> list[dict]:
    _, body = api._request("GET", _PATHS[kind], base_url=base_url, timeout=timeout)
    if not isinstance(body, list) or len(body) > _MAX_OBJECTS:
        raise api.RobotApiError("底盘返回的部署对象列表无效。")
    seen = set()
    for item in body:
        if not isinstance(item, dict):
            raise api.RobotApiError("底盘返回的部署对象格式无效。")
        item_id = _raw_id(item, kind)
        if item_id in seen:
            raise api.RobotApiError("底盘返回了重复的部署对象编号。")
        seen.add(item_id)
    return body


def _find(items: list[dict], kind: str, item_id: object) -> dict:
    for item in items:
        if _raw_id(item, kind) == item_id:
            return item
    raise ValueError("对象已不存在，请刷新后重试。")


def _normal(kind: str, raw: dict) -> dict:
    metadata = _metadata(raw)
    item = {"type": kind, "id": _raw_id(raw, kind)}
    item["name"] = str(metadata.get("display_name") or f"{kind} {item['id']}")[:128]
    if kind in {"poi", "dock", "pose"}:
        pose = raw if kind == "pose" else raw.get("pose")
        if not isinstance(pose, dict):
            raise api.RobotApiError("底盘返回的对象位姿格式无效。")
        item.update(x=_number(pose.get("x"), "x"), y=_number(pose.get("y"), "y"),
                    yaw=_number(pose.get("yaw", 0), "yaw"))
    elif kind in _LINES:
        start, end = raw.get("start"), raw.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise api.RobotApiError("底盘返回的线段端点格式无效。")
        item.update(x=_number(start.get("x"), "x"), y=_number(start.get("y"), "y"),
                    endX=_number(end.get("x"), "endX"), endY=_number(end.get("y"), "endY"))
        item["curved"] = any(key in metadata for key in ("control_point1", "control_point2"))
    elif kind in _AREAS:
        area = raw.get("area")
        if not isinstance(area, dict) or not all(isinstance(area.get(key), dict) for key in ("start", "end")):
            raise api.RobotApiError("底盘返回的矩形区域格式无效。")
        sx, sy = (_number(area["start"].get(key), key) for key in ("x", "y"))
        ex, ey = (_number(area["end"].get(key), key) for key in ("x", "y"))
        width = _number(math.hypot(ex - sx, ey - sy), "width")
        height = _number(2 * _number(area.get("half_width"), "half_width"), "height")
        if width <= 0 or height <= 0:
            raise api.RobotApiError("底盘返回了空矩形区域。")
        item.update(x=sx / 2 + ex / 2, y=sy / 2 + ey / 2, width=width, height=height,
                    yaw=math.atan2(ey - sy, ex - sx))
        if kind == "danger":
            item["dangerous_area_type"] = str(metadata.get("dangerous_area_type", "1"))
            if "max_line_speed" in metadata:
                item["speed_mps"] = _number(metadata["max_line_speed"], "speed_mps")
        elif kind == "forbidden":
            item["escape_distance"] = _number(metadata.get("escape_distance", 0), "escape_distance")
    return item


def _cache_pois(raw: list[dict], base_url: str) -> list[dict]:
    with api._POI_CACHE_LOCK:
        ordered = api._merge_pois_in_cached_order(
            api._load_poi_cache(base_url), [api._normalize_poi(item) for item in raw]
        )
        api._save_poi_cache(base_url, ordered)
    by_id = {item["id"]: item for item in raw}
    return [by_id[item["id"]] for item in ordered]


def list_objects(base_url: str) -> dict:
    objects, errors = [], {}
    for kind in _PATHS:
        if kind == "origin":
            continue  # Firmware exposes no origin GET; never invent a current value.
        try:
            if kind == "pose":
                _, raw = api._request("GET", _PATHS[kind], base_url=base_url,
                                      timeout=api._TELEMETRY_REQUEST_TIMEOUT_SECONDS)
                if not isinstance(raw, dict):
                    raise api.RobotApiError("底盘返回的机器人位姿格式无效。")
                items = [{**raw, "id": "pose"}]
            else:
                items = _read(kind, base_url, timeout=api._TELEMETRY_REQUEST_TIMEOUT_SECONDS)
                if kind == "poi":
                    items = _cache_pois(items, base_url)
            normalized = [_normal(kind, item) for item in items]
            objects.extend(normalized)
        except (api.RobotApiError, ValueError) as error:
            errors[kind] = str(error)
    return {"objects": objects, "errors": errors,
            "capabilities": {"origin_read": False, "origin_delete": False, "pose_delete": False}}


def _write(method: str, path: str, payload: object, base_url: str, *, boolean: bool = True) -> None:
    try:
        encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("对象数据必须为有限数值且符合 JSON 格式。") from error
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError("部署对象数据过大。")
    _, body = api._request(method, path, payload, base_url=base_url)
    if boolean and body is not True:
        raise api.RobotApiError("底盘未确认对象变更，请刷新后重试。")


def _validated(payload: dict, kind: str) -> dict:
    item = {"type": kind, "x": _number(payload.get("x"), "x"),
            "y": _number(payload.get("y"), "y")}
    if "id" in payload and payload["id"] not in (None, ""):
        item["id"] = _id(payload["id"], kind)
    if kind == "origin":
        if "yaw" in payload and _number(payload["yaw"], "yaw") != 0:
            raise ValueError("当前固件的地图原点接口不支持旋转。")
        return item
    item["name"] = _name(payload.get("name", "机器人位姿" if kind == "pose" else None))
    if kind in _LINES:
        item.update(endX=_number(payload.get("endX"), "endX"),
                    endY=_number(payload.get("endY"), "endY"))
        if item["x"] == item["endX"] and item["y"] == item["endY"]:
            raise ValueError("线段起点与终点不能重合。")
    else:
        item["yaw"] = _number(payload.get("yaw", 0), "yaw")
    if kind in _AREAS:
        for key in ("width", "height"):
            item[key] = _number(payload.get(key), key)
            if item[key] <= 0:
                raise ValueError("矩形宽度和高度必须大于零。")
    if kind == "danger":
        item["dangerous_area_type"] = str(payload.get("dangerous_area_type", "1"))
        if item["dangerous_area_type"] not in {"0", "1"}:
            raise ValueError("危险区域类型必须为 0 或 1。")
        if "speed_mps" in payload:
            item["speed_mps"] = _number(payload["speed_mps"], "speed_mps")
            if item["speed_mps"] <= 0:
                raise ValueError("危险区域限速必须大于零。")
    if kind == "forbidden":
        item["escape_distance"] = _number(payload.get("escape_distance", 0), "escape_distance")
        if item["escape_distance"] < 0:
            raise ValueError("禁行区域逃逸距离不能小于零。")
    return item


def save_object(payload: dict, base_url: str) -> dict:
    kind = _kind(payload)
    item = _validated(payload, kind)
    path = _PATHS[kind]
    if kind in {"pose", "origin"}:
        if payload.get("confirm") is not True:
            raise ValueError("设置机器人位姿或地图原点须明确确认：confirm=true。")
        if kind == "origin":
            body = {"new_origin": {"x": item["x"], "y": item["y"]}}
        else:
            body = {key: item[key] for key in ("x", "y", "yaw")}
        _write("PUT", path, body, base_url, boolean=False)
        api._PATROL_TRACK_PLANS.pop(base_url, None)
        api._invalidate_telemetry_cache()
        return {"object": {**item, "id": kind} if kind == "pose" else None}

    items = _read(kind, base_url)
    existing = _find(items, kind, item["id"]) if "id" in item else None
    metadata = _metadata(existing) if existing is not None else {}
    if existing is not None and kind not in _LINES:
        previous = _normal(kind, existing)
        for key in ("yaw", "escape_distance", "dangerous_area_type"):
            if key not in payload and key in previous:
                item[key] = previous[key]
    metadata["display_name"] = item["name"]
    if kind in {"poi", "dock"}:
        item["id"] = item.get("id", str(uuid.uuid4()))
        pose = deepcopy(existing.get("pose", {})) if existing is not None else {}
        pose.update({key: item[key] for key in ("x", "y", "yaw")})
        body = {"pose": pose, "metadata": metadata}
        api._PATROL_TRACK_PLANS.pop(base_url, None)
        if existing is None:
            _write("POST", path, {**body, "id": item["id"]}, base_url, boolean=kind == "dock")
            items.append({**body, "id": item["id"]})
        else:
            _write("PUT", f"{path}/{item['id']}", body, base_url)
            items = [{**raw, **body} if _raw_id(raw, kind) == item["id"] else raw for raw in items]
        if kind == "poi":
            _cache_pois(items, base_url)
        return {"object": item}

    if kind in _LINES:
        if kind == "track" and any(key in metadata for key in ("control_point1", "control_point2")):
            raise ValueError("曲线轨道包含控制点，不能按直线修改；请使用支持曲线编辑的工具。")
        body = {"start": {"x": item["x"], "y": item["y"]},
                "end": {"x": item["endX"], "y": item["endY"]}, "metadata": metadata}
    else:
        dx, dy = math.cos(item["yaw"]) * item["width"] / 2, math.sin(item["yaw"]) * item["width"] / 2
        body = {"area": {"start": {"x": item["x"] - dx, "y": item["y"] - dy},
                         "end": {"x": item["x"] + dx, "y": item["y"] + dy},
                         "half_width": item["height"] / 2}, "metadata": metadata}
        if kind == "danger":
            metadata["dangerous_area_type"] = item["dangerous_area_type"]
            if "speed_mps" in item:
                metadata["max_line_speed"] = str(item["speed_mps"])
            if "max_line_speed" not in metadata:
                raise ValueError("新增危险限速区必须填写 speed_mps。")
        elif kind == "forbidden":
            metadata["escape_distance"] = str(item["escape_distance"])
    api._PATROL_TRACK_PLANS.pop(base_url, None)
    if existing is not None:
        if kind in _LINES:
            changed = [{**raw, **body, "id": item["id"]} if _raw_id(raw, kind) == item["id"] else raw for raw in items]
            _write("PUT", path, changed, base_url)
        else:
            _write("PUT", f"{path}/{item['id']}", body, base_url)
        return {"object": item}

    _write("POST", path, [body] if kind in _LINES else body, base_url)
    current = _read(kind, base_url)
    old_ids = {_raw_id(raw, kind) for raw in items}
    added = [raw for raw in current if _raw_id(raw, kind) not in old_ids]
    if len(added) != 1:
        raise api.RobotApiError("底盘已接受变更，但新增对象编号尚未确认，请刷新后重试。")
    return {"object": _normal(kind, added[0])}


def delete_object(payload: dict, base_url: str) -> dict:
    kind = _kind(payload)
    if kind in {"pose", "origin"}:
        raise ValueError("当前固件不支持删除机器人位姿或地图原点。")
    item_id = _id(payload.get("id"), kind)
    items = _read(kind, base_url)
    _find(items, kind, item_id)
    api._PATROL_TRACK_PLANS.pop(base_url, None)
    _write("DELETE", f"{_PATHS[kind]}/{item_id}", None, base_url)
    remaining = _read(kind, base_url)
    expected = {_raw_id(raw, kind) for raw in items} - {item_id}
    actual = {_raw_id(raw, kind) for raw in remaining}
    if item_id in actual or not expected <= actual:
        raise api.RobotApiError("对象删除回读校验失败，请刷新后重试。")
    if kind == "poi":
        _cache_pois(remaining, base_url)
    return {"deleted": {"type": kind, "id": item_id}}
