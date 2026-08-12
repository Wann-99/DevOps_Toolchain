"""Extract knowledge + shelf + optional configs from a zip bundle."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ksq.constants import (
    PICK_STRATEGY_FILE_NAME,
    SHELVES_FILE_NAME,
    SHELVES_FILE_PREFIX,
    TOOL_MAPPING_FILE_NAME,
)
from ksq.models import BundlePaths
from ksq.naming import (
    is_knowledge_member,
    is_pick_strategy_file_name,
    is_shelves_file_name,
    is_tool_mapping_file_name,
    is_unavailable_file_name,
)


def extract_bundle_from_zip(zip_path: Path, destination: Path) -> BundlePaths:
    if not zip_path.is_file():
        raise FileNotFoundError(f"压缩包不存在：{zip_path}")
    if destination.exists():
        raise ValueError(f"解压目标目录已存在：{destination}")
    destination.mkdir(parents=True, exist_ok=True)

    knowledge_directory = destination / "knowledge"
    knowledge_directory.mkdir()
    shelves_file: Path | None = None
    unavailable_file: Path | None = None
    tool_mapping_file: Path | None = None
    pick_strategy_file: Path | None = None
    saved_knowledge_count = 0

    with zipfile.ZipFile(zip_path) as archive:
        for member_name in archive.namelist():
            if member_name.endswith("/"):
                continue
            file_name = Path(member_name).name
            if is_shelves_file_name(file_name):
                shelves_file = destination / SHELVES_FILE_NAME
                with archive.open(member_name) as raw_file, shelves_file.open("wb") as out:
                    out.write(raw_file.read())
                continue
            if is_unavailable_file_name(file_name):
                unavailable_file = destination / "unavailabel_obj.json"
                with archive.open(member_name) as raw_file, unavailable_file.open(
                    "wb"
                ) as out:
                    out.write(raw_file.read())
                continue
            if is_tool_mapping_file_name(file_name):
                tool_mapping_file = destination / TOOL_MAPPING_FILE_NAME
                with archive.open(member_name) as raw_file, tool_mapping_file.open(
                    "wb"
                ) as out:
                    out.write(raw_file.read())
                continue
            if is_pick_strategy_file_name(file_name):
                pick_strategy_file = destination / PICK_STRATEGY_FILE_NAME
                with archive.open(member_name) as raw_file, pick_strategy_file.open(
                    "wb"
                ) as out:
                    out.write(raw_file.read())
                continue
            if is_knowledge_member(member_name, file_name):
                target = knowledge_directory / file_name
                with archive.open(member_name) as raw_file, target.open("wb") as out:
                    out.write(raw_file.read())
                saved_knowledge_count += 1

    if saved_knowledge_count == 0:
        raise ValueError("压缩包中未找到 knowledge JSON 文件。")
    if shelves_file is None:
        raise ValueError(
            f"压缩包中未找到库位表（文件名以 {SHELVES_FILE_PREFIX} 开头的 .csv）。"
        )
    return BundlePaths(
        knowledge_directory=knowledge_directory,
        shelves_file=shelves_file,
        unavailable_file=unavailable_file,
        tool_mapping_file=tool_mapping_file,
        pick_strategy_file=pick_strategy_file,
    )
