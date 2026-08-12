"""Generate test-order SKU lists."""

from __future__ import annotations

import csv
import io
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_TOOL = "double_vacuum_gripper"
SPECIAL_TOOLS = frozenset({"four_vacuum_gripper", "gripper"})
KNOWN_TOOLS = (
    "double_vacuum_gripper",
    "four_vacuum_gripper",
    "gripper",
)
ALL_TOOLS = "全部"
TOOL_CHOICES = (ALL_TOOLS,) + KNOWN_TOOLS
ALL_PACKAGING = "全部"
KNOWN_PACKAGING = (
    "纸盒等硬质包装",
    "塑料等柔性袋装(易变形)",
    "塑料等柔性袋装(不易变形)",
    "瓶装",
    "塑料管",
)
CODE_PUSHER = "code_pusher"
DEFAULT_CLOSED_LOOP_RATIO = 0.3
DEFAULT_TOOL_RATIO = 0.3
DEFAULT_PACKAGING_RATIO = 0.2

CSV_EXPORT_FIELDS = (
    "out_item_id",
    "location_code",
    "sku_code",
    "name",
    "推荐工具",
    "包装类型",
    "货架属性",
    "是否闭环抓取",
)


def load_unavailable(path: Optional[Path]) -> Set[str]:
    if path is None or not path.is_file():
        return set()
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return {
        str(sku).strip()
        for sku in data.get("unavailable_obj", [])
        if str(sku).strip()
    }


def load_tool_mapping(path: Optional[Path]) -> Dict[str, str]:
    if path is None or not path.is_file():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in data.items()
        if str(key).strip()
    }


def load_small_skus(path: Optional[Path]) -> Set[str]:
    if path is None or not path.is_file():
        return set()
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return {
        str(sku).strip()
        for sku in data.get("closed_loop", [])
        if str(sku).strip()
    }


def load_packaging(knowledge_dir: Path) -> Dict[str, str]:
    packaging: Dict[str, str] = {}
    if not knowledge_dir.is_dir():
        return packaging
    for path in knowledge_dir.glob("*.json"):
        try:
            with path.open(encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        pkg = str(data.get("包装类型") or "").strip()
        sku = path.stem.strip()
        if sku:
            packaging[sku] = pkg
        code = str(data.get("sku_code") or data.get("商品条码") or "").strip()
        if code:
            packaging[code] = pkg
    return packaging


def packaging_choices(candidates: Iterable[Dict[str, str]]) -> List[str]:
    values = {ALL_PACKAGING}
    for item in KNOWN_PACKAGING:
        values.add(item)
    for item in candidates:
        pkg = str(item.get("包装类型") or "").strip()
        if pkg:
            values.add(pkg)
    ordered = [ALL_PACKAGING]
    for item in KNOWN_PACKAGING:
        if item in values and item not in ordered:
            ordered.append(item)
    rest = sorted(
        (item for item in values if item not in ordered),
        key=lambda text: text,
    )
    return ordered + rest


def location_code(row: Dict[str, str]) -> str:
    return (
        f"{(row.get('shelf_number') or '').strip()}"
        f"{(row.get('level') or '').strip()}"
        f"{(row.get('bin_unit') or '').strip()}"
    )


def format_location_display(code: str) -> str:
    text = str(code or "").strip().replace("-", "")
    if len(text) >= 6 and text.isdigit():
        return f"{text[:2]}-{text[2:4]}-{text[4:6]}"
    return code


def closed_loop_label(item: Dict[str, str]) -> str:
    return "是" if item.get("is_small") == "1" else "否"


def item_key(item: Dict[str, str]) -> Tuple[str, str]:
    return (item["sku_code"], item["location_code"])


def public_item(item: Dict[str, str]) -> Dict[str, str]:
    return {
        "out_item_id": item.get("out_item_id", ""),
        "location_code": item.get("location_code", ""),
        "location_display": format_location_display(item.get("location_code", "")),
        "sku_code": item.get("sku_code", ""),
        "name": item.get("name", ""),
        "推荐工具": item.get("推荐工具", ""),
        "包装类型": item.get("包装类型", ""),
        "货架属性": item.get("shelf_attribute", ""),
        "是否闭环抓取": closed_loop_label(item),
        "shelf_number": item.get("shelf_number", ""),
        "level": item.get("level", ""),
        "bin_unit": item.get("bin_unit", ""),
        "is_small": item.get("is_small", "0"),
        "is_special": item.get("is_special", "0"),
        "is_code_pusher": item.get("is_code_pusher", "0"),
        "key": f"{item.get('sku_code', '')}|{item.get('location_code', '')}",
    }


def load_candidates(
    shelves_csv: Path,
    unavailable: Set[str],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
) -> List[Dict[str, str]]:
    """1) load all SKU rows  2) exclude unavailable."""
    if not shelves_csv.is_file():
        raise FileNotFoundError(f"库位表不存在：{shelves_csv}")
    candidates: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    with shelves_csv.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            sku = (row.get("sku_code") or "").strip()
            if not sku:
                continue
            if sku in unavailable:
                continue
            attr = (row.get("shelf_attribute") or "").strip()
            loc = location_code(row)
            if not loc:
                continue
            key = (sku, loc, (row.get("out_item_id") or "").strip())
            if key in seen:
                continue
            seen.add(key)
            tool = tools.get(sku, DEFAULT_TOOL)
            pkg = packaging.get(sku, "")
            candidates.append(
                {
                    "out_item_id": (row.get("out_item_id") or "").strip(),
                    "location_code": loc,
                    "sku_code": sku,
                    "name": (row.get("name") or "").strip(),
                    "推荐工具": tool,
                    "包装类型": pkg,
                    "shelf_attribute": attr,
                    "shelf_number": (row.get("shelf_number") or "").strip(),
                    "level": (row.get("level") or "").strip(),
                    "bin_unit": (row.get("bin_unit") or "").strip(),
                    "is_small": "1" if sku in small_skus else "0",
                    "is_special": "1" if tool in SPECIAL_TOOLS else "0",
                    "is_code_pusher": "1" if attr == CODE_PUSHER else "0",
                }
            )
    return candidates


def sort_key(row: Dict[str, str]) -> tuple:
    shelf = row.get("shelf_number") or ""
    level = row.get("level") or ""
    unit = row.get("bin_unit") or ""
    loc = row.get("location_code") or ""
    if not (shelf and level and unit) and len(loc) >= 6:
        shelf, level, unit = loc[:2], loc[2:4], loc[4:6]

    def num(value: str) -> tuple:
        return (0, int(value)) if value.isdigit() else (1, value)

    return (
        num(shelf),
        num(level),
        num(unit),
        row.get("sku_code", ""),
        row.get("out_item_id", ""),
    )


def _pick_one(
    pool: List[Dict[str, str]],
    selected: List[Dict[str, str]],
    selected_keys: Set[Tuple[str, str]],
    used_skus: Set[str],
) -> Optional[Dict[str, str]]:
    if not pool:
        return None
    unused = [item for item in pool if item_key(item) not in selected_keys]
    if not unused:
        return None
    fresh = [item for item in unused if item["sku_code"] not in used_skus]
    choices = fresh or unused
    item = random.choice(choices)
    selected.append(item)
    selected_keys.add(item_key(item))
    used_skus.add(item["sku_code"])
    return item


def _ratio_fill_by_predicate(
    by_shelf: Dict[str, List[Dict[str, str]]],
    shelves: List[str],
    selected: List[Dict[str, str]],
    selected_keys: Set[Tuple[str, str]],
    used_skus: Set[str],
    target: int,
    ratio_target: int,
    predicate: Callable[[Dict[str, str]], bool],
) -> None:
    current = sum(1 for item in selected if predicate(item))
    if len(selected) >= target or current >= ratio_target or ratio_target < 1:
        return
    remaining_by_shelf = {
        shelf: [
            item
            for item in items
            if predicate(item) and item_key(item) not in selected_keys
        ]
        for shelf, items in by_shelf.items()
    }
    active = [shelf for shelf in shelves if remaining_by_shelf.get(shelf)]
    random.shuffle(active)
    guard = 0
    while (
        len(selected) < target
        and current < ratio_target
        and active
        and guard < target * 20
    ):
        guard += 1
        shelf = active.pop(0)
        shelf_pool = remaining_by_shelf[shelf]
        picked = _pick_one(shelf_pool, selected, selected_keys, used_skus)
        if picked is None:
            continue
        current += 1
        remaining_by_shelf[shelf] = [
            item for item in shelf_pool if item_key(item) not in selected_keys
        ]
        if remaining_by_shelf[shelf]:
            active.append(shelf)


def select_items(
    candidates: List[Dict[str, str]],
    target: int,
    closed_loop_enabled: bool,
    closed_loop_ratio: float,
    tool_enabled: bool,
    selected_tool: str,
    target_tool_ratio: float,
    packaging_enabled: bool,
    selected_packaging: str,
    target_packaging_ratio: float,
) -> List[Dict[str, str]]:
    """生成规则：

    1) 候选已含全部 SKU，并已排除不可处理
    2) 闭环吸取：按选项占比抽取
    3) 使用工具：按选项/占比抽取
    4) 包装类型：按选项/占比抽取
    5) 剩余优先按小药（闭环）补齐到配置数量
    """
    if target < 1:
        raise ValueError("数量必须 >= 1。")
    if tool_enabled and selected_tool not in TOOL_CHOICES:
        raise ValueError(f"不支持的工具类型：{selected_tool}")
    if packaging_enabled and not str(selected_packaging or "").strip():
        raise ValueError("包装类型不能为空。")

    closed_ratio = (
        min(1.0, max(0.0, float(closed_loop_ratio))) if closed_loop_enabled else 0.0
    )
    tool_ratio = (
        min(1.0, max(0.0, float(target_tool_ratio))) if tool_enabled else 0.0
    )
    packaging_ratio = (
        min(1.0, max(0.0, float(target_packaging_ratio)))
        if packaging_enabled
        else 0.0
    )
    use_all_tools = selected_tool == ALL_TOOLS
    packaging_value = str(selected_packaging or "").strip()
    use_all_packaging = packaging_value in {"", ALL_PACKAGING}

    if not candidates:
        raise ValueError("按当前过滤条件没有可生成的候选。")

    by_shelf: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for item in candidates:
        shelf = item.get("shelf_number") or (item.get("location_code") or "")[:2]
        by_shelf[shelf].append(item)

    selected: List[Dict[str, str]] = []
    selected_keys: Set[Tuple[str, str]] = set()
    used_skus: Set[str] = set()
    shelves = sorted(
        by_shelf.keys(),
        key=lambda shelf: (0, int(shelf)) if shelf.isdigit() else (1, shelf),
    )

    # 3) 闭环吸取按占比
    if closed_loop_enabled:
        closed_target = min(target, int(target * closed_ratio))
        _ratio_fill_by_predicate(
            by_shelf,
            shelves,
            selected,
            selected_keys,
            used_skus,
            target,
            closed_target,
            lambda item: item.get("is_small") == "1",
        )

    # 4) 使用工具按占比
    if tool_enabled:
        tool_target = min(target, int(target * tool_ratio))
        if use_all_tools:
            _ratio_fill_by_predicate(
                by_shelf,
                shelves,
                selected,
                selected_keys,
                used_skus,
                target,
                tool_target,
                lambda item: True,
            )
        else:
            _ratio_fill_by_predicate(
                by_shelf,
                shelves,
                selected,
                selected_keys,
                used_skus,
                target,
                tool_target,
                lambda item: (item.get("推荐工具") or DEFAULT_TOOL) == selected_tool,
            )

    # 5) 包装类型按占比
    if packaging_enabled:
        packaging_target = min(target, int(target * packaging_ratio))
        if use_all_packaging:
            _ratio_fill_by_predicate(
                by_shelf,
                shelves,
                selected,
                selected_keys,
                used_skus,
                target,
                packaging_target,
                lambda item: True,
            )
        else:
            _ratio_fill_by_predicate(
                by_shelf,
                shelves,
                selected,
                selected_keys,
                used_skus,
                target,
                packaging_target,
                lambda item: (item.get("包装类型") or "") == packaging_value,
            )

    # 剩余优先按小药补齐
    if len(selected) < target:
        remaining_by_shelf = {
            shelf: [item for item in items if item_key(item) not in selected_keys]
            for shelf, items in by_shelf.items()
        }
        active = [shelf for shelf in shelves if remaining_by_shelf.get(shelf)]
        random.shuffle(active)
        guard = 0
        while len(selected) < target and active and guard < target * 20:
            guard += 1
            shelf = active.pop(0)
            shelf_pool = remaining_by_shelf[shelf]
            smalls = [item for item in shelf_pool if item.get("is_small") == "1"]
            picked = _pick_one(smalls or shelf_pool, selected, selected_keys, used_skus)
            if picked is None:
                continue
            remaining_by_shelf[shelf] = [
                item for item in shelf_pool if item_key(item) not in selected_keys
            ]
            if remaining_by_shelf[shelf]:
                active.append(shelf)

    selected.sort(key=sort_key)
    return selected


def summarize(rows: Iterable[Dict[str, str]]) -> Dict[str, object]:
    rows_list = list(rows)
    shelf_counts: Dict[str, int] = defaultdict(int)
    for row in rows_list:
        shelf = row.get("shelf_number") or (row.get("location_code") or "")[:2]
        shelf_counts[shelf] += 1
    shelves = sorted(
        shelf_counts.keys(),
        key=lambda shelf: (0, int(shelf)) if shelf.isdigit() else (1, shelf),
    )
    tool_counts: Dict[str, int] = defaultdict(int)
    packaging_counts: Dict[str, int] = defaultdict(int)
    for row in rows_list:
        tool_counts[row.get("推荐工具") or DEFAULT_TOOL] += 1
        pkg = row.get("包装类型") or "-"
        packaging_counts[pkg] += 1
    per_shelf = [shelf_counts[shelf] for shelf in shelves]
    return {
        "count": len(rows_list),
        "shelf_count": len(shelves),
        "small_count": sum(1 for row in rows_list if row.get("is_small") == "1"),
        "special_count": sum(1 for row in rows_list if row.get("is_special") == "1"),
        "code_pusher_count": sum(
            1 for row in rows_list if row.get("is_code_pusher") == "1"
        ),
        "tool_counts": dict(tool_counts),
        "packaging_counts": dict(packaging_counts),
        "per_shelf_min": min(per_shelf) if per_shelf else 0,
        "per_shelf_max": max(per_shelf) if per_shelf else 0,
    }


def rows_to_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for item in rows:
        public = public_item(item)
        writer.writerow({field: public.get(field, "") for field in CSV_EXPORT_FIELDS})
    return buffer.getvalue().encode("utf-8-sig")


def normalize_import_location(raw: str) -> str:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return ""
    if text[0].isalpha():
        if "-" in text[:3]:
            text = text.split("-", 1)[1]
        else:
            index = 0
            while index < len(text) and text[index].isalpha():
                index += 1
            text = text[index:]
    if "-" in text:
        parts = [part.strip() for part in text.split("-") if part.strip()]
        if len(parts) == 3:
            return "".join(
                part.zfill(2) if part.isdigit() else part for part in parts
            )
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return text.replace("-", "")


def _lookup_maps(
    candidates: List[Dict[str, str]],
) -> Tuple[Dict[Tuple[str, str], Dict[str, str]], Dict[Tuple[str, str], Dict[str, str]]]:
    by_sku: Dict[Tuple[str, str], Dict[str, str]] = {}
    by_out: Dict[Tuple[str, str], Dict[str, str]] = {}
    for item in candidates:
        loc = item.get("location_code") or ""
        sku = item.get("sku_code") or ""
        out_id = item.get("out_item_id") or ""
        if sku and loc:
            by_sku[(sku, loc)] = item
        if out_id and loc:
            by_out[(out_id, loc)] = item
    return by_sku, by_out


def _build_item_from_import_row(
    row: Dict[str, str],
    by_sku: Dict[Tuple[str, str], Dict[str, str]],
    by_out: Dict[Tuple[str, str], Dict[str, str]],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
) -> Dict[str, str]:
    sku = (
        (row.get("sku_code") or row.get("69码") or row.get("商品条码") or "")
        .strip()
    )
    out_id = (row.get("out_item_id") or row.get("商品编码") or "").strip()
    loc = normalize_import_location(
        row.get("location_code") or row.get("库位") or ""
    )
    name = (row.get("name") or row.get("药品名称") or "").strip()
    if not loc:
        raise ValueError("缺少库位 location_code")
    if not sku and not out_id:
        raise ValueError("缺少 sku_code 或 out_item_id")

    matched = None
    if sku and (sku, loc) in by_sku:
        matched = by_sku[(sku, loc)]
    elif out_id and (out_id, loc) in by_out:
        matched = by_out[(out_id, loc)]
    if matched is not None:
        return dict(matched)

    if not sku:
        sku = out_id
    if not out_id:
        out_id = sku
    tool = (
        (row.get("推荐工具") or row.get("使用工具") or "").strip()
        or tools.get(sku, DEFAULT_TOOL)
    )
    attr = (row.get("货架属性") or row.get("shelf_attribute") or "").strip()
    pkg = (row.get("包装类型") or "").strip() or packaging.get(sku, "")
    shelf = loc[:2] if len(loc) >= 2 else ""
    level = loc[2:4] if len(loc) >= 4 else ""
    unit = loc[4:6] if len(loc) >= 6 else ""
    return {
        "out_item_id": out_id,
        "location_code": loc,
        "sku_code": sku,
        "name": name,
        "推荐工具": tool,
        "包装类型": pkg,
        "shelf_attribute": attr,
        "shelf_number": shelf,
        "level": level,
        "bin_unit": unit,
        "is_small": "1" if sku in small_skus else "0",
        "is_special": "1" if tool in SPECIAL_TOOLS else "0",
        "is_code_pusher": "1" if attr == CODE_PUSHER else "0",
    }


def parse_import_csv(
    csv_text: str,
    candidates: List[Dict[str, str]],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    text = str(csv_text or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("CSV 内容为空。")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV 缺少表头。")
    by_sku, by_out = _lookup_maps(candidates)
    selected: List[Dict[str, str]] = []
    selected_keys: Set[Tuple[str, str]] = set()
    errors: List[str] = []
    for index, row in enumerate(reader, start=2):
        if not isinstance(row, dict):
            continue
        if not any(str(value or "").strip() for value in row.values()):
            continue
        try:
            item = _build_item_from_import_row(
                row, by_sku, by_out, tools, small_skus, packaging
            )
        except ValueError as error:
            errors.append(f"第 {index} 行：{error}")
            continue
        key = item_key(item)
        if key in selected_keys:
            continue
        selected_keys.add(key)
        selected.append(item)
    if not selected:
        detail = "；".join(errors[:5])
        raise ValueError(
            "CSV 未解析出有效药品。" + (f"（{detail}）" if detail else "")
        )
    # Keep CSV row order; UI may sort on demand.
    return selected, errors
