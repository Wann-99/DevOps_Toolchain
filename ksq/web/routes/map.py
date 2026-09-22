from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urlparse

from ksq.robot import service as robot_map_api
from ksq.robot.service import RobotApiError
from ksq.web.request_utils import _drain_request_body, _expected_robot_base_url, _parse_finite_float, read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/map/settings":
        handler._send_json(HTTPStatus.OK, robot_map_api.load_settings())
        return True
    if path == "/api/map/robot-info":
        try:
            handler._send_json(HTTPStatus.OK, robot_map_api.get_robot_info())
        except RobotApiError as error:
            handler._send_json(
                HTTPStatus(error.status_code)
                if 400 <= error.status_code < 600
                else HTTPStatus.BAD_GATEWAY,
                {"error": str(error)},
            )
        return True
    if path == "/api/map/speed-limit":
        query = parse_qs(parsed.query)
        expected_base_url = (
            query.get("expected_robot_base_url") or [None]
        )[0]
        try:
            max_speed = robot_map_api.get_max_moving_speed(
                expected_base_url=expected_base_url
            )
            handler._send_json(
                HTTPStatus.OK,
                {
                    "min_speed_mps": round(max_speed * 0.1, 6),
                    "max_speed_mps": max_speed,
                    "default_speed_mps": round(max_speed * 0.8, 6),
                },
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RobotApiError as error:
            handler._send_json(
                HTTPStatus(error.status_code)
                if 400 <= error.status_code < 600
                else HTTPStatus.BAD_GATEWAY,
                {"error": str(error)},
            )
        return True
    if path == "/api/map/power":
        try:
            handler._send_json(HTTPStatus.OK, robot_map_api.get_power_status())
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path == "/api/map/pois":
        try:
            handler._send_json(HTTPStatus.OK, {"pois": robot_map_api.list_pois()})
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path in ("/api/map/pose", "/api/map/health"):
        query = parse_qs(parsed.query)
        expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
        reader = (
            robot_map_api.get_current_pose
            if path == "/api/map/pose"
            else robot_map_api.get_robot_health
        )
        try:
            handler._send_json(
                HTTPStatus.OK, reader(expected_base_url=expected_base_url)
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RobotApiError as error:
            handler._send_json(
                HTTPStatus(error.status_code)
                if 400 <= error.status_code < 600
                else HTTPStatus.BAD_GATEWAY,
                {"error": str(error)},
            )
        return True
    if path == "/api/map/home-pose":
        try:
            handler._send_json(
                HTTPStatus.OK, {"pose": robot_map_api.get_home_pose()}
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path == "/api/map/telemetry":
        # The map collector returns one coherent scan/pose/quality snapshot.
        # Keep this endpoint read-only and expose stale snapshots as JSON so
        # the UI can render the last known frame without treating a transient
        # chassis timeout as a missing response.
        try:
            handler._send_json(
                HTTPStatus.OK, robot_map_api.get_telemetry_snapshot()
            )
        except RobotApiError as error:
            handler._send_json(
                HTTPStatus(error.status_code)
                if 400 <= error.status_code < 600
                else HTTPStatus.BAD_GATEWAY,
                {"error": str(error)},
            )
        return True
    if path == "/api/map/image":
        try:
            png_bytes, meta = robot_map_api.get_map_image()
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(png_bytes)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Map-Origin-X", str(meta["origin_x"]))
        handler.send_header("X-Map-Origin-Y", str(meta["origin_y"]))
        handler.send_header("X-Map-Resolution", str(meta["resolution"]))
        handler.send_header("X-Map-Width", str(meta["width"]))
        handler.send_header("X-Map-Height", str(meta["height"]))
        handler.end_headers()
        handler.wfile.write(png_bytes)
        return True
    if path == "/api/map/zones":
        try:
            handler._send_json(HTTPStatus.OK, robot_map_api.get_zones())
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path == "/api/map/events":
        try:
            handler._send_json(HTTPStatus.OK, {"events": robot_map_api.get_events()})
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path == "/api/map/current-action":
        query = parse_qs(parsed.query)
        expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
        try:
            handler._send_json(
                HTTPStatus.OK,
                {
                    "active": True,
                    "action": robot_map_api.get_current_action(
                        expected_base_url=expected_base_url
                    ),
                },
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RobotApiError as error:
            # Slamware returns 404 when no action is running; expose that
            # normal idle state as data instead of a failed status request.
            if error.status_code == 404:
                handler._send_json(
                    HTTPStatus.OK, {"active": False, "action": None}
                )
            else:
                handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path in ("/api/map/path", "/api/map/milestones"):
        query = parse_qs(parsed.query)
        expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
        reader = (
            robot_map_api.get_remaining_path
            if path == "/api/map/path"
            else robot_map_api.get_remaining_milestones
        )
        try:
            handler._send_json(
                HTTPStatus.OK,
                reader(expected_base_url=expected_base_url),
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RobotApiError as error:
            if error.status_code == 404:
                handler._send_json(HTTPStatus.OK, {"path_points": []})
            else:
                handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    if path.startswith("/api/map/actions/"):
        action_id = unquote(path[len("/api/map/actions/") :]).strip()
        if not action_id or "/" in action_id:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "action_id 无效。"})
            return True
        query = parse_qs(parsed.query)
        expected_base_url = (query.get("expected_robot_base_url") or [None])[0]
        try:
            handler._send_json(
                HTTPStatus.OK,
                robot_map_api.get_action_status(
                    action_id, expected_base_url=expected_base_url
                ),
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        return True
    return False


def handle_put(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/map/settings":
        payload = read_json_body(handler)
        _expected_robot_base_url(payload)
        try:
            settings = robot_map_api.save_settings(payload)
        except robot_map_api.RobotConnectionSwitchRequired as error:
            handler._send_json(
                HTTPStatus.CONFLICT,
                {"error": str(error), "code": "force_switch_required"},
            )
            return True
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, settings)
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/map/health/clear":
        if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            _drain_request_body(handler)
            handler._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "请求必须使用 application/json。"})
            return True
        payload = read_json_body(handler)
        try:
            result = robot_map_api.clear_robot_health(
                expected_base_url=_expected_robot_base_url(payload),
                confirm=payload.get("confirm"),
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus(error.status_code), {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/navigate":
        payload = read_json_body(handler)
        try:
            x = _parse_finite_float(payload["x"], "x")
            y = _parse_finite_float(payload["y"], "y")
        except (KeyError, TypeError, ValueError):
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "x/y 必须是数字。"})
            return True
        yaw = payload.get("yaw")
        if yaw is not None:
            yaw = _parse_finite_float(yaw, "yaw")
        speed_mps = None
        if "speed_mps" in payload:
            if "speed_ratio" in payload:
                raise ValueError("speed_mps 与 speed_ratio 不能同时设置。")
            speed_mps = _parse_finite_float(
                payload["speed_mps"], "speed_mps"
            )
            if speed_mps <= 0:
                raise ValueError("巡逻速度必须大于 0 m/s。")
        speed_ratio = _parse_finite_float(
            payload.get("speed_ratio", 0.8), "speed_ratio"
        )
        if speed_ratio < 0.1 or speed_ratio > 1:
            raise ValueError("speed_ratio 必须在 0.1~1 之间。")
        try:
            move_options = {
                "yaw": yaw,
                "precise": bool(payload.get("precise", True)),
                "speed_ratio": speed_ratio,
                "expected_base_url": _expected_robot_base_url(payload),
            }
            if speed_mps is not None:
                move_options["speed_mps"] = speed_mps
            result = robot_map_api.move_to(x, y, **move_options)
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path in ("/api/map/patrol", "/api/map/patrol/plan"):
        payload = read_json_body(handler)
        try:
            options = {
                "loop": payload.get("loop", False),
                "track_priority": payload.get("track_priority", False),
                "expected_base_url": _expected_robot_base_url(payload),
            }
            if path == "/api/map/patrol/plan":
                result = robot_map_api.plan_patrol(payload.get("targets"), **options)
            else:
                speed_mps = _parse_finite_float(payload.get("speed_mps"), "speed_mps")
                if speed_mps <= 0:
                    raise ValueError("巡逻速度必须大于 0 m/s。")
                result = robot_map_api.series_move_to(
                    payload.get("targets"), speed_mps=speed_mps,
                    plan_id=payload.get("plan_id"), **options,
                )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return True
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/tracks/delete":
        payload = read_json_body(handler)
        try:
            result = robot_map_api.delete_tracks(
                payload.get("tracks"), expected_base_url=_expected_robot_base_url(payload),
            )
        except ValueError as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return True
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/actions/cancel":
        payload = read_json_body(handler)
        try:
            robot_map_api.cancel_current_action(
                expected_base_url=_expected_robot_base_url(payload)
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, {"ok": True})
        return True
    if path == "/api/map/gohome":
        payload = read_json_body(handler)
        try:
            result = robot_map_api.go_home(
                expected_base_url=_expected_robot_base_url(payload)
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/relocate":
        payload = read_json_body(handler)
        try:
            result = robot_map_api.recover_localization(
                expected_base_url=_expected_robot_base_url(payload)
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/pois":
        payload = read_json_body(handler)
        name = str(payload.get("name") or "").strip()
        if not name:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "停留点名称不能为空。"})
            return True
        try:
            x = _parse_finite_float(payload["x"], "x")
            y = _parse_finite_float(payload["y"], "y")
        except (KeyError, TypeError, ValueError):
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "x/y 必须是数字。"})
            return True
        try:
            result = robot_map_api.create_poi(
                name,
                x,
                y,
                expected_base_url=_expected_robot_base_url(payload),
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    if path == "/api/map/pois/delete":
        payload = read_json_body(handler)
        poi_id = str(payload.get("id") or "").strip()
        if not poi_id:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": "id 不能为空。"})
            return True
        try:
            robot_map_api.delete_poi(
                poi_id,
                expected_base_url=_expected_robot_base_url(payload),
            )
        except RobotApiError as error:
            handler._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, {"ok": True})
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'GET': ('/api/map/settings', '/api/map/robot-info', '/api/map/speed-limit', '/api/map/power', '/api/map/pois', '/api/map/pose', '/api/map/health', '/api/map/home-pose', '/api/map/telemetry', '/api/map/image', '/api/map/zones', '/api/map/events', '/api/map/current-action', '/api/map/path', '/api/map/milestones', '/api/map/actions/{action_id:path}'), 'PUT': ('/api/map/settings',), 'POST': ('/api/map/health/clear', '/api/map/navigate', '/api/map/patrol', '/api/map/patrol/plan', '/api/map/tracks/delete', '/api/map/actions/cancel', '/api/map/gohome', '/api/map/relocate', '/api/map/pois', '/api/map/pois/delete')}
