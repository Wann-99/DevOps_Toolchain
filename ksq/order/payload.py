"""Build Order Broker create-task request bodies."""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Mapping
from uuid import uuid4

from ksq.order.config import customer_order_profile, source_prefix

_ORDER_NO_LOCK = Lock()
_LAST_ORDER_NO_TIME: datetime | None = None
_ORDER_FIELDS = frozenset(
    {
        "store_name", "store_phone", "store_address", "recipient_name",
        "recipient_phone", "delivery_address", "privacy_phone",
        "backup_privacy_phones", "phone_last4_digits", "invoice_code",
        "order_time_timezone", "buyer_note", "rider_pickup_number",
        "retail_order_id", "total_price", "total_quantity", "goods_amount",
        "pay_amount", "discount_amount", "delivery_fee", "packaging_fee",
    }
)
_ITEM_SOURCES = {
    "common_name": ("common_name", "name", "item_name"),
    "specifications": ("specifications", "spec", "specification"),
    "manufacturer": ("manufacturer",),
    "approval_number": ("approval_number",),
    "batch_number": ("batch_number",),
    "expiry_date": ("expiry_date",),
    "retail_price": ("retail_price",),
    "item_amount": ("item_amount",),
    "discount_amount": ("discount_amount",),
    "dosage": ("dosage",),
    "production_place": ("production_place",),
}


class OrderPayloadError(ValueError):
    """Local request validation with all missing item fields collected."""

    def __init__(self, message: str, missing_fields: List[Dict[str, object]] | None = None):
        super().__init__(message)
        self.missing_fields = list(missing_fields or [])


def generate_order_no() -> str:
    global _LAST_ORDER_NO_TIME
    # ponytail: single-process guard; use a shared allocator if multiple instances create orders.
    with _ORDER_NO_LOCK:
        now = datetime.now().replace(microsecond=0)
        if _LAST_ORDER_NO_TIME is not None and now <= _LAST_ORDER_NO_TIME:
            now = _LAST_ORDER_NO_TIME + timedelta(seconds=1)
        _LAST_ORDER_NO_TIME = now
        return now.strftime("%Y%m%d%H%M%S")


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


def _first_text(raw: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_order_items(
    raw_items: object, profile: Dict[str, object] | None = None
) -> List[Dict[str, object]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items 不能为空。")
    items: List[Dict[str, object]] = []
    missing: List[Dict[str, object]] = []
    require_location = bool((profile or {}).get("require_location", True))
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}] 必须是对象。")
        item_id = str(raw.get("item_id") or "").strip()
        customer_location = raw.get("customer_location_code")
        if customer_location is not None and not isinstance(customer_location, str):
            raise ValueError(f"items[{index}].customer_location_code 必须是字符串。")
        location_code = (customer_location or "").strip() or str(
            raw.get("location_code") or ""
        ).strip().replace("-", "")
        if not item_id or (require_location and not location_code):
            fields = []
            if not item_id:
                fields.append("item_id")
            if require_location and not location_code:
                fields.append("location_code")
            missing.append({"item_index": index, "item_id": item_id, "fields": fields})
            continue
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
        for target, source_keys in _ITEM_SOURCES.items():
            value = _first_text(raw, source_keys)
            if value:
                item[target] = value
        barcode = str(raw.get("barcode") or raw.get("sku_code") or "").strip()
        if barcode:
            item["barcode"] = barcode
        for key in ("sku_id", "group_id", "group_field"):
            value = str(raw.get(key) or "").strip()
            if value:
                item[key] = value
        items.append(item)
    if missing:
        labels = "; ".join(
            f"第{entry['item_index'] + 1}项 "
            + "、".join(f"{field} 不能为空" for field in entry["fields"])
            for entry in missing
        )
        raise OrderPayloadError(f"订单字段不完整：{labels}。", missing)
    return items


def _map_item(item: Dict[str, object], profile: Dict[str, object]) -> Dict[str, object]:
    fields = profile.get("item_fields") or ()
    mapping = profile.get("item_map") or {}
    result: Dict[str, object] = {}
    for field in fields:
        source = mapping.get(field, field)
        value = item.get(source)
        if field == "quantity":
            result[field] = item["quantity"]
        elif field in {"item_id", "location_code"}:
            if field in item:
                result[field] = item[field]
        elif value not in (None, ""):
            result[field] = value
    return result


def build_create_task_body(
    config: Dict[str, object], raw_items: object, order_fields: object = None
) -> Dict[str, object]:
    profile = customer_order_profile(config.get("customer"))
    normalized_items = normalize_order_items(raw_items, profile)
    allowed_sources = tuple(str(value) for value in profile.get("sources") or ())
    order_source = str(config.get("order_source") or "").strip().lower()
    if not order_source:
        order_source = allowed_sources[0] if allowed_sources else "meituan"
    if order_source not in allowed_sources:
        raise ValueError(
            f"客户 {config.get('customer') or '默认'} 不支持订单来源：{order_source}。"
        )
    store_id = str(config.get("store_id") or "").strip()
    if not store_id:
        raise ValueError("store_id 未配置。")
    if order_fields is not None and not isinstance(order_fields, dict):
        raise ValueError("order_fields 必须是对象。")
    extras = {
        key: value
        for key, value in (order_fields or {}).items()
        if key in _ORDER_FIELDS and value not in (None, "")
    }
    total_quantity = sum(int(item["quantity"]) for item in normalized_items)
    order_no = generate_order_no()
    body: Dict[str, object] = {
        "store_id": store_id,
        "order_source": order_source,
        "order_no": order_no,
        "order_time": now_order_time(),
        "platform_order_no": generate_platform_order_no(order_source),
        "items": [_map_item(item, profile) for item in normalized_items],
        "total_quantity": str(total_quantity),
        "buyer_note": "",
        "order_time_timezone": str(
            config.get("order_time_timezone") or "Asia/Shanghai"
        ),
        "rider_pickup_number": str(uuid4().int % 1000 + 1),
    }
    if profile.get("include_order_amounts"):
        body.update({
            "goods_amount": "0.00",
            "pay_amount": "0.00",
            "total_price": "0.00",
            "discount_amount": "0.00",
        })
    store_name = str(config.get("store_name") or "").strip()
    if store_name:
        body["store_name"] = store_name
    body.update(extras)
    if profile.get("need_retail_order_id") and not body.get("retail_order_id"):
        body["retail_order_id"] = "RO" + order_no
    if bool(config.get("need_image_upload")):
        body["need_image_upload"] = True
    return body
