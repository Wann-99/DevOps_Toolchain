"""Test-order list generation and pending/ordered state."""

from __future__ import annotations

import json
import random
import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from uuid import uuid4

from ksq.constants import TEST_ORDER_STATE_FILE
from ksq.test_order_select import (
    ALL_PACKAGING,
    ALL_TOOLS,
    DEFAULT_CLOSED_LOOP_RATIO,
    DEFAULT_COLUMNS,
    DEFAULT_PACKAGING_RATIO,
    DEFAULT_TOOL_RATIO,
    TOOL_CHOICES,
    display_rows_to_csv_bytes,
    item_key,
    load_candidates,
    load_packaging,
    load_small_skus,
    load_tool_mapping,
    load_unavailable,
    packaging_choices,
    parse_import_csv_full,
    public_item,
    select_items,
    summarize,
)
from ksq.web import state
from ksq.safe_io import safe_write_text

STATE_FILE = TEST_ORDER_STATE_FILE
_STATE_BACKUP_KEEP_DAYS = 2

DEFAULT_CONFIG: Dict[str, object] = {
    "count": 200,
    "seed": None,
    "closed_loop_enabled": True,
    "closed_loop_ratio": DEFAULT_CLOSED_LOOP_RATIO,
    "tool_enabled": True,
    "selected_tool": ALL_TOOLS,
    "target_tool_ratio": DEFAULT_TOOL_RATIO,
    "packaging_enabled": True,
    "selected_packaging": ALL_PACKAGING,
    "target_packaging_ratio": DEFAULT_PACKAGING_RATIO,
}


def _empty_state() -> Dict[str, object]:
    return {
        "config": deepcopy(DEFAULT_CONFIG),
        "pending": [],
        "ordered": [],
        "order_batches": [],
        "summary": summarize([]),
        "candidate_count": 0,
        "columns": deepcopy(DEFAULT_COLUMNS),
        "group_mode": "raw",
        "group_field": "",
    }


def _normalize_columns(raw: object) -> List[Dict[str, str]]:
    columns: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            label = str(entry.get("label") or "").strip() or key
            if key:
                columns.append({"key": key, "label": label})
    return columns or deepcopy(DEFAULT_COLUMNS)


def _normalize_group_mode(raw: object) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in {"raw", "group"} else "raw"


def _view_scheme(state_data: Dict[str, object]) -> Dict[str, object]:
    """列表展示方案（动态列 + 组合模式），在状态变更时整体携带。"""
    return {
        "columns": _normalize_columns(state_data.get("columns")),
        "group_mode": _normalize_group_mode(state_data.get("group_mode")),
        "group_field": str(state_data.get("group_field") or ""),
    }


def _parse_key(raw: object) -> Optional[Tuple[str, str, str]]:
    text = str(raw or "").strip()
    if not text or "|" not in text:
        return None
    parts = text.split("|")
    if len(parts) < 2:
        return None
    sku = parts[0].strip()
    location = parts[1].strip().replace("-", "")
    group = parts[2].strip() if len(parts) > 2 else ""
    if not sku or not location:
        return None
    return sku, location, group


def _parse_ratio(source: Dict[str, object], key: str, default: object) -> float:
    try:
        ratio = float(source.get(key, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} 必须是数字。") from error
    if ratio < 0 or ratio > 1:
        raise ValueError(f"{key} 必须在 0~1 之间。")
    return ratio


def _load_packaging_choices() -> List[str]:
    try:
        unavailable = load_unavailable(state.configured_unavailable)
        tools = load_tool_mapping(state.configured_tool_mapping)
        small_skus = load_small_skus(state.configured_pick_strategy)
        packaging = load_packaging(state.configured_knowledge)
        if state.configured_shelves is None:
            return packaging_choices([])
        candidates = load_candidates(
            state.configured_shelves, unavailable, tools, small_skus, packaging
        )
        return packaging_choices(candidates)
    except (OSError, ValueError, FileNotFoundError, TypeError):
        return packaging_choices([])


# 选项计算需全量扫描知识库（数千个文件），状态轮询期间缓存避免重复扫描拖垮服务
_PACKAGING_CHOICES_CACHE_SECONDS = 60.0
_packaging_choices_lock = threading.Lock()
_packaging_choices_cache_at = 0.0
_packaging_choices_cache: Optional[List[str]] = None


def _candidate_packaging_choices() -> List[str]:
    global _packaging_choices_cache_at, _packaging_choices_cache
    with _packaging_choices_lock:
        now = time.monotonic()
        if (
            _packaging_choices_cache is not None
            and now - _packaging_choices_cache_at < _PACKAGING_CHOICES_CACHE_SECONDS
        ):
            return list(_packaging_choices_cache)
        choices = _load_packaging_choices()
        _packaging_choices_cache = choices
        _packaging_choices_cache_at = now
        return list(choices)


def _normalize_config(raw: object) -> Dict[str, object]:
    source = raw if isinstance(raw, dict) else {}
    try:
        count = int(source.get("count", DEFAULT_CONFIG["count"]))
    except (TypeError, ValueError) as error:
        raise ValueError("count 必须是整数。") from error
    if count < 1:
        raise ValueError("count 必须 >= 1。")

    if source.get("seed") in (None, "", "null"):
        seed_value: Optional[int] = None
    else:
        try:
            seed_value = int(source.get("seed"))
        except (TypeError, ValueError) as error:
            raise ValueError("seed 必须是整数或留空。") from error

    # migrate old keys: small_enabled -> closed_loop_enabled
    if "closed_loop_enabled" in source:
        closed_loop_enabled = bool(source.get("closed_loop_enabled"))
    elif "small_enabled" in source:
        closed_loop_enabled = bool(source.get("small_enabled"))
    else:
        closed_loop_enabled = bool(DEFAULT_CONFIG["closed_loop_enabled"])

    if "closed_loop_ratio" in source:
        closed_loop_ratio = _parse_ratio(
            source, "closed_loop_ratio", DEFAULT_CONFIG["closed_loop_ratio"]
        )
    elif "target_small_ratio" in source:
        closed_loop_ratio = _parse_ratio(
            source, "target_small_ratio", DEFAULT_CONFIG["closed_loop_ratio"]
        )
    else:
        closed_loop_ratio = float(DEFAULT_CONFIG["closed_loop_ratio"])

    tool_enabled = bool(source.get("tool_enabled", DEFAULT_CONFIG["tool_enabled"]))
    packaging_enabled = bool(
        source.get("packaging_enabled", DEFAULT_CONFIG["packaging_enabled"])
    )

    tool_ratio = _parse_ratio(
        source, "target_tool_ratio", DEFAULT_CONFIG["target_tool_ratio"]
    )
    packaging_ratio = _parse_ratio(
        source, "target_packaging_ratio", DEFAULT_CONFIG["target_packaging_ratio"]
    )
    if not closed_loop_enabled:
        closed_loop_ratio = 0.0
    if not tool_enabled:
        tool_ratio = 0.0
    if not packaging_enabled:
        packaging_ratio = 0.0

    selected_tool = str(
        source.get("selected_tool", DEFAULT_CONFIG["selected_tool"]) or ""
    ).strip()
    if selected_tool not in TOOL_CHOICES:
        raise ValueError(f"不支持的工具类型：{selected_tool}")

    selected_packaging = str(
        source.get("selected_packaging", DEFAULT_CONFIG["selected_packaging"]) or ""
    ).strip()
    if not selected_packaging:
        selected_packaging = ALL_PACKAGING
    allowed_packaging = set(_candidate_packaging_choices())
    if selected_packaging not in allowed_packaging:
        allowed_packaging.add(selected_packaging)

    return {
        "count": count,
        "seed": seed_value,
        "closed_loop_enabled": closed_loop_enabled,
        "closed_loop_ratio": closed_loop_ratio,
        "tool_enabled": tool_enabled,
        "selected_tool": selected_tool,
        "target_tool_ratio": tool_ratio,
        "packaging_enabled": packaging_enabled,
        "selected_packaging": selected_packaging,
        "target_packaging_ratio": packaging_ratio,
    }


def _load_state_file() -> Dict[str, object]:
    if not STATE_FILE.is_file():
        return _empty_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    state_data = _empty_state()
    try:
        state_data["config"] = _normalize_config(payload.get("config"))
    except ValueError:
        state_data["config"] = deepcopy(DEFAULT_CONFIG)
    pending = payload.get("pending")
    ordered = payload.get("ordered")
    if isinstance(pending, list):
        state_data["pending"] = [item for item in pending if isinstance(item, dict)]
    if isinstance(ordered, list):
        state_data["ordered"] = [item for item in ordered if isinstance(item, dict)]
    batches = payload.get("order_batches")
    if isinstance(batches, list):
        state_data["order_batches"] = [
            batch for batch in batches if isinstance(batch, dict)
        ]
    state_data["order_batches"] = _normalize_order_batches(
        state_data["order_batches"],  # type: ignore[arg-type]
        state_data["ordered"],  # type: ignore[arg-type]
    )
    state_data["summary"] = summarize(state_data["pending"])  # type: ignore[arg-type]
    state_data["candidate_count"] = int(payload.get("candidate_count") or 0)
    state_data["columns"] = _normalize_columns(payload.get("columns"))
    state_data["group_mode"] = _normalize_group_mode(payload.get("group_mode"))
    state_data["group_field"] = str(payload.get("group_field") or "")
    return state_data


def _save_state(state_data: Dict[str, object]) -> None:
    safe_write_text(
        STATE_FILE,
        json.dumps(state_data, ensure_ascii=False, indent=2) + "\n",
        keep_days=_STATE_BACKUP_KEEP_DAYS,
    )


def _item_identity(item: Dict[str, str]) -> Tuple[str, str, str]:
    sku, location = item_key(item)
    return sku, location, str(item.get("group_id") or "").strip()


def _item_key_text(item: Dict[str, str]) -> str:
    sku, location, group = _item_identity(item)
    return f"{sku}|{location}|{group}" if group else f"{sku}|{location}"


def _normalize_order_batches(
    raw_batches: List[Dict[str, object]],
    ordered: List[Dict[str, str]],
) -> List[Dict[str, object]]:
    ordered_keys = {_item_key_text(item) for item in ordered}
    batches: List[Dict[str, object]] = []
    assigned: Set[str] = set()
    for raw in raw_batches:
        raw_keys = raw.get("item_keys")
        if not isinstance(raw_keys, list):
            continue
        keys = [
            str(key)
            for key in raw_keys
            if str(key) in ordered_keys and str(key) not in assigned
        ]
        if not keys:
            continue
        batch_id = str(raw.get("batch_id") or "").strip() or uuid4().hex
        batch = {
            "batch_id": batch_id,
            "ordered_at": str(raw.get("ordered_at") or ""),
            "order_no": str(raw.get("order_no") or ""),
            "task_id": str(raw.get("task_id") or ""),
            "item_keys": keys,
        }
        batches.append(batch)
        assigned.update(keys)

    legacy_keys = [
        _item_key_text(item)
        for item in ordered
        if _item_key_text(item) not in assigned
    ]
    if legacy_keys:
        batches.append(
            {
                "batch_id": "legacy",
                "ordered_at": "",
                "order_no": "",
                "task_id": "",
                "item_keys": legacy_keys,
            }
        )
    return batches


def _decorate_ordered_items(
    ordered: List[Dict[str, str]], batches: List[Dict[str, object]]
) -> List[Dict[str, str]]:
    metadata: Dict[str, Dict[str, str]] = {}
    for batch in batches:
        batch_meta = {
            "order_batch_id": str(batch.get("batch_id") or ""),
            "ordered_at": str(batch.get("ordered_at") or ""),
            "order_no": str(batch.get("order_no") or ""),
            "task_id": str(batch.get("task_id") or ""),
        }
        for key in batch.get("item_keys", []):  # type: ignore[union-attr]
            metadata[str(key)] = batch_meta
    decorated: List[Dict[str, str]] = []
    for item in ordered:
        row = dict(item)
        row.update(metadata.get(_item_key_text(item), {}))
        decorated.append(row)
    return decorated


def _attach_display(
    public: Dict[str, str], columns: List[Dict[str, str]]
) -> Dict[str, str]:
    """按当前列方案补齐 display：导入行用原始值，生成行回退到规范字段。"""
    raw = public.get("display")
    raw_display = raw if isinstance(raw, dict) else {}
    display: Dict[str, str] = {}
    for column in columns:
        key = column["key"]
        value = raw_display.get(key)
        if value is None:
            value = public.get(key, "")
        display[key] = "" if value is None else str(value)
    public["display"] = display
    return public


def _public_state(state_data: Dict[str, object]) -> Dict[str, object]:
    scheme = _view_scheme(state_data)
    columns = scheme["columns"]  # type: ignore[assignment]
    pending = [
        _attach_display(public_item(item), columns)  # type: ignore[arg-type]
        for item in state_data["pending"]  # type: ignore[index]
    ]
    batches = _normalize_order_batches(
        state_data.get("order_batches", []),  # type: ignore[arg-type]
        state_data["ordered"],  # type: ignore[arg-type,index]
    )
    decorated = _decorate_ordered_items(
        state_data["ordered"], batches  # type: ignore[arg-type,index]
    )
    ordered = [
        _attach_display(public_item(item), columns)  # type: ignore[arg-type]
        for item in decorated
    ]
    public_batches = [
        {
            "batch_id": str(batch.get("batch_id") or ""),
            "ordered_at": str(batch.get("ordered_at") or ""),
            "order_no": str(batch.get("order_no") or ""),
            "task_id": str(batch.get("task_id") or ""),
            "sku_count": len(batch.get("item_keys", [])),  # type: ignore[arg-type]
        }
        for batch in batches
    ]
    return {
        "config": state_data["config"],
        "pending": pending,
        "ordered": ordered,
        "summary": summarize(state_data["pending"]),  # type: ignore[arg-type]
        "ordered_count": len(ordered),
        "pending_count": len(pending),
        "order_count": len(public_batches),
        "order_batches": public_batches,
        "candidate_count": state_data.get("candidate_count", 0),
        "known_tools": list(TOOL_CHOICES),
        "known_packaging": _candidate_packaging_choices(),
        "columns": scheme["columns"],
        "group_mode": scheme["group_mode"],
        "group_field": scheme["group_field"],
    }


def get_state() -> Dict[str, object]:
    return _public_state(_load_state_file())


def generate(payload: Dict[str, object]) -> Dict[str, object]:
    config = _normalize_config(payload.get("config") if "config" in payload else payload)
    seed = config["seed"]
    if seed is not None:
        random.seed(int(seed))

    unavailable = load_unavailable(state.configured_unavailable)
    tools = load_tool_mapping(state.configured_tool_mapping)
    small_skus = load_small_skus(state.configured_pick_strategy)
    packaging = load_packaging(state.configured_knowledge)
    candidates = load_candidates(
        state.configured_shelves, unavailable, tools, small_skus, packaging
    )

    current = _load_state_file()
    ordered_items: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    ordered_keys: Set[Tuple[str, str]] = {item_key(item) for item in ordered_items}
    available = [item for item in candidates if item_key(item) not in ordered_keys]
    if not available:
        raise ValueError("没有可生成的候选（可能都在已下单测试列表中）。")

    selected = select_items(
        available,
        int(config["count"]),
        bool(config["closed_loop_enabled"]),
        float(config["closed_loop_ratio"]),
        bool(config["tool_enabled"]),
        str(config["selected_tool"]),
        float(config["target_tool_ratio"]),
        bool(config["packaging_enabled"]),
        str(config["selected_packaging"]),
        float(config["target_packaging_ratio"]),
    )
    if not selected:
        raise ValueError("按当前配置未筛出任何测试药品。")

    next_state = {
        "config": config,
        "pending": selected,
        "ordered": ordered_items,
        "order_batches": current.get("order_batches", []),
        "summary": summarize(selected),
        "candidate_count": len(candidates),
        "columns": deepcopy(DEFAULT_COLUMNS),
        "group_mode": "raw",
        "group_field": "",
    }
    _save_state(next_state)
    public = _public_state(next_state)
    public["requested_count"] = int(config["count"])
    public["shortfall"] = max(0, int(config["count"]) - len(selected))
    return public


def export_pending_csv() -> Tuple[str, bytes]:
    current = _load_state_file()
    pending: List[Dict[str, str]] = current["pending"]  # type: ignore[assignment]
    if not pending:
        raise ValueError("待下单 SKU 列表为空，请先生成。")
    columns = _normalize_columns(current.get("columns"))
    rows = [_attach_display(public_item(item), columns) for item in pending]
    return "test_order_pending.csv", display_rows_to_csv_bytes(rows, columns)


def export_ordered_csv() -> Tuple[str, bytes]:
    current = _load_state_file()
    ordered: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    if not ordered:
        raise ValueError("已下单 SKU 列表为空。")
    columns = _normalize_columns(current.get("columns"))
    batches = _normalize_order_batches(
        current.get("order_batches", []),  # type: ignore[arg-type]
        ordered,
    )
    decorated = _decorate_ordered_items(ordered, batches)
    rows = [_attach_display(public_item(item), columns) for item in decorated]
    leading = (("下单时间", "ordered_at"), ("订单号", "order_no"))
    return "test_order_ordered.csv", display_rows_to_csv_bytes(
        rows, columns, leading
    )


def import_csv(payload: Dict[str, object]) -> Dict[str, object]:
    csv_text = payload.get("csv")
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("csv 内容不能为空。")
    group_mode = _normalize_group_mode(payload.get("mode"))
    group_field = str(payload.get("group_field") or "").strip()
    if group_mode == "group" and not group_field:
        raise ValueError("组合模式需要指定组合字段。")

    unavailable = load_unavailable(state.configured_unavailable)
    tools = load_tool_mapping(state.configured_tool_mapping)
    small_skus = load_small_skus(state.configured_pick_strategy)
    packaging = load_packaging(state.configured_knowledge)
    candidates = load_candidates(
        state.configured_shelves, unavailable, tools, small_skus, packaging
    )
    imported, errors, columns = parse_import_csv_full(
        csv_text,
        candidates,
        tools,
        small_skus,
        packaging,
        group_field if group_mode == "group" else "",
    )
    if not imported:
        raise ValueError("CSV 未解析出有效药品。")

    current = _load_state_file()
    # Re-import replaces both lists; pending keeps CSV order.
    next_state = {
        "config": current["config"],
        "pending": imported,
        "ordered": [],
        "order_batches": [],
        "summary": summarize(imported),
        "candidate_count": len(candidates),
        "columns": columns,
        "group_mode": group_mode,
        "group_field": group_field if group_mode == "group" else "",
    }
    _save_state(next_state)
    public = _public_state(next_state)
    public["imported_count"] = len(imported)
    public["skipped_ordered"] = 0
    public["parse_errors"] = errors[:20]
    public["parse_error_count"] = len(errors)
    return public


def _move_keys(
    source: List[Dict[str, str]],
    target: List[Dict[str, str]],
    wanted: Set[Tuple[str, str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """Move matching items from source to end of target; preserve relative orders."""
    moved: List[Dict[str, str]] = []
    next_source: List[Dict[str, str]] = []
    for item in source:
        key = _item_identity(item)
        if key in wanted:
            moved.append(item)
        else:
            next_source.append(item)
    if not moved:
        return source, target, []
    moved_keys = {_item_identity(item) for item in moved}
    next_target = [item for item in target if _item_identity(item) not in moved_keys]
    next_target.extend(moved)
    return next_source, next_target, moved


def mark_ordered(payload: Dict[str, object]) -> Dict[str, object]:
    keys_raw = payload.get("keys")
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ValueError("keys 不能为空。")
    wanted: Set[Tuple[str, str, str]] = set()
    for raw in keys_raw:
        parsed = _parse_key(raw)
        if parsed is None:
            raise ValueError(f"无效 key：{raw}")
        wanted.add(parsed)

    current = _load_state_file()
    pending: List[Dict[str, str]] = current["pending"]  # type: ignore[assignment]
    ordered: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    next_pending, _, moved = _move_keys(pending, ordered, wanted)
    if not moved:
        raise ValueError("未在待下单 SKU 列表中找到要移动的药品。")

    moved_keys = {_item_key_text(item) for item in moved}
    remaining_ordered = [
        item for item in ordered if _item_key_text(item) not in moved_keys
    ]
    batch_id = uuid4().hex
    ordered_at = datetime.now().astimezone().isoformat(timespec="seconds")
    order_no = str(payload.get("order_no") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    moved_with_metadata: List[Dict[str, str]] = []
    for item in moved:
        row = dict(item)
        row.update(
            {
                "order_batch_id": batch_id,
                "ordered_at": ordered_at,
                "order_no": order_no,
                "task_id": task_id,
            }
        )
        moved_with_metadata.append(row)
    next_ordered = moved_with_metadata + remaining_ordered
    batches = _normalize_order_batches(
        current.get("order_batches", []),  # type: ignore[arg-type]
        ordered,
    )
    batches = [
        batch
        for batch in batches
        if any(str(key) not in moved_keys for key in batch.get("item_keys", []))  # type: ignore[union-attr]
    ]
    for batch in batches:
        batch["item_keys"] = [
            str(key)
            for key in batch.get("item_keys", [])  # type: ignore[union-attr]
            if str(key) not in moved_keys
        ]
    batches.insert(
        0,
        {
            "batch_id": batch_id,
            "ordered_at": ordered_at,
            "order_no": order_no,
            "task_id": task_id,
            "item_keys": [_item_key_text(item) for item in moved],
        },
    )

    next_state = {
        "config": current["config"],
        "pending": next_pending,
        "ordered": next_ordered,
        "order_batches": batches,
        "candidate_count": current.get("candidate_count", 0),
        **_view_scheme(current),
    }
    next_state["summary"] = summarize(next_state["pending"])  # type: ignore[arg-type]
    _save_state(next_state)
    return _public_state(next_state)


def clear_list(which: object) -> Dict[str, object]:
    target = str(which or "").strip().lower()
    if target not in {"pending", "ordered"}:
        raise ValueError("which 仅支持 pending 或 ordered。")
    current = _load_state_file()
    next_state = {
        "config": current["config"],
        "pending": [] if target == "pending" else current["pending"],
        "ordered": [] if target == "ordered" else current["ordered"],
        "order_batches": [] if target == "ordered" else current.get("order_batches", []),
        "candidate_count": current.get("candidate_count", 0),
        **_view_scheme(current),
    }
    next_state["summary"] = summarize(next_state["pending"])  # type: ignore[arg-type]
    _save_state(next_state)
    return _public_state(next_state)
