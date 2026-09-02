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
RUNTIME_LOG_FILE: Final[Path] = Path(
    os.environ.get(
        "KSQ_LOG_FILE",
        str(APP_DIRECTORY / "logs" / "knowledge_shelf_query.log"),
    )
).expanduser()

HOST: Final[str] = "127.0.0.1"
PORT: Final[int] = 8765
APP_VERSION: Final[str] = os.environ.get("KSQ_APP_VERSION", "dev").strip() or "dev"

_DEFAULT_KNOWLEDGE_ROOT = (
    Path("/data/knowledge")
    if APP_DIRECTORY == Path("/app")
    else APP_DIRECTORY.parent / "VfmApp_deploy/model/templates"
)
# The mounted templates directory is the path base shown on the load page.
# ``knowledge`` remains the default actual dataset directory below that root.
DEFAULT_KNOWLEDGE_ROOT: Final[Path] = Path(
    os.environ.get("KSQ_KNOWLEDGE_ROOT", str(_DEFAULT_KNOWLEDGE_ROOT))
).expanduser().resolve()
DEFAULT_KNOWLEDGE: Final[Path] = Path(
    os.environ.get("KSQ_DEFAULT_KNOWLEDGE", str(DEFAULT_KNOWLEDGE_ROOT / "knowledge"))
).expanduser().resolve()
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
# VfmApp 部署目录；其 config.yaml 的 template.template_root 决定当前生效的
# knowledge 目录（见 ksq/vfm_app.py）。
DEFAULT_VFM_APP_DIR: Final[Path] = (
    Path("/data/vfm_app")
    if APP_DIRECTORY == Path("/app")
    else APP_DIRECTORY.parent / "VfmApp_deploy"
)

BASE_COLUMNS: Final[Tuple[str, ...]] = (
    "id",
    "69码",
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
FEISHU_RULES_FILE: Final[Path] = APP_DIRECTORY / "feishu_rules.json"
DEFAULT_ETM_BASE_URL: Final[str] = "http://127.0.0.1:12005"
# Host-mounted path inside knowledge_shelf_query for robot keyboard env sync.
ROBOT_KEYBOARD_ENV_FILE: Final[Path] = Path("/data/robot_keyboard.env")

# 穹彻 Hermes 底盘（导航/建图）RESTful API 相关配置。底盘与本工具部署在同一
# 内网、IP 固定；出厂默认走有线口 192.168.11.1，端口固定 1448。
DEFAULT_ROBOT_BASE_URL: Final[str] = "http://192.168.11.1:1448"
ROBOT_MAP_SETTINGS_FILE: Final[Path] = APP_DIRECTORY / "robot_map_settings.json"
ROBOT_MAP_POIS_FILE: Final[Path] = APP_DIRECTORY / "robot_map_pois_cache.json"

PACKAGE_VERSION: Final[int] = 4
PACKAGE_SUFFIX: Final[str] = ".kpkg"
DEFAULT_TOOL_NAME: Final[str] = "double_vacuum_gripper"

SHELVES_FILE_NAME: Final[str] = "sku-shelves.csv"
SHELVES_FILE_PREFIX: Final[str] = "sku-shelves"
ETM_SHELVES_FILE_PREFIX: Final[str] = "etm_sku_locations_cache"
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
