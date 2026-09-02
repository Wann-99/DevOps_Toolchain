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
    "sku_id",
    "out_item_id",
    "location_code",
    "sku_code",
    "name",
    "推荐工具",
    "包装类型",
    "货架属性",
    "是否闭环抓取",
)

# 生成清单时的固定展示列（key 与 public_item 输出字段一致）。
# 导入 CSV 时改为文件自身的表头，见 parse_import_csv_full。
DEFAULT_COLUMNS: List[Dict[str, str]] = [
    {"key": "sku_id", "label": "SKU ID"},
    {"key": "out_item_id", "label": "商品编码"},
    {"key": "location_display", "label": "库位"},
    {"key": "sku_code", "label": "69码"},
    {"key": "name", "label": "药品名称"},
    {"key": "是否闭环抓取", "label": "是否闭环抓取"},
    {"key": "货架属性", "label": "货架属性"},
    {"key": "推荐工具", "label": "推荐工具"},
    {"key": "包装类型", "label": "包装类型"},
]


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


def item_identity(item: Dict[str, str]) -> str:
    # 回退到商品编码：导入 CSV 只填「商品编码+库位」时也要有非空标识，
    # 否则 key 形如 "|520701"，_parse_key 会判为非法而无法下单/移动。
    return str(
        item.get("sku_id") or item.get("sku_code") or item.get("out_item_id") or ""
    ).strip()


def item_key(item: Dict[str, str]) -> Tuple[str, str]:
    return (item_identity(item), item["location_code"])


def public_item(item: Dict[str, str]) -> Dict[str, str]:
    location_parts = [
        str(item.get(key) or "").strip()
        for key in ("shelf_number", "level", "bin_unit")
    ]
    location_display = (
        "-".join(location_parts)
        if all(location_parts)
        else format_location_display(item.get("location_code", ""))
    )
    public = {
        "sku_id": item.get("sku_id", ""),
        "out_item_id": item.get("out_item_id", ""),
        "location_code": item.get("location_code", ""),
        "location_display": location_display,
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
        # 组合模式下同一 69码 可能属于多个组合，key 追加组合标识保证唯一
        "key": (
            f"{item_identity(item)}|{item.get('location_code', '')}"
            + (
                f"|{str(item.get('group_id') or '').strip()}"
                if str(item.get("group_id") or "").strip()
                else ""
            )
        ),
    }
    for field in ("order_batch_id", "ordered_at", "order_no", "task_id"):
        value = item.get(field, "")
        if value:
            public[field] = value
    # 导入 CSV 时保留的原始行数据与组合标识，用于动态列渲染
    display = item.get("display")
    if isinstance(display, dict):
        public["display"] = {str(key): str(value) for key, value in display.items()}
    if "group_id" in item:
        public["group_id"] = str(item.get("group_id") or "")
    return public


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
        reader = csv.DictReader(file)
        if reader.fieldnames is not None:
            reader.fieldnames = [name.lstrip("\ufeff") for name in reader.fieldnames]
        for row in reader:
            sku_id = (row.get("sku_id") or "").strip()
            sku = (row.get("sku_code") or "").strip()
            identity = sku_id or sku
            if not identity:
                continue
            identifiers = tuple(value for value in (sku_id, sku) if value)
            if any(value in unavailable for value in identifiers):
                continue
            attr = (row.get("shelf_attribute") or "").strip()
            loc = location_code(row)
            if not loc:
                continue
            key = (identity, loc, (row.get("out_item_id") or "").strip())
            if key in seen:
                continue
            seen.add(key)
            tool = next(
                (tools[value] for value in identifiers if value in tools), DEFAULT_TOOL
            )
            pkg = next(
                (packaging[value] for value in identifiers if value in packaging), ""
            )
            candidates.append(
                {
                    "sku_id": sku_id,
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
                    "is_small": (
                        "1" if any(value in small_skus for value in identifiers) else "0"
                    ),
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
        item_identity(row),
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
    fresh = [item for item in unused if item_identity(item) not in used_skus]
    choices = fresh or unused
    item = random.choice(choices)
    selected.append(item)
    selected_keys.add(item_key(item))
    used_skus.add(item_identity(item))
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


def display_rows_to_csv_bytes(
    rows: List[Dict[str, str]],
    columns: List[Dict[str, str]],
    leading: Tuple[Tuple[str, str], ...] = (),
) -> bytes:
    """按当前展示列导出 CSV：表头用列 label，单元格取行 display 值。

    leading 为附加的前置列 (表头, 取值键)，例如已下单列表的下单时间/订单号。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for header, _ in leading] + [col["label"] for col in columns])
    for item in rows:
        display = item.get("display")
        display = display if isinstance(display, dict) else {}
        row = [str(item.get(key, "") or "") for _, key in leading]
        row.extend(
            str(display.get(col["key"], item.get(col["key"], "")) or "")
            for col in columns
        )
        writer.writerow(row)
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
        return digits
    return text.replace("-", "")


_IMPORT_FIELD_ALIASES = {
    "sku_id": frozenset({"skuid", "sku编号", "skuid编码"}),
    "out_item_id": frozenset(
        {"outitemid", "itemid", "商品编码", "商品id", "货品编码"}
    ),
    "location_code": frozenset(
        {"locationcode", "库位", "库位编码", "货位", "货位编码"}
    ),
    "sku_code": frozenset(
        {"skucode", "sku", "69码", "商品条码", "条形码", "barcode"}
    ),
}


def _halfwidth(text: str) -> str:
    """全角 ASCII（０-９Ａ-Ｚａ-ｚ等）转半角，便于表头与编码匹配。"""
    return "".join(
        (
            chr(ord(char) - 0xFEE0)
            if 0xFF01 <= ord(char) <= 0xFF5E
            else (" " if char == "\u3000" else char)
        )
        for char in str(text or "")
    )


def _normalize_import_header(raw: object) -> str:
    text = _halfwidth(str(raw or "").strip()).lower()
    return "".join(
        char for char in text if not char.isspace() and char not in {"_", "-"}
    )


def _canonical_import_field(raw: object) -> str:
    normalized = _normalize_import_header(raw)
    for field, aliases in _IMPORT_FIELD_ALIASES.items():
        if normalized in aliases:
            return field
    return ""


def _normalize_import_identifier(raw: object) -> str:
    value = _halfwidth(str(raw or "").strip())
    if value.endswith(".0") and value[:-2].isdigit():
        return value[:-2]
    return value


def _import_identifiers(row: Dict[str, str]) -> Dict[str, str]:
    values = {
        "sku_id": "",
        "out_item_id": "",
        "location_code": "",
        "sku_code": "",
    }
    for raw_key, raw_value in row.items():
        field = _canonical_import_field(raw_key)
        if not field or values[field]:
            continue
        value = _normalize_import_identifier(raw_value)
        if field == "location_code":
            value = normalize_import_location(value)
        values[field] = value
    return values


def _lookup_maps(
    candidates: List[Dict[str, str]],
) -> Tuple[
    Dict[str, List[Dict[str, str]]],
    Dict[str, List[Dict[str, str]]],
    Dict[str, List[Dict[str, str]]],
    Dict[str, List[Dict[str, str]]],
]:
    by_id: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    by_sku: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    by_out: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    by_location: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for item in candidates:
        sku_id = item.get("sku_id") or ""
        loc = item.get("location_code") or ""
        sku = item.get("sku_code") or ""
        out_id = item.get("out_item_id") or ""
        if sku_id:
            by_id[sku_id].append(item)
        if sku:
            by_sku[sku].append(item)
        if out_id:
            by_out[out_id].append(item)
        if loc:
            by_location[loc].append(item)
    return dict(by_id), dict(by_sku), dict(by_out), dict(by_location)


def _item_from_identifiers(
    values: Dict[str, str],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
) -> Dict[str, str]:
    """候选数据里没有这一行时，按 CSV 自身内容成条目。

    只补能从工具映射/闭环列表/包装类型查到的属性，其余留空；商品是否真实存在
    交给下单接口判定并弹窗报错，不在导入阶段拦截。
    """
    identifiers = [
        value
        for value in (
            values["sku_id"],
            values["sku_code"],
            values["out_item_id"],
        )
        if value
    ]
    tool = next(
        (tools[value] for value in identifiers if value in tools), DEFAULT_TOOL
    )
    pkg = next((packaging[value] for value in identifiers if value in packaging), "")
    # shelf_number/level/bin_unit 留空，public_item 会用 location_code 格式化展示
    return {
        "sku_id": values["sku_id"],
        "out_item_id": values["out_item_id"],
        "location_code": values["location_code"],
        "sku_code": values["sku_code"],
        "name": "",
        "推荐工具": tool,
        "包装类型": pkg,
        "shelf_attribute": "",
        "shelf_number": "",
        "level": "",
        "bin_unit": "",
        "is_small": (
            "1" if any(value in small_skus for value in identifiers) else "0"
        ),
        "is_special": "1" if tool in SPECIAL_TOOLS else "0",
        "is_code_pusher": "0",
    }


def _build_item_from_import_row(
    row: Dict[str, str],
    by_id: Dict[str, List[Dict[str, str]]],
    by_sku: Dict[str, List[Dict[str, str]]],
    by_out: Dict[str, List[Dict[str, str]]],
    by_location: Dict[str, List[Dict[str, str]]],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
    identifiers: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], str]:
    values = identifiers if identifiers is not None else _import_identifiers(row)
    criteria = [
        ("SKU ID", values["sku_id"], by_id),
        ("商品编码", values["out_item_id"], by_out),
        ("库位", values["location_code"], by_location),
        ("69码", values["sku_code"], by_sku),
    ]
    provided = [(label, value, mapping) for label, value, mapping in criteria if value]
    if not provided:
        raise ValueError("商品编码、库位、69码至少填写一项，也支持 SKU ID")

    matches: Optional[List[Dict[str, str]]] = None
    for _, value, mapping in provided:
        current = mapping.get(value, [])
        if matches is None:
            matches = list(current)
            continue
        current_ids = {id(item) for item in current}
        matches = [item for item in matches if id(item) in current_ids]
    if not matches:
        # 候选数据里没有 → 按原文件内容导入，不拦截
        return _item_from_identifiers(values, tools, small_skus, packaging), ""

    warning = ""
    if len(matches) > 1:
        detail = "、".join(f"{label}={value}" for label, value, _ in provided)
        warning = f"{detail} 匹配 {len(matches)} 条，已按候选顺序取第一条"
    return dict(matches[0]), warning


def _normalize_fullwidth_commas(text: str) -> str:
    """引号外的全角逗号视作分隔符（兼容表头/数据混用中英文逗号的 CSV）。"""
    out: List[str] = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
            out.append(char)
        elif char == "\uff0c" and not in_quotes:
            out.append(",")
        else:
            out.append(char)
    return "".join(out)


def _unique_columns(raw_headers: List[object]) -> List[Dict[str, str]]:
    """把 CSV 原始表头转成唯一列定义（保留原始顺序与文案）。"""
    columns: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for index, raw in enumerate(raw_headers, start=1):
        label = str(raw or "").strip() or f"列{index}"
        key = label
        suffix = 2
        while key in seen:
            key = f"{label} ({suffix})"
            suffix += 1
        seen.add(key)
        columns.append({"key": key, "label": label})
    return columns


def parse_import_csv_full(
    csv_text: str,
    candidates: List[Dict[str, str]],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
    group_field: str = "",
) -> Tuple[List[Dict[str, str]], List[str], List[Dict[str, str]]]:
    """解析导入 CSV，返回 (药品列表, 提示, 动态列)。

    每行除解析出的候选药品字段外，还带 display（原始行内容，按动态列 key）
    与 group_id（指定 group_field 时，为该列的原始值）。
    """
    text = str(csv_text or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("CSV 内容为空。")
    reader = csv.reader(io.StringIO(_normalize_fullwidth_commas(text)))
    raw_rows = [
        row for row in reader if any(str(cell or "").strip() for cell in row)
    ]
    if not raw_rows:
        raise ValueError("CSV 缺少表头。")
    columns = _unique_columns(raw_rows[0])
    column_fields = [_canonical_import_field(column["label"]) for column in columns]
    recognized_headers = set(column_fields)
    if not recognized_headers.intersection(
        {"sku_id", "out_item_id", "location_code", "sku_code"}
    ):
        header_text = "、".join(
            column["label"] for column in columns if column["label"]
        )
        detail = f"（识别到的表头：{header_text}）" if header_text else ""
        raise ValueError(
            "CSV 表头至少需要商品编码、库位、69码中的一项，也支持 SKU ID。"
            + detail
        )
    group_field = str(group_field or "").strip()
    if group_field and group_field not in {column["key"] for column in columns}:
        raise ValueError(f"CSV 表头中不存在组合字段：{group_field}")
    by_id, by_sku, by_out, by_location = _lookup_maps(candidates)
    # 宽表：同一个标识字段出现多列（如两个 69码 或两个 SKU ID）才表示
    # 一行含多个药品。「一个 sku_id + 一个 69码」是同一药品的两种标识，
    # 不能拆，所以这里按字段分组而不是把两类列掘到一起数。
    member_field = ""
    member_keys: List[str] = []
    for field in ("sku_code", "sku_id"):
        keys_for_field = [
            column["key"]
            for column, column_field in zip(columns, column_fields)
            if column_field == field
        ]
        if len(keys_for_field) > 1:
            member_field = field
            member_keys = keys_for_field
            break
    selected: List[Dict[str, str]] = []
    selected_keys: Set[Tuple[str, ...]] = set()
    errors: List[str] = []
    for index, raw_row in enumerate(raw_rows[1:], start=2):
        display = {
            column["key"]: (
                str(raw_row[offset]).strip() if offset < len(raw_row) else ""
            )
            for offset, column in enumerate(columns)
        }
        # 宽表：一行含多个同类标识列时，每个非空取值拆成一条药品
        identifiers_per_row: List[Optional[Dict[str, str]]]
        if member_keys:
            identifiers_per_row = []
            for member_key in member_keys:
                member_value = _normalize_import_identifier(
                    display.get(member_key, "")
                )
                if not member_value:
                    continue
                values = _import_identifiers(display)
                values[member_field] = member_value
                identifiers_per_row.append(values)
        else:
            identifiers_per_row = [None]
        for identifiers in identifiers_per_row:
            try:
                item, warning = _build_item_from_import_row(
                    display,
                    by_id,
                    by_sku,
                    by_out,
                    by_location,
                    tools,
                    small_skus,
                    packaging,
                    identifiers=identifiers,
                )
            except ValueError as error:
                errors.append(f"第 {index} 行：{error}")
                continue
            if warning:
                errors.append(f"第 {index} 行：{warning}")
            item["display"] = dict(display)
            if group_field:
                item["group_id"] = display.get(group_field, "")
            # 组合模式允许同一药品出现在不同组合中，仅同组内去重
            key = item_key(item) + (str(item.get("group_id") or "").strip(),)
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
    return selected, errors, columns


def parse_import_csv(
    csv_text: str,
    candidates: List[Dict[str, str]],
    tools: Dict[str, str],
    small_skus: Set[str],
    packaging: Dict[str, str],
) -> Tuple[List[Dict[str, str]], List[str]]:
    selected, errors, _columns = parse_import_csv_full(
        csv_text, candidates, tools, small_skus, packaging
    )
    return selected, errors
