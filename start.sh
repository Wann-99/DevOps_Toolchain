#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-../VfmApp_deploy/model/templates/knowledge}"
SHELVES_FILE="${SHELVES_FILE:-../PNPApp_deploy/config_pnp/sku-shelves.csv}"
UNAVAILABLE_FILE="${UNAVAILABLE_FILE:-../PNPApp_deploy/config_pnp/unavailabel_obj.json}"
TOOL_MAPPING_FILE="${TOOL_MAPPING_FILE:-../PNPApp_deploy/config_pnp/obj_tool_mapping.json}"
PICK_STRATEGY_FILE="${PICK_STRATEGY_FILE:-../PNPApp_deploy/config_pnp/pick_strategy_obj.json}"

exec python3 app.py \
  --knowledge "$KNOWLEDGE_DIR" \
  --shelves "$SHELVES_FILE" \
  --unavailable "$UNAVAILABLE_FILE" \
  --tool-mapping "$TOOL_MAPPING_FILE" \
  --pick-strategy "$PICK_STRATEGY_FILE"
