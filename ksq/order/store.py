"""The sole in-memory active-order owner and JSON persistence boundary."""

from __future__ import annotations

from threading import Lock
from typing import Dict, Optional
import json

from ksq.constants import DASHBOARD_ACTIVE_ORDER_FILE
from ksq.runtime_logging import get_logger
from ksq.safe_io import safe_write_text


LOGGER = get_logger("dashboard")


_ACTIVE_ORDER_LOCK = Lock()


_ACTIVE_ORDER: Optional[Dict[str, object]] = None


_ACTIVE_ORDER_LOADED = False


# Last persistence error, surfaced so a failing write is not silently invisible.
_ACTIVE_ORDER_SAVE_ERROR: Optional[str] = None


def active_order_save_error() -> Optional[str]:
    return _ACTIVE_ORDER_SAVE_ERROR


def _save_active_order_unlocked() -> None:
    """Persist current active order. Caller must hold _ACTIVE_ORDER_LOCK."""
    global _ACTIVE_ORDER_SAVE_ERROR
    path = DASHBOARD_ACTIVE_ORDER_FILE
    try:
        if _ACTIVE_ORDER is None:
            if path.is_file():
                # Bind-mounted single files cannot be unlinked from inside the
                # container; truncating to an empty object clears state too.
                try:
                    path.unlink()
                except OSError:
                    safe_write_text(path, "{}\n", backup=False)
            _ACTIVE_ORDER_SAVE_ERROR = None
            return
        safe_write_text(
            path,
            json.dumps(_ACTIVE_ORDER, ensure_ascii=False, indent=2) + "\n",
            backup=False,
        )
        _ACTIVE_ORDER_SAVE_ERROR = None
    except OSError as error:
        # In-memory state stays authoritative, but record why disk is stale.
        _ACTIVE_ORDER_SAVE_ERROR = str(error)
        LOGGER.error("活动订单持久化失败：%s", error, exc_info=True)
        return


def _ensure_active_order_loaded() -> None:
    """Load active order from disk once per process if memory is empty."""
    global _ACTIVE_ORDER, _ACTIVE_ORDER_LOADED
    if _ACTIVE_ORDER_LOADED:
        return
    _ACTIVE_ORDER_LOADED = True
    if _ACTIVE_ORDER is not None:
        return
    path = DASHBOARD_ACTIVE_ORDER_FILE
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    current = payload if (
        payload.get("task_id") or payload.get("items") or payload.get("lifecycle")
    ) else None
    # Loading display state must never schedule or promote a queued order.
    if current is not None:
        _ACTIVE_ORDER = current
