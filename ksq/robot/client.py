"""Chassis HTTP transport; callers choose and validate the active endpoint."""

from __future__ import annotations

from typing import Optional, Tuple
import json
import urllib.error
import urllib.request


class RobotApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def request(
    method: str,
    path: str,
    payload: Optional[object] = None,
    *,
    timeout: float = 8,
    base_url: str,
    accept: str = "application/json",
) -> Tuple[int, object]:
    request_base_url = base_url
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


def request_bytes(
    method: str,
    path: str,
    *,
    timeout: float = 8,
    base_url: str,
) -> bytes:
    url = f"{base_url}{path}"
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
            f"无法连接机器人 {base_url}：{reason}", status_code=504
        ) from error
