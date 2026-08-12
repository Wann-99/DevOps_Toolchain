#!/usr/bin/env bash
set -euo pipefail

# 固定运行时镜像 + 独立应用包管理脚本。
# 用法:
#   bash start.sh start                 启动（默认）
#   bash start.sh restart               重建容器并启动
#   bash start.sh update <应用包.bin>   更新应用包并重建容器
#   bash start.sh rollback              回滚到上一个应用包
#   bash start.sh version               查看当前应用包版本
#   bash start.sh stop                  停止
#   bash start.sh logs                  查看日志
#   bash start.sh pull-runtime          仅拉取运行时镜像

CALLER_DIRECTORY="$(pwd)"
cd "$(dirname "${BASH_SOURCE[0]}")"

ACTION="${1:-start}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.1.0}"
APP_BIN="bin/knowledge_shelf_query.bin"
APP_BIN_BACKUP="${APP_BIN}.bak"
export RUNTIME_IMAGE
export CONFIG_PNP_DIR="${CONFIG_PNP_DIR:-/home/nvidia/compiled/PNPApp_deploy/config_pnp}"
export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-/home/nvidia/compiled/VfmApp_deploy/model/templates/knowledge}"

die() { echo "[ERROR] $*" >&2; exit 1; }

ensure_docker() {
    command -v docker >/dev/null 2>&1 || die "未找到 docker"
}

compose_cli() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

ensure_files() {
    mkdir -p config bin
    for f in dashboard_settings.json dashboard_active_order.json \
             test_order_state.json order_config.json order_config.prod.json; do
        if [[ -d "config/$f" ]]; then
            die "config/$f 被误创建为目录，请删除后重试（应为 JSON 文件）"
        fi
        [[ -f "config/$f" ]] || echo '{}' > "config/$f"
    done
    if [[ -d robot_keyboard.env ]]; then
        die "robot_keyboard.env 被误创建为目录，请删除后重试"
    fi
    [[ -f robot_keyboard.env ]] || touch robot_keyboard.env
}

ensure_paths() {
    local missing=0
    [[ -d "${CONFIG_PNP_DIR}" ]] || { echo "[ERROR] CONFIG_PNP_DIR 不存在: ${CONFIG_PNP_DIR}"; missing=1; }
    [[ -d "${KNOWLEDGE_DIR}" ]] || { echo "[ERROR] KNOWLEDGE_DIR 不存在: ${KNOWLEDGE_DIR}"; missing=1; }
    if [[ "${missing}" -ne 0 ]]; then
        die "请设置: CONFIG_PNP_DIR=... KNOWLEDGE_DIR=... bash start.sh ${ACTION}"
    fi
    [[ -f "${CONFIG_PNP_DIR}/sku-shelves.csv" ]] \
        || echo "[WARN] ${CONFIG_PNP_DIR}/sku-shelves.csv 缺失，查询功能将不可用"
}

ensure_runtime_image() {
    if ! docker image inspect "${RUNTIME_IMAGE}" >/dev/null 2>&1; then
        echo "[INFO] 本地无运行时镜像，仅首次需要拉取: ${RUNTIME_IMAGE}"
        docker pull "${RUNTIME_IMAGE}" || die "运行时镜像拉取失败: ${RUNTIME_IMAGE}"
    fi
}

verify_app_bin() {
    local candidate="$1"
    [[ -f "${candidate}" ]] || die "应用包不存在: ${candidate}"
    if ! python3 - "${candidate}" <<'PY'
import json
import sys
import zipfile

required = {
    "__main__.py",
    "KSQ_BUILD.json",
    "ksq/cli.py",
    "ksq/web/templates/shell.html",
    "ksq/web/static/app.css",
}
path = sys.argv[1]
try:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError("文件损坏: " + bad_member)
        missing = sorted(required - set(archive.namelist()))
        if missing:
            raise ValueError("缺少文件: " + "、".join(missing))
        json.loads(archive.read("KSQ_BUILD.json"))
except Exception as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
PY
    then
        die "应用包校验失败，未更新: ${candidate}"
    fi
}

show_version() {
    verify_app_bin "${APP_BIN}"
    python3 -c 'import json,sys,zipfile; data=json.loads(zipfile.ZipFile(sys.argv[1]).read("KSQ_BUILD.json")); print("版本: " + str(data.get("version", "unknown"))); print("构建时间: " + str(data.get("built_at", "unknown")))' "${APP_BIN}"
}

restart_container() {
    compose_cli up -d --force-recreate
}

wait_for_service() {
    local attempt
    for attempt in $(seq 1 90); do
        if docker exec knowledge_shelf_query python3 -c \
            'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=2); raise SystemExit(0 if response.status == 200 else 1)' \
            >/dev/null 2>&1; then
            echo "[OK] 服务已就绪"
            return 0
        fi
        sleep 1
    done
    echo "[ERROR] 服务在 90 秒内未就绪" >&2
    docker logs --tail 80 knowledge_shelf_query >&2 || true
    return 1
}

update_app_bin() {
    local candidate="${1:-}"
    [[ -n "${candidate}" ]] || die "用法: bash start.sh update <应用包.bin>"
    if [[ "${candidate}" != /* ]]; then
        candidate="${CALLER_DIRECTORY}/${candidate}"
    fi
    verify_app_bin "${candidate}"
    ensure_files

    local incoming="${APP_BIN}.incoming"
    cp "${candidate}" "${incoming}"
    verify_app_bin "${incoming}"
    if [[ -f "${APP_BIN}" ]]; then
        cp -p "${APP_BIN}" "${APP_BIN_BACKUP}"
    fi
    mv "${incoming}" "${APP_BIN}"

    if ! restart_container || ! wait_for_service; then
        if [[ -f "${APP_BIN_BACKUP}" ]]; then
            echo "[WARN] 新应用包启动失败，正在恢复上一版本"
            cp -p "${APP_BIN_BACKUP}" "${APP_BIN}"
            restart_container || true
            wait_for_service || true
        fi
        die "应用包更新失败，已尝试恢复上一版本"
    fi
    show_version
    echo "[OK] 应用包已更新，无需拉取镜像"
}

rollback_app_bin() {
    [[ -f "${APP_BIN_BACKUP}" ]] || die "没有可回滚的应用包: ${APP_BIN_BACKUP}"
    verify_app_bin "${APP_BIN_BACKUP}"
    local current="${APP_BIN}.current"
    [[ -f "${APP_BIN}" ]] && cp -p "${APP_BIN}" "${current}"
    cp -p "${APP_BIN_BACKUP}" "${APP_BIN}"
    [[ -f "${current}" ]] && mv "${current}" "${APP_BIN_BACKUP}"
    restart_container
    wait_for_service || die "已切换应用包，但服务未就绪，请查看日志"
    show_version
    echo "[OK] 已回滚应用包"
}

case "${ACTION}" in
    start)
        ensure_docker
        ensure_files
        ensure_paths
        verify_app_bin "${APP_BIN}"
        ensure_runtime_image
        compose_cli up -d
        wait_for_service || die "容器已创建，但服务未就绪，请执行 bash start.sh logs"
        show_version
        echo "[OK] 已启动，访问 http://<本机IP>:8765"
        ;;
    restart)
        ensure_docker
        ensure_files
        ensure_paths
        verify_app_bin "${APP_BIN}"
        ensure_runtime_image
        restart_container
        wait_for_service || die "容器已重建，但服务未就绪，请执行 bash start.sh logs"
        ;;
    update)
        ensure_docker
        ensure_paths
        ensure_runtime_image
        update_app_bin "${2:-}"
        ;;
    rollback)
        ensure_docker
        ensure_paths
        ensure_runtime_image
        rollback_app_bin
        ;;
    version)
        show_version
        ;;
    stop)
        ensure_docker
        compose_cli down
        ;;
    logs)
        ensure_docker
        docker logs -f knowledge_shelf_query
        ;;
    pull-runtime|pull)
        ensure_docker
        docker pull "${RUNTIME_IMAGE}"
        ;;
    *)
        die "用法: bash start.sh {start|restart|update <应用包.bin>|rollback|version|stop|logs|pull-runtime}"
        ;;
esac
