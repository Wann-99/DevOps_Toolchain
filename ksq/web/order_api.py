"""Order Broker HTTP API helpers used by the web handler."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from ksq.constants import ORDER_CONFIG_FILE, ORDER_CONFIG_PROD_FILE
from ksq.order import broker
from ksq.order.config import (
    load_order_config,
    merge_config_update,
    public_order_config,
    save_order_config,
    validate_order_config,
)
from ksq.order.payload import build_create_task_body
from ksq.web import state


def config_file_for_mode(mode: object) -> Path:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return ORDER_CONFIG_PROD_FILE
    if value not in {"", "test"}:
        raise ValueError("mode 仅支持 test 或 prod。")
    return ORDER_CONFIG_FILE


def get_public_config(mode: object = "test") -> Dict[str, object]:
    config_file = config_file_for_mode(mode)
    config = load_order_config(config_file)
    payload = public_order_config(config)
    payload["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    payload["config_file"] = str(config_file)
    return payload


def _token_cache_key(config: Dict[str, object]) -> str:
    return (
        str(config.get("server") or "").strip()
        + "|"
        + str(config.get("client_id") or "").strip()
    )


def _cached_token(config: Dict[str, object]) -> Optional[str]:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if isinstance(tokens, dict):
            cached = tokens.get(key)
            if isinstance(cached, str) and cached:
                return cached
        # Backward compatible single-slot cache for the default config path.
        if state.order_access_token and key == getattr(
            state, "order_access_token_key", ""
        ):
            return state.order_access_token
    return None


def _store_token(config: Dict[str, object], token: str) -> None:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if not isinstance(tokens, dict):
            tokens = {}
            state.order_access_tokens = tokens
        tokens[key] = token
        state.order_access_token = token
        state.order_access_token_key = key


def _clear_token(config: Dict[str, object]) -> None:
    key = _token_cache_key(config)
    with state.DATASET_LOCK:
        tokens = getattr(state, "order_access_tokens", None)
        if isinstance(tokens, dict):
            tokens.pop(key, None)
        if getattr(state, "order_access_token_key", "") == key:
            state.order_access_token = None
            state.order_access_token_key = ""


def update_config(
    payload: Dict[str, object], mode: object = "test"
) -> Dict[str, object]:
    config_file = config_file_for_mode(mode)
    current = load_order_config(config_file)
    merged = merge_config_update(current, payload)
    saved = save_order_config(config_file, merged)
    _clear_token(saved)
    result = public_order_config(saved)
    result["mode"] = "prod" if config_file == ORDER_CONFIG_PROD_FILE else "test"
    result["config_file"] = str(config_file)
    return result


def _token_auth_mode(mode: object) -> str:
    value = str(mode or "test").strip().lower()
    if value == "prod":
        return "user_login"
    return "client"


def _ensure_token(config: Dict[str, object], mode: object = "test") -> str:
    validate_order_config(config)
    cached = _cached_token(config)
    if cached:
        return cached
    token = broker.fetch_access_token(
        str(config["server"]),
        str(config["client_id"]),
        str(config["client_secret"]),
        _token_auth_mode(mode),
    )
    _store_token(config, token)
    return token


def refresh_token(mode: object = "test") -> Dict[str, object]:
    config = load_order_config(config_file_for_mode(mode))
    _clear_token(config)
    token = _ensure_token(config, mode)
    return {"ok": True, "token_preview": token[:12] + "..."}


def _mode_for_config_file(config_file: Path) -> str:
    return "prod" if Path(config_file).resolve() == ORDER_CONFIG_PROD_FILE.resolve() else "test"


def list_stores(mode: object = "test") -> Dict[str, object]:
    config = load_order_config(config_file_for_mode(mode))
    token = _ensure_token(config, mode)
    try:
        status, data = broker.list_my_stores(str(config["server"]), token)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            status, data = broker.list_my_stores(str(config["server"]), token)
        else:
            raise
    payload = data.get("data") if isinstance(data, dict) else data
    stores = payload if isinstance(payload, list) else []
    return {"status": status, "stores": stores}


def create_order(payload: Dict[str, object]) -> Tuple[int, object, Dict[str, object]]:
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode(payload.get("mode"))
    blocking = set(dashboard_api.active_order_blocking_keys())
    raw_items = payload.get("items")
    if blocking and isinstance(raw_items, list):
        conflicts = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            for key_name in ("item_id", "barcode", "code", "sku_code"):
                value = str(raw.get(key_name) or "").strip()
                if value and value in blocking:
                    conflicts.append(value)
                    break
        if conflicts:
            unique = ", ".join(dict.fromkeys(conflicts))
            raise ValueError(
                f"当前工单仍在处理中，请勿重复下单：{unique}"
            )
    config = load_order_config(config_file_for_mode(mode))
    body = build_create_task_body(config, payload.get("items"))
    token = _ensure_token(config, mode)
    try:
        status, data = broker.create_robot_task(str(config["server"]), token, body)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            status, data = broker.create_robot_task(str(config["server"]), token, body)
        else:
            raise
    return status, data, body


def get_task_detail(
    task_id: str, config_file: Optional[Path] = None
) -> Tuple[int, object]:
    from ksq.web import dashboard_api

    if config_file is not None:
        path = config_file
        mode = _mode_for_config_file(path)
    else:
        mode = dashboard_api.resolve_dashboard_mode("")
        path = config_file_for_mode(mode)
    config = load_order_config(path)
    token = _ensure_token(config, mode)
    try:
        return broker.get_robot_task(str(config["server"]), token, task_id)
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            return broker.get_robot_task(str(config["server"]), token, task_id)
        raise


def cancel_task(
    task_id: str, cancel_type: str, cancel_reason: str
) -> Tuple[int, object]:
    from ksq.web import dashboard_api

    mode = dashboard_api.resolve_dashboard_mode("")
    config = load_order_config(config_file_for_mode(mode))
    token = _ensure_token(config, mode)
    try:
        return broker.cancel_robot_task(
            str(config["server"]), token, task_id, cancel_type, cancel_reason
        )
    except broker.OrderBrokerError as error:
        if error.status_code in {401, 403}:
            _clear_token(config)
            token = _ensure_token(config, mode)
            return broker.cancel_robot_task(
                str(config["server"]), token, task_id, cancel_type, cancel_reason
            )
        raise


def extract_task_id(response_body: object) -> Optional[str]:
    if not isinstance(response_body, dict):
        return None
    data = response_body.get("data")
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            return task_id.strip()
    task_id = response_body.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        return task_id.strip()
    return None
