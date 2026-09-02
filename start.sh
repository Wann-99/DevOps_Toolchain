#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ACTION="${1:-start}"
if [[ "${ACTION}" == "runtime-logs" ]]; then
  mkdir -p logs
  touch logs/knowledge_shelf_query.log
  exec tail -n 200 -f logs/knowledge_shelf_query.log
fi
if [[ "${ACTION}" != "start" ]]; then
  echo "用法: bash start.sh [runtime-logs]" >&2
  exit 2
fi

# KNOWLEDGE_DIR is the templates root; the default target is its knowledge child.
KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-../VfmApp_deploy/model/templates}"
CONFIG_PNP_DIR="${CONFIG_PNP_DIR:-../PNPApp_deploy/config_pnp}"

# 兼容旧配置：旧版本把 KNOWLEDGE_DIR 填成 templates/knowledge，新的挂载
# 约定以 templates 为根。仅对父目录名确为 templates 的路径自动提升。
knowledge_root_candidate="${KNOWLEDGE_DIR%/}"
if [[ "${knowledge_root_candidate##*/}" == "knowledge" \
    && "$(basename "$(dirname "${knowledge_root_candidate}")")" == "templates" \
    && -d "$(dirname "${knowledge_root_candidate}")" \
    && ! -d "${knowledge_root_candidate}/knowledge" ]]; then
  echo "[WARN] KNOWLEDGE_DIR 使用旧的 templates/knowledge 路径，已改用其 templates 父目录"
  KNOWLEDGE_DIR="$(dirname "${knowledge_root_candidate}")"
fi

# 旧参数（由 config_pnp 目录驱动，不再逐项指定）:
# SHELVES_FILE="${SHELVES_FILE:-../PNPApp_deploy/config_pnp/sku-shelves.csv}"
# UNAVAILABLE_FILE="${UNAVAILABLE_FILE:-../PNPApp_deploy/config_pnp/unavailabel_obj.json}"
# TOOL_MAPPING_FILE="${TOOL_MAPPING_FILE:-../PNPApp_deploy/config_pnp/obj_tool_mapping.json}"
# PICK_STRATEGY_FILE="${PICK_STRATEGY_FILE:-../PNPApp_deploy/config_pnp/pick_strategy_obj.json}"

exec python3 app.py \
  --knowledge-root "$KNOWLEDGE_DIR" \
  --knowledge "$KNOWLEDGE_DIR/knowledge" \
  --config-pnp "$CONFIG_PNP_DIR"
# 旧参数（由 config_pnp 目录驱动，不再逐项指定）:
#   --shelves "$SHELVES_FILE" \
#   --unavailable "$UNAVAILABLE_FILE" \
#   --tool-mapping "$TOOL_MAPPING_FILE" \
#   --pick-strategy "$PICK_STRATEGY_FILE"
