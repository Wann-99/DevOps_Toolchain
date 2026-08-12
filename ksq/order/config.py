"""Persist and validate Order Broker store configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Dict

from ksq.constants import ORDER_CONFIG_FILE
from ksq.safe_io import safe_write_text

_CONFIG_BACKUP_KEEP_DAYS = 2

ORDER_SOURCES = [
    {"value": "meituan", "prefix": "MT", "cn": "美团外卖"},
    {"value": "eleme", "prefix": "ELEM", "cn": "淘宝闪购"},
    {"value": "jd", "prefix": "JD", "cn": "京东"},
    {"value": "dy", "prefix": "DY", "cn": "抖音"},
    {"value": "dsl", "prefix": "DSL", "cn": "大参林健康"},
]

CUSTOMER_DEFAULTS: Dict[str, Dict[str, object]] = {
    "dashenlin": {
        "order_source": "meituan",
        "need_image_upload": False,
        "business_mode_code": "MODE_PICK_WAIT_PACK",
    },
    "yaoshibang": {
        "order_source": "meituan",
        "need_image_upload": True,
        "business_mode_code": "MODE_PICK",
    },
}

# Only these fields are filled by the operator; the rest are auto-generated.
MANUAL_CONFIG_KEYS = (
    "server",
    "customer",
    "client_id",
    "client_secret",
    "store_id",
)

DEFAULT_ORDER_CONFIG: Dict[str, object] = {
    "server": "http://localhost:8000",
    "client_id": "",
    "client_secret": "",
    "customer": "dashenlin",
    "store_id": "",
    "store_name": "",
    "order_source": "meituan",
    "order_time_timezone": "Asia/Shanghai",
    "need_image_upload": False,
    "business_mode_code": "MODE_PICK_WAIT_PACK",
}


def default_order_config() -> Dict[str, object]:
    return deepcopy(DEFAULT_ORDER_CONFIG)


def apply_customer_defaults(config: Dict[str, object]) -> Dict[str, object]:
    customer = str(config.get("customer") or "dashenlin").strip() or "dashenlin"
    defaults = CUSTOMER_DEFAULTS.get(customer, CUSTOMER_DEFAULTS["dashenlin"])
    merged = deepcopy(config)
    merged["customer"] = customer
    for key, value in defaults.items():
        merged[key] = value
    return merged


def load_order_config(config_file: Path) -> Dict[str, object]:
    config = default_order_config()
    if config_file.is_file():
        with config_file.open(encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise ValueError(f"下单配置根节点必须是对象：{config_file}")
        for key, value in payload.items():
            if key in config:
                config[key] = value
    return apply_customer_defaults(config)


def save_order_config(config_file: Path, payload: Dict[str, object]) -> Dict[str, object]:
    current = load_order_config(config_file)
    merged = merge_config_update(current, payload)
    merged = apply_customer_defaults(merged)
    safe_write_text(
        config_file,
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        keep_days=_CONFIG_BACKUP_KEEP_DAYS,
    )
    return merged


def validate_order_config(config: Dict[str, object]) -> None:
    required_strings = ("server", "client_id", "client_secret", "store_id")
    for key in required_strings:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"下单配置缺少有效字段：{key}")
    server = str(config["server"]).strip()
    if not (server.startswith("http://") or server.startswith("https://")):
        raise ValueError("server 必须以 http:// 或 https:// 开头。")


def public_order_config(config: Dict[str, object]) -> Dict[str, object]:
    public = {
        "server": config.get("server", ""),
        "customer": config.get("customer", "dashenlin"),
        "client_id": config.get("client_id", ""),
        "client_secret": "",
        "store_id": config.get("store_id", ""),
        "store_name": config.get("store_name", ""),
        "has_client_secret": bool(str(config.get("client_secret") or "").strip()),
        "order_source": config.get("order_source", "meituan"),
        "need_image_upload": bool(config.get("need_image_upload")),
        "business_mode_code": config.get("business_mode_code", ""),
        "customers": [
            {"value": "dashenlin", "cn": "大参林"},
            {"value": "yaoshibang", "cn": "药师帮"},
        ],
    }
    return public


def merge_config_update(
    current: Dict[str, object], update: Dict[str, object]
) -> Dict[str, object]:
    merged = deepcopy(current)
    for key in MANUAL_CONFIG_KEYS:
        if key not in update:
            continue
        if key == "client_secret":
            secret = update.get("client_secret")
            if secret is None:
                continue
            if not isinstance(secret, str):
                raise ValueError("client_secret 必须是字符串。")
            if secret.strip() == "" and str(current.get("client_secret") or "").strip():
                continue
            merged[key] = secret
            continue
        merged[key] = update[key]
    if "store_name" in update and isinstance(update.get("store_name"), str):
        merged["store_name"] = update["store_name"]
    return merged


def load_default_order_config() -> Dict[str, object]:
    return load_order_config(ORDER_CONFIG_FILE)


def source_prefix(order_source: str) -> str:
    for item in ORDER_SOURCES:
        if item["value"] == order_source:
            return item["prefix"]
    return order_source.upper()
