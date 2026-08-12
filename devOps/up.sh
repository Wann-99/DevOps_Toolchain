#!/usr/bin/env bash
# 本地管理 knowledge_shelf_query 容器。
#   bash up.sh start [版本]       构建应用包并启动
#   bash up.sh restart [版本]     重新构建应用包并重建容器
#   bash up.sh build-bin [版本]   仅构建应用包
#   bash up.sh down               停止
#   bash up.sh pull-runtime       拉取固定运行时镜像
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SCRIPT_DIR}"

ACTION="${1:-}"
VERSION="${2:-dev}"
APP_BIN="${APP_DIR}/deploy/standalone/bin/knowledge_shelf_query.bin"
[[ -n "${ACTION}" ]] || {
    echo "[ERROR] 缺少动作参数。用法: bash up.sh {start|restart|build-bin|down|pull-runtime} [版本]"
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
        build_bin
        compose_cli up -d
        ;;
    restart)
        ensure_paths
        build_bin
        compose_cli up -d --force-recreate
        ;;
    build-bin)
        build_bin
        ;;
    down)
        compose_cli down
        ;;
    pull-runtime|pull)
        compose_cli pull
        ;;
    *)
        echo "[ERROR] 不支持的动作: ${ACTION}"
        echo "用法: bash up.sh {start|restart|build-bin|down|pull-runtime} [版本]"
        exit 1
        ;;
esac

echo "[DONE] 完成"
