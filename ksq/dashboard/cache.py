"""Single-process dashboard snapshot cache and invalidation lock."""

from __future__ import annotations

from threading import Lock
from typing import Dict, Optional


_DASHBOARD_CACHE_LOCK = Lock()


_DASHBOARD_CACHE: Optional[Dict[str, object]] = None


_DASHBOARD_CACHE_GENERATION = 0


def _invalidate_dashboard_snapshot_cache() -> None:
    global _DASHBOARD_CACHE, _DASHBOARD_CACHE_GENERATION
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE = None
        _DASHBOARD_CACHE_GENERATION += 1
