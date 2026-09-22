from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

from ksq.robot import mapping as robot_mapping_api
from ksq.robot import mapping_objects as robot_mapping_objects
from ksq.robot import service as robot_map_api
from ksq.robot.service import RobotApiError
from ksq.web.request_utils import _expected_robot_base_url, _request_content_length, read_json_body


def handle_get(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path in ("/api/map/mapping", "/api/map/mapping/objects", "/api/map/mapping/export", "/api/map/mapping/upload-progress"):
        try:
            query = parse_qs(urlparse(handler.path).query)
            expected = _expected_robot_base_url({
                "expected_robot_base_url": query.get("expected_robot_base_url", [""])[0]
            })
            if path.endswith("/upload-progress"):
                handler._send_json(HTTPStatus.OK, robot_mapping_api.get_upload_progress(
                    expected, query.get("upload_id", [None])[0],
                ))
            elif path == "/api/map/mapping":
                handler._send_json(HTTPStatus.OK, robot_mapping_api.get_status(expected))
            elif path.endswith("/objects"):
                with robot_map_api._ROBOT_CONNECTION_LOCK:
                    base_url = robot_map_api.require_current_base_url(expected)
                result = robot_mapping_objects.list_objects(base_url)
                with robot_map_api._ROBOT_CONNECTION_LOCK:
                    robot_map_api.require_current_base_url(base_url)
                handler._send_json(HTTPStatus.OK, result)
            else:
                content = robot_mapping_api.export_map(expected)
                handler.send_response(HTTPStatus.OK)
                handler.send_header("Content-Type", "application/octet-stream")
                handler.send_header("Content-Disposition", 'attachment; filename="map.stcm"')
                handler.send_header("Content-Length", str(len(content)))
                handler.send_header("Cache-Control", "no-store")
                handler.end_headers()
                handler.wfile.write(content)
        except RobotApiError as error:
            handler._send_json(HTTPStatus(error.status_code), {"error": str(error)})
        except (ValueError, OSError) as error:
            handler._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        return True
    return False


def handle_post(handler, session) -> bool:
    parsed = urlparse(handler.path)
    path = parsed.path
    if path == "/api/map/mapping":
        if _request_content_length(handler) > 45 * 1024 * 1024:
            handler.close_connection = True
            handler._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "地图文件过大，最多支持 32 MiB。"})
            return True
        payload = read_json_body(handler)
        _expected_robot_base_url(payload)
        try:
            result = robot_mapping_api.execute(payload)
        except RobotApiError as error:
            handler._send_json(HTTPStatus(error.status_code), {"error": str(error)})
            return True
        handler._send_json(HTTPStatus.OK, result)
        return True
    return False

# Explicit FastAPI registration; existing parsing preserves the HTTP contract.
ROUTES = {'GET': ('/api/map/mapping', '/api/map/mapping/objects', '/api/map/mapping/export', '/api/map/mapping/upload-progress'), 'POST': ('/api/map/mapping',)}
