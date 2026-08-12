"""Load and validate knowledge JSON records."""

from __future__ import annotations

import json
from pathlib import Path


def normalize_item_id(raw_id: object, source: str) -> str:
    if isinstance(raw_id, bool) or raw_id is None:
        raise ValueError(f"缺少有效的 id 字段：{source}")
    if isinstance(raw_id, (int, float)):
        if isinstance(raw_id, float) and not raw_id.is_integer():
            raise ValueError(f"id 不能是非整数浮点数：{source}")
        return str(int(raw_id))
    if isinstance(raw_id, str):
        item_id = raw_id.strip()
        if not item_id:
            raise ValueError(f"缺少有效的 id 字段：{source}")
        return item_id
    raise ValueError(f"id 类型无效（{type(raw_id).__name__}）：{source}")


def is_knowledge_json_filename(file_name: str) -> bool:
    name = str(file_name or "").strip()
    if not name or name.startswith("."):
        return False
    lower = name.lower()
    if not lower.endswith(".json"):
        return False
    if ".bak" in lower:
        return False
    return True


def list_knowledge_files(
    knowledge_directory: Path,
) -> tuple[list[Path], list[str]]:
    """List knowledge JSON files.

    A missing directory is a configuration error and still raises. An existing
    but empty directory is a valid (if suspicious) state: shelf data is the
    subject, knowledge is only a lookup dictionary. Callers surface the empty
    case as a prominent warning instead.
    """
    if not knowledge_directory.is_dir():
        raise FileNotFoundError(f"knowledge 目录不存在：{knowledge_directory}")

    knowledge_files: list[Path] = []
    ignored_files: list[str] = []
    for path in sorted(knowledge_directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        if is_knowledge_json_filename(path.name):
            knowledge_files.append(path)
        else:
            ignored_files.append(path.name)

    return knowledge_files, ignored_files


def load_knowledge_from_mapping(
    file_payloads: list[tuple[str, dict[str, object]]]
) -> tuple[list[dict[str, object]], list[str], list[str], list[str]]:
    knowledge_records: list[dict[str, object]] = []
    seen_by_id: dict[str, dict[str, object]] = {}
    duplicate_knowledge_files: list[str] = []
    filename_id_mismatches: list[str] = []
    conflicting_knowledge_ids: list[str] = []
    classified_count = 0

    for source_name, knowledge in file_payloads:
        if not isinstance(knowledge, dict):
            raise ValueError(f"JSON 根节点必须是对象：{source_name}")

        item_id = normalize_item_id(knowledge.get("id"), source_name)
        normalized = dict(knowledge)
        normalized["id"] = item_id

        stem = Path(source_name).stem
        if stem != item_id:
            filename_id_mismatches.append(f"{source_name} -> id={item_id}")

        existing = seen_by_id.get(item_id)
        if existing is None:
            seen_by_id[item_id] = normalized
            knowledge_records.append(normalized)
            classified_count += 1
            continue

        if existing == normalized:
            duplicate_knowledge_files.append(source_name)
            classified_count += 1
            continue

        conflicting_knowledge_ids.append(item_id)
        knowledge_records.append(normalized)
        classified_count += 1

    if classified_count != len(file_payloads):
        raise ValueError(
            "knowledge 分类数量校验失败："
            f"输入 {len(file_payloads)}，已分类 {classified_count}。"
        )

    return (
        knowledge_records,
        duplicate_knowledge_files,
        filename_id_mismatches,
        conflicting_knowledge_ids,
    )


def load_knowledge_records(
    knowledge_directory: Path,
) -> tuple[
    list[dict[str, object]], int, list[str], list[str], list[str], list[str]
]:
    knowledge_files, ignored_files = list_knowledge_files(knowledge_directory)
    payloads: list[tuple[str, dict[str, object]]] = []
    for knowledge_file in knowledge_files:
        try:
            with knowledge_file.open(encoding="utf-8") as file:
                knowledge = json.load(file)
        except UnicodeDecodeError as error:
            raise ValueError(f"JSON 文件编码错误：{knowledge_file}") from error
        except json.JSONDecodeError as error:
            raise ValueError(f"JSON 文件格式错误：{knowledge_file}") from error
        if not isinstance(knowledge, dict):
            raise ValueError(f"JSON 根节点必须是对象：{knowledge_file}")
        payloads.append((knowledge_file.name, knowledge))

    records, duplicates, mismatches, conflicts = load_knowledge_from_mapping(payloads)
    return (
        records,
        len(knowledge_files),
        duplicates,
        mismatches,
        conflicts,
        ignored_files,
    )
