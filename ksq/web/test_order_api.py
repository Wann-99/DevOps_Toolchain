"""Test-order list generation and pending/ordered state."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from typing import Dict, List, Optional, Set, Tuple

from ksq.constants import TEST_ORDER_STATE_FILE
from ksq.test_order_select import (
    ALL_PACKAGING,
    ALL_TOOLS,
    DEFAULT_CLOSED_LOOP_RATIO,
    DEFAULT_PACKAGING_RATIO,
    DEFAULT_TOOL_RATIO,
    TOOL_CHOICES,
    item_key,
    load_candidates,
    load_packaging,
    load_small_skus,
    load_tool_mapping,
    load_unavailable,
    packaging_choices,
    parse_import_csv,
    public_item,
    rows_to_csv_bytes,
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
        "summary": summarize([]),
        "candidate_count": 0,
    }


def _parse_key(raw: object) -> Optional[Tuple[str, str]]:
    text = str(raw or "").strip()
    if not text or "|" not in text:
        return None
    sku, location = text.split("|", 1)
    sku = sku.strip()
    location = location.strip().replace("-", "")
    if not sku or not location:
        return None
    return sku, location


def _parse_ratio(source: Dict[str, object], key: str, default: object) -> float:
    try:
        ratio = float(source.get(key, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} 必须是数字。") from error
    if ratio < 0 or ratio > 1:
        raise ValueError(f"{key} 必须在 0~1 之间。")
    return ratio


def _candidate_packaging_choices() -> List[str]:
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
    state_data["summary"] = summarize(state_data["pending"])  # type: ignore[arg-type]
    state_data["candidate_count"] = int(payload.get("candidate_count") or 0)
    return state_data


def _save_state(state_data: Dict[str, object]) -> None:
    safe_write_text(
        STATE_FILE,
        json.dumps(state_data, ensure_ascii=False, indent=2) + "\n",
        keep_days=_STATE_BACKUP_KEEP_DAYS,
    )


def _public_state(state_data: Dict[str, object]) -> Dict[str, object]:
    pending = [public_item(item) for item in state_data["pending"]]  # type: ignore[index]
    ordered = [public_item(item) for item in state_data["ordered"]]  # type: ignore[index]
    return {
        "config": state_data["config"],
        "pending": pending,
        "ordered": ordered,
        "summary": summarize(state_data["pending"]),  # type: ignore[arg-type]
        "ordered_count": len(ordered),
        "pending_count": len(pending),
        "candidate_count": state_data.get("candidate_count", 0),
        "known_tools": list(TOOL_CHOICES),
        "known_packaging": _candidate_packaging_choices(),
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
        "summary": summarize(selected),
        "candidate_count": len(candidates),
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
        raise ValueError("未下单列表为空，请先生成。")
    return "test_order_pending.csv", rows_to_csv_bytes(pending)


def export_ordered_csv() -> Tuple[str, bytes]:
    current = _load_state_file()
    ordered: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    if not ordered:
        raise ValueError("已下单列表为空。")
    return "test_order_ordered.csv", rows_to_csv_bytes(ordered)


def import_csv(payload: Dict[str, object]) -> Dict[str, object]:
    csv_text = payload.get("csv")
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("csv 内容不能为空。")

    unavailable = load_unavailable(state.configured_unavailable)
    tools = load_tool_mapping(state.configured_tool_mapping)
    small_skus = load_small_skus(state.configured_pick_strategy)
    packaging = load_packaging(state.configured_knowledge)
    candidates = load_candidates(
        state.configured_shelves, unavailable, tools, small_skus, packaging
    )
    imported, errors = parse_import_csv(
        csv_text, candidates, tools, small_skus, packaging
    )
    if not imported:
        raise ValueError("CSV 未解析出有效药品。")

    current = _load_state_file()
    # Re-import replaces both lists; pending keeps CSV order.
    next_state = {
        "config": current["config"],
        "pending": imported,
        "ordered": [],
        "summary": summarize(imported),
        "candidate_count": len(candidates),
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
    wanted: Set[Tuple[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """Move matching items from source to end of target; preserve relative orders."""
    moved: List[Dict[str, str]] = []
    next_source: List[Dict[str, str]] = []
    for item in source:
        key = item_key(item)
        if key in wanted:
            moved.append(item)
        else:
            next_source.append(item)
    if not moved:
        return source, target, []
    moved_keys = {item_key(item) for item in moved}
    next_target = [item for item in target if item_key(item) not in moved_keys]
    next_target.extend(moved)
    return next_source, next_target, moved


def mark_ordered(payload: Dict[str, object]) -> Dict[str, object]:
    keys_raw = payload.get("keys")
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ValueError("keys 不能为空。")
    wanted: Set[Tuple[str, str]] = set()
    for raw in keys_raw:
        parsed = _parse_key(raw)
        if parsed is None:
            raise ValueError(f"无效 key：{raw}")
        wanted.add(parsed)

    current = _load_state_file()
    pending: List[Dict[str, str]] = current["pending"]  # type: ignore[assignment]
    ordered: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    next_pending, next_ordered, moved = _move_keys(pending, ordered, wanted)
    if not moved:
        raise ValueError("未在未下单列表中找到要移动的药品。")

    next_state = {
        "config": current["config"],
        "pending": next_pending,
        "ordered": next_ordered,
        "candidate_count": current.get("candidate_count", 0),
    }
    next_state["summary"] = summarize(next_state["pending"])  # type: ignore[arg-type]
    _save_state(next_state)
    return _public_state(next_state)


def restore(payload: Dict[str, object]) -> Dict[str, object]:
    keys_raw = payload.get("keys")
    if not isinstance(keys_raw, list) or not keys_raw:
        raise ValueError("keys 不能为空。")
    wanted: Set[Tuple[str, str]] = set()
    for raw in keys_raw:
        parsed = _parse_key(raw)
        if parsed is None:
            raise ValueError(f"无效 key：{raw}")
        wanted.add(parsed)

    current = _load_state_file()
    pending: List[Dict[str, str]] = current["pending"]  # type: ignore[assignment]
    ordered: List[Dict[str, str]] = current["ordered"]  # type: ignore[assignment]
    next_ordered, next_pending, moved = _move_keys(ordered, pending, wanted)
    if not moved:
        raise ValueError("未在已下单列表中找到要恢复的药品。")

    next_state = {
        "config": current["config"],
        "pending": next_pending,
        "ordered": next_ordered,
        "candidate_count": current.get("candidate_count", 0),
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
        "candidate_count": current.get("candidate_count", 0),
    }
    next_state["summary"] = summarize(next_state["pending"])  # type: ignore[arg-type]
    _save_state(next_state)
    return _public_state(next_state)
