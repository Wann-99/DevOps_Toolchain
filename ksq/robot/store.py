"""Robot connection settings and per-robot POI files."""

from __future__ import annotations

from typing import Dict, List, Tuple
import json

from ksq.constants import DEFAULT_ROBOT_BASE_URL
from ksq.safe_io import safe_write_text


def load_settings(path) -> Dict[str, object]:
    settings: Dict[str, object] = {"robot_base_url": DEFAULT_ROBOT_BASE_URL}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            base_url = str(payload.get("robot_base_url") or "").strip()
            if base_url:
                settings["robot_base_url"] = base_url.rstrip("/")
    return settings


def read_poi_caches(path) -> Dict[str, List[Dict[str, object]]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
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


def save_poi_cache(path, base_url, pois):
    caches = read_poi_caches(path)
    caches[base_url] = pois
    safe_write_text(path, json.dumps({"version": 3, "endpoints": caches}, ensure_ascii=False, indent=2) + "\n")


def save_settings(path, settings):
    safe_write_text(path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
