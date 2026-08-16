"""Startup state reset based on application version marker.

When the ``.bin`` application package is updated on a device, the four
runtime state files (``test_order_state.json``, ``dashboard_active_order.json``,
``order_config.json``, ``order_config.prod.json``) may still carry stale data
from the previous version.  Because the ``.bin`` only contains Python code
(not ``start.sh``), the shell-level ``reset_state`` in the device's old
``start.sh`` may not run.  This module provides a pure-Python reset that
travels inside the ``.bin`` and therefore always takes effect.

The mechanism stores an internal version marker (``_app_version_marker``)
inside ``dashboard_settings.json`` — the only state file that is both
bind-mounted (persistent) and intentionally excluded from the reset.
On startup the current application version is compared against the marker;
a mismatch or missing marker triggers a reset of the four files, after
which the marker is updated.

``dashboard_settings.json`` itself is never reset — only the marker field
inside it is added/updated.  ``save_dashboard_settings`` in
``dashboard_api.py`` is patched to preserve the marker across user-initiated
settings saves.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from ksq.constants import (
    APP_VERSION,
    DASHBOARD_ACTIVE_ORDER_FILE,
    DASHBOARD_SETTINGS_FILE,
    ORDER_CONFIG_FILE,
    ORDER_CONFIG_PROD_FILE,
    SOURCE_APP_DIRECTORY,
    TEST_ORDER_STATE_FILE,
)
from ksq.order.config import DEFAULT_ORDER_CONFIG
from ksq.safe_io import safe_write_text

_VERSION_MARKER_KEY = "_app_version_marker"
_BACKUP_DIR_NAME = ".backup"
_BACKUP_STAMP_FORMAT = "%Y%m%d_%H%M%S"

# Files that get reset to clean defaults on version change.
# dashboard_settings.json is NOT in this list — it is preserved.
_RESET_FILES: tuple[Path, ...] = (
    TEST_ORDER_STATE_FILE,
    DASHBOARD_ACTIVE_ORDER_FILE,
    ORDER_CONFIG_FILE,
    ORDER_CONFIG_PROD_FILE,
)


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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload = raw
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload[_VERSION_MARKER_KEY] = version
    safe_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        backup=False,
    )


def _backup_reset_files(timestamp: str) -> Path:
    """Copy the four reset-target files into ``.backup/<timestamp>/``.

    Returns the backup directory path.  Missing files are silently skipped.
    """
    backup_dir = DASHBOARD_SETTINGS_FILE.parent / _BACKUP_DIR_NAME / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for file_path in _RESET_FILES:
        if file_path.is_file():
            try:
                shutil.copy2(file_path, backup_dir / file_path.name)
            except OSError:
                pass
    return backup_dir


def _write_reset_values() -> None:
    """Overwrite the four state files with clean defaults.

    * ``test_order_state.json``        → ``{}``
    * ``dashboard_active_order.json``  → ``{}``
    * ``order_config.json``            → ``DEFAULT_ORDER_CONFIG``
    * ``order_config.prod.json``       → ``DEFAULT_ORDER_CONFIG``
    """
    empty_json = "{}\n"
    default_config_json = (
        json.dumps(DEFAULT_ORDER_CONFIG, ensure_ascii=False, indent=2) + "\n"
    )
    safe_write_text(TEST_ORDER_STATE_FILE, empty_json, backup=False)
    safe_write_text(DASHBOARD_ACTIVE_ORDER_FILE, empty_json, backup=False)
    safe_write_text(ORDER_CONFIG_FILE, default_config_json, backup=False)
    safe_write_text(ORDER_CONFIG_PROD_FILE, default_config_json, backup=False)


def reset_state_if_version_changed() -> bool:
    """Reset state files if the application version has changed.

    Returns ``True`` if a reset was performed, ``False`` if skipped (version
    unchanged — plain container restart).  Any exception is caught and logged;
    the function never raises so it cannot block application startup.
    """
    try:
        current_version = get_app_version()
        marker = _read_version_marker()

        if marker is not None and marker == current_version:
            # Version unchanged — this is a container restart, not an upgrade.
            return False

        # Version changed or new deployment (no marker) → reset.
        timestamp = datetime.now().strftime(_BACKUP_STAMP_FORMAT)
        backup_dir = _backup_reset_files(timestamp)
        _write_reset_values()

        # Update dashboard_settings marker (preserving all other fields).
        _write_version_marker(current_version)

        if marker is None:
            reason = f"新部署（无版本标记），当前版本 {current_version}"
        else:
            reason = f"版本变更（{marker} → {current_version}）"
        print(f"[state_reset] 状态文件已重置：{reason}")
        print(f"[state_reset] 备份位于 {backup_dir}")
        print("[state_reset] dashboard_settings.json 已保留（仅更新版本标记）")
        return True
    except Exception as exc:  # noqa: BLE001 — must not block startup
        print(f"[state_reset] 重置失败（不阻断启动）：{exc}")
        return False
