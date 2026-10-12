"""Record the application version without resetting device configuration or data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ksq.constants import (
    APP_VERSION,
    DASHBOARD_SETTINGS_FILE,
    SOURCE_APP_DIRECTORY,
)
from ksq.runtime_logging import get_logger
from ksq.safe_io import safe_write_text


LOGGER = get_logger("state_reset")

_VERSION_MARKER_KEY = "_app_version_marker"


def get_app_version() -> str:
    """Return the current application version.

    Priority:
      1. ``KSQ_BUILD.json`` in the source application directory (the
         extracted ``.bin`` root, which always contains a ``version`` field).
      2. ``APP_VERSION`` constant (reads ``KSQ_APP_VERSION`` env var, also
         set by the ``.bin`` bootstrap from the same manifest).
      3. ``"dev"`` fallback (running from source without a build manifest).
    """
    build_file = SOURCE_APP_DIRECTORY / "KSQ_BUILD.json"
    if build_file.is_file():
        try:
            manifest = json.loads(build_file.read_text(encoding="utf-8"))
            version = str(manifest.get("version", "")).strip()
            if version:
                return version
        except (OSError, json.JSONDecodeError):
            pass
    return APP_VERSION


def _read_version_marker() -> Optional[str]:
    """Read the persisted version marker from ``dashboard_settings.json``.

    Returns ``None`` when the file is missing, invalid, or does not contain
    the marker key.
    """
    path = DASHBOARD_SETTINGS_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    marker = payload.get(_VERSION_MARKER_KEY)
    if marker is None:
        return None
    return str(marker).strip() or None


def _write_version_marker(version: str) -> None:
    """Update the version marker in ``dashboard_settings.json``.

    Reads the current file content (if any), adds or replaces the marker,
    and writes it back.  All other fields are preserved.
    """
    path = DASHBOARD_SETTINGS_FILE
    payload: dict[str, object] = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("仪表板配置格式无效，保留原文件。")
        payload = raw
    payload[_VERSION_MARKER_KEY] = version
    safe_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        backup=False,
    )


def reset_state_if_version_changed() -> bool:
    """Keep the legacy startup entry point; only the version marker is updated."""
    try:
        current_version = get_app_version()
        if _read_version_marker() == current_version:
            return False
        _write_version_marker(current_version)
        LOGGER.info("应用版本标记已更新为 %s，配置、订单状态和数据均保留。", current_version)
        return True
    except Exception:
        LOGGER.exception("无法更新应用版本标记，保留原配置并继续启动。")
        return False
