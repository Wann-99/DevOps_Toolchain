"""Build Order Broker create-task request body."""

from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from ksq.order.config import source_prefix

_ORDER_NO_SUFFIX_CHARS = string.ascii_uppercase + string.digits


def generate_order_no() -> str:
    now = datetime.now()
    stamp = (
        f"{now.year:04d}{now.month:02d}{now.day:02d}"
        f"{now.hour:02d}{now.minute:02d}{now.second:02d}{now.microsecond // 1000:03d}"
    )
    suffix = "".join(secrets.choice(_ORDER_NO_SUFFIX_CHARS) for _ in range(2))
    return f"TEST{stamp}{suffix}"


def generate_platform_order_no(order_source: str) -> str:
    now = datetime.now()
    prefix = source_prefix(order_source)
    stamp = (
        f"{now.year:04d}{now.month:02d}{now.day:02d}"
        f"{now.hour:02d}{now.minute:02d}"
    )
    rnd = f"{uuid4().int % 10000:04d}"
    return f"{prefix}{stamp}{rnd}"


def now_order_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_order_items(raw_items: object) -> List[Dict[str, object]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 不能为空。")
    items: List[Dict[str, object]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}] 必须是对象。")
        item_id = str(raw.get("item_id") or "").strip()
        location_code = str(raw.get("location_code") or "").strip().replace("-", "")
        if not item_id:
            raise ValueError(f"items[{index}].item_id 不能为空。")
        if not location_code:
            raise ValueError(f"items[{index}].location_code 不能为空。")
        quantity_raw = raw.get("quantity", 1)
        try:
            quantity = int(quantity_raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"items[{index}].quantity 无效。") from error
        if quantity < 1:
            raise ValueError(f"items[{index}].quantity 必须 >= 1。")
        item: Dict[str, object] = {
            "item_id": item_id,
            "location_code": location_code,
            "quantity": quantity,
        }
        barcode = str(raw.get("barcode") or "").strip()
        if barcode:
            item["barcode"] = barcode
        name = str(raw.get("name") or raw.get("common_name") or "").strip()
        if name:
            item["common_name"] = name
        items.append(item)
    return items


def build_create_task_body(
    config: Dict[str, object], raw_items: object
) -> Dict[str, object]:
    items = normalize_order_items(raw_items)
    order_source = str(config.get("order_source") or "meituan").strip()
    store_id = str(config.get("store_id") or "").strip()
    if not store_id:
        raise ValueError("store_id 未配置。")

    total_quantity = sum(int(item["quantity"]) for item in items)
    body: Dict[str, object] = {
        "store_id": store_id,
        "order_source": order_source,
        "order_no": generate_order_no(),
        "order_time": now_order_time(),
        "platform_order_no": generate_platform_order_no(order_source),
        "items": items,
        "total_quantity": str(total_quantity),
        "goods_amount": "0.00",
        "pay_amount": "0.00",
        "total_price": "0.00",
        "discount_amount": "0.00",
        "buyer_note": "",
        "order_time_timezone": str(
            config.get("order_time_timezone") or "Asia/Shanghai"
        ),
        "rider_pickup_number": str(uuid4().int % 1000 + 1),
    }

    store_name = str(config.get("store_name") or "").strip()
    if store_name:
        body["store_name"] = store_name

    if bool(config.get("need_image_upload")):
        body["need_image_upload"] = True

    return body
