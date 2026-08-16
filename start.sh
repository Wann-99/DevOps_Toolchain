#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-../VfmApp_deploy/model/templates/knowledge}"
CONFIG_PNP_DIR="${CONFIG_PNP_DIR:-../PNPApp_deploy/config_pnp}"

# 旧参数（由 config_pnp 目录驱动，不再逐项指定）:
# SHELVES_FILE="${SHELVES_FILE:-../PNPApp_deploy/config_pnp/sku-shelves.csv}"
# UNAVAILABLE_FILE="${UNAVAILABLE_FILE:-../PNPApp_deploy/config_pnp/unavailabel_obj.json}"
# TOOL_MAPPING_FILE="${TOOL_MAPPING_FILE:-../PNPApp_deploy/config_pnp/obj_tool_mapping.json}"
# PICK_STRATEGY_FILE="${PICK_STRATEGY_FILE:-../PNPApp_deploy/config_pnp/pick_strategy_obj.json}"

exec python3 app.py \
  --knowledge "$KNOWLEDGE_DIR" \
  --config-pnp "$CONFIG_PNP_DIR"
# 旧参数（由 config_pnp 目录驱动，不再逐项指定）:
#   --shelves "$SHELVES_FILE" \
#   --unavailable "$UNAVAILABLE_FILE" \
#   --tool-mapping "$TOOL_MAPPING_FILE" \
#   --pick-strategy "$PICK_STRATEGY_FILE"
