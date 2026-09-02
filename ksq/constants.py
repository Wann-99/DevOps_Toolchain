"""Shared constants and default filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Tuple


PACKAGE_DIRECTORY: Final[Path] = Path(__file__).resolve().parent
SOURCE_APP_DIRECTORY: Final[Path] = PACKAGE_DIRECTORY.parent
APP_DIRECTORY: Final[Path] = Path(
    os.environ.get("KSQ_APP_DIRECTORY", str(SOURCE_APP_DIRECTORY))
).expanduser().resolve()
RUNTIME_UPLOAD_DIRECTORY: Final[Path] = APP_DIRECTORY / ".runtime_upload"

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 8765
APP_VERSION: Final[str] = os.environ.get("KSQ_APP_VERSION", "dev").strip() or "dev"

DEFAULT_KNOWLEDGE: Final[Path] = (
    APP_DIRECTORY.parent / "VfmApp_deploy/model/templates/knowledge"
)
DEFAULT_SHELVES: Final[Path] = (
    APP_DIRECTORY.parent / "PNPApp_deploy/config_pnp/sku-shelves.csv"
)
DEFAULT_UNAVAILABLE: Final[Path] = (
    APP_DIRECTORY.parent / "PNPApp_deploy/config_pnp/unavailable_obj.json"
)
DEFAULT_TOOL_MAPPING: Final[Path] = (
    APP_DIRECTORY.parent / "PNPApp_deploy/config_pnp/obj_tool_mapping.json"
)
DEFAULT_PICK_STRATEGY: Final[Path] = (
    APP_DIRECTORY.parent / "PNPApp_deploy/config_pnp/pick_strategy_obj.json"
)
DEFAULT_CONFIG_PNP_DIR: Final[Path] = (
    APP_DIRECTORY.parent / "PNPApp_deploy/config_pnp"
)

BASE_COLUMNS: Final[Tuple[str, ...]] = (
    "id",
    "商品编码",
    "药品名称",
    "库位",
    "货架属性",
    "使用工具",
    "是否闭环",
    "是否不可处理",
    "挡板高度",
)

ORDER_CONFIG_FILE: Final[Path] = APP_DIRECTORY / "order_config.json"
ORDER_CONFIG_PROD_FILE: Final[Path] = APP_DIRECTORY / "order_config.prod.json"
TEST_ORDER_STATE_FILE: Final[Path] = APP_DIRECTORY / "test_order_state.json"
DASHBOARD_SETTINGS_FILE: Final[Path] = APP_DIRECTORY / "dashboard_settings.json"
DASHBOARD_ACTIVE_ORDER_FILE: Final[Path] = APP_DIRECTORY / "dashboard_active_order.json"
USERS_FILE: Final[Path] = APP_DIRECTORY / "users.json"
DEFAULT_ETM_BASE_URL: Final[str] = "http://127.0.0.1:12005"
# Host-mounted path inside knowledge_shelf_query for robot keyboard env sync.
ROBOT_KEYBOARD_ENV_FILE: Final[Path] = Path("/data/robot_keyboard.env")

# Slamware chassis connection and locally persisted map navigation state.
DEFAULT_ROBOT_BASE_URL: Final[str] = "http://192.168.11.1:1448"
ROBOT_MAP_SETTINGS_FILE: Final[Path] = APP_DIRECTORY / "robot_map_settings.json"
ROBOT_MAP_POIS_FILE: Final[Path] = APP_DIRECTORY / "robot_map_pois_cache.json"

PACKAGE_VERSION: Final[int] = 3
PACKAGE_SUFFIX: Final[str] = ".kpkg"
DEFAULT_TOOL_NAME: Final[str] = "double_vacuum_gripper"

SHELVES_FILE_NAME: Final[str] = "sku-shelves.csv"
SHELVES_FILE_PREFIX: Final[str] = "sku-shelves"
TOOL_MAPPING_FILE_NAME: Final[str] = "obj_tool_mapping.json"
TOOL_MAPPING_FILE_PREFIX: Final[str] = "obj_tool_mapping"
PICK_STRATEGY_FILE_NAME: Final[str] = "pick_strategy_obj.json"
PICK_STRATEGY_FILE_PREFIX: Final[str] = "pick_strategy_obj"
UNAVAILABLE_FILE_PREFIXES: Final[Tuple[str, ...]] = (
    "unavailabel_obj",
    "unavailable_obj",
)
UNAVAILABLE_FILE_NAMES: Final[frozenset] = frozenset(
    {"unavailabel_obj.json", "unavailable_obj.json"}
)
OPTIONAL_CONFIG_FILE_NAMES: Final[frozenset] = frozenset(
    {
        TOOL_MAPPING_FILE_NAME,
        PICK_STRATEGY_FILE_NAME,
        *UNAVAILABLE_FILE_NAMES,
    }
)

EMPTY_DISPLAY_PLACEHOLDERS: Final[frozenset] = frozenset(
    {
        "",
        "-",
        "未命名",
        "未找到名称",
        "未找到库位",
        "无库位",
        "null",
        "undefined",
    }
)
