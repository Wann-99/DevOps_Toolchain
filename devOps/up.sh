#!/usr/bin/env bash
# 本地管理 knowledge_shelf_query 容器。
#   bash up.sh start [版本]       构建应用包并启动
#   bash up.sh restart [版本]     重新构建应用包并重建容器（自动重置状态）
#   bash up.sh build-bin [版本]   仅构建应用包
#   bash up.sh down               停止
#   bash up.sh pull-runtime       拉取固定运行时镜像
#   bash up.sh reset-state        手动重置状态文件（保留 dashboard_settings.json）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SCRIPT_DIR}"

ACTION="${1:-}"
VERSION="${2:-dev}"
APP_BIN="${APP_DIR}/deploy/standalone/bin/knowledge_shelf_query.bin"
[[ -n "${ACTION}" ]] || {
    echo "[ERROR] 缺少动作参数。用法: bash up.sh {start|restart|build-bin|down|pull-runtime|reset-state} [版本]"
    exit 1
}

COMPILED_DIR="${COMPILED_DIR:-/home/nvidia/compiled}"
export CONFIG_PNP_DIR="${CONFIG_PNP_DIR:-${COMPILED_DIR}/PNPApp_deploy/config_pnp}"
export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-${COMPILED_DIR}/VfmApp_deploy/model/templates/knowledge}"
export RUNTIME_IMAGE="${RUNTIME_IMAGE:-hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.1.0}"

compose_cli() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

# DEFAULT_ORDER_CONFIG 的 JSON 序列化（与 ksq/order/config.py 保持一致）
DEFAULT_ORDER_CONFIG_JSON='{
  "server": "",
  "client_id": "",
  "client_secret": "",
  "customer": "",
  "store_id": "",
  "store_name": "",
  "order_source": "",
  "order_time_timezone": "Asia/Shanghai",
  "need_image_upload": false,
  "business_mode_code": ""
}'

# 仅在文件不存在时写入初始值
_init_file() {
    local path="$1"
    local content="$2"
    if [[ -d "${path}" ]]; then
        echo "[ERROR] ${path} 被误创建为目录，请删除后重试（应为 JSON 文件）"
        exit 1
    fi
    [[ -f "${path}" ]] || printf '%s\n' "${content}" > "${path}"
}

# 首次创建各状态/配置文件，写入正确初始值（而非统一 {}）
ensure_files() {
    mkdir -p config
    _init_file "config/test_order_state.json" '{}'
    _init_file "config/dashboard_active_order.json" '{}'
    _init_file "config/order_config.json" "${DEFAULT_ORDER_CONFIG_JSON}"
    _init_file "config/order_config.prod.json" "${DEFAULT_ORDER_CONFIG_JSON}"
    _init_file "config/dashboard_settings.json" '{}'
    if [[ -d robot_keyboard.env ]]; then
        echo "[ERROR] robot_keyboard.env 被误创建为目录，请删除后重试"
        exit 1
    fi
    [[ -f robot_keyboard.env ]] || touch robot_keyboard.env
}

# 重置状态文件为干净初始值（不触碰 dashboard_settings.json）
# 重置前先备份到 config/.backup/（带时间戳）
reset_state() {
    mkdir -p config/.backup
    local ts
    ts="$(date +%Y%m%d_%H%M%S)"
    local f
    for f in test_order_state.json dashboard_active_order.json \
             order_config.json order_config.prod.json; do
        if [[ -f "config/${f}" ]]; then
            cp -p "config/${f}" "config/.backup/${f}.${ts}"
        fi
    done
    printf '{}\n' > "config/test_order_state.json"
    printf '{}\n' > "config/dashboard_active_order.json"
    printf '%s\n' "${DEFAULT_ORDER_CONFIG_JSON}" > "config/order_config.json"
    printf '%s\n' "${DEFAULT_ORDER_CONFIG_JSON}" > "config/order_config.prod.json"
    echo "[OK] 状态文件已重置（dashboard_settings.json 保留不变）"
    echo "[INFO] 备份位于 config/.backup/*.${ts}"
}

build_bin() {
    mkdir -p "$(dirname "${APP_BIN}")"
    python3 "${APP_DIR}/deploy/build_app_bin.py" "${VERSION}" --output "${APP_BIN}"
}

ensure_paths() {
    local missing=0
    if [[ ! -d "${CONFIG_PNP_DIR}" ]]; then
        echo "[ERROR] CONFIG_PNP_DIR 不存在: ${CONFIG_PNP_DIR}"
        missing=1
    fi
    if [[ ! -d "${KNOWLEDGE_DIR}" ]]; then
        echo "[ERROR] KNOWLEDGE_DIR 不存在: ${KNOWLEDGE_DIR}"
        missing=1
    fi
    if [[ "${missing}" -ne 0 ]]; then
        echo "[INFO] 可设置: CONFIG_PNP_DIR=... KNOWLEDGE_DIR=... bash up.sh ${ACTION}"
        exit 1
    fi
}

echo "[INFO] ACTION=${ACTION}"
echo "[INFO] CONFIG_PNP_DIR=${CONFIG_PNP_DIR}"
echo "[INFO] KNOWLEDGE_DIR=${KNOWLEDGE_DIR}"

case "${ACTION}" in
    start)
        ensure_paths
        ensure_files
        build_bin
        compose_cli up -d
        ;;
    restart)
        ensure_paths
        ensure_files
        reset_state
        build_bin
        compose_cli up -d --force-recreate
        ;;
    build-bin)
        build_bin
        ;;
    down)
        compose_cli down
        ;;
    reset-state)
        reset_state
        ;;
    pull-runtime|pull)
        compose_cli pull
        ;;
    *)
        echo "[ERROR] 不支持的动作: ${ACTION}"
        echo "用法: bash up.sh {start|restart|build-bin|down|pull-runtime|reset-state} [版本]"
        exit 1
        ;;
esac

echo "[DONE] 完成"
