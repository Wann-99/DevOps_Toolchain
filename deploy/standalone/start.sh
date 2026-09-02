#!/usr/bin/env bash
set -euo pipefail

# 固定运行时镜像 + 独立应用包管理脚本。
# 用法:
#   ./start.sh                           部署并启动（默认）
#   ./start.sh start                     部署并启动
#   bash start.sh restart               重建容器并启动
#   bash start.sh update <应用包.bin>   更新应用包并重建容器（自动重置状态）
#   bash start.sh rollback              回滚到上一个应用包
#   bash start.sh version               查看当前应用包版本
#   bash start.sh stop                  停止
#   bash start.sh logs                  查看日志
#   bash start.sh runtime-logs          查看自身服务运行日志文件
#   bash start.sh pull-runtime          仅拉取运行时镜像
#   bash start.sh reset-state           手动重置状态文件（保留 dashboard_settings.json）

CALLER_DIRECTORY="$(pwd)"
cd "$(dirname "${BASH_SOURCE[0]}")"

ACTION="${1:-start}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime:v1.1.1}"
APP_BIN="bin/knowledge_shelf_query.bin"
APP_BIN_BACKUP="${APP_BIN}.bak"
export RUNTIME_IMAGE
export CONFIG_PNP_DIR="${CONFIG_PNP_DIR:-/home/nvidia/compiled/PNPApp_deploy/config_pnp}"
# KNOWLEDGE_DIR is the mounted templates root; the application defaults to its
# ``knowledge`` child and accepts any other child path from the load page.
export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-/home/nvidia/compiled/VfmApp_deploy/model/templates}"

# 兼容旧部署包：旧 .env 可能仍填写 templates/knowledge。新约定挂载
# templates 根目录，检测到旧值时自动提升一级，避免默认路径变成 knowledge/knowledge。
_knowledge_root_candidate="${KNOWLEDGE_DIR%/}"
if [[ "${_knowledge_root_candidate##*/}" == "knowledge" \
    && "$(basename "$(dirname "${_knowledge_root_candidate}")")" == "templates" \
    && -d "$(dirname "${_knowledge_root_candidate}")" \
    && ! -d "${_knowledge_root_candidate}/knowledge" ]]; then
    echo "[WARN] KNOWLEDGE_DIR 使用旧的 templates/knowledge 路径，已改用其 templates 父目录"
    export KNOWLEDGE_DIR="$(dirname "${_knowledge_root_candidate}")"
fi

die() { echo "[ERROR] $*" >&2; exit 1; }

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

# 初始账号（明文密码首次登录后自动迁移为加盐哈希；admin=管理员，user=普通用户只读）
DEFAULT_USERS_JSON='{
  "users": [
    {
      "username": "admin",
      "display_name": "管理员",
      "role": "admin",
      "password": "noematrix"
    },
    {
      "username": "nvidia",
      "display_name": "普通用户",
      "role": "viewer",
      "password": "nvidia"
    }
  ]
}'

# 仅在文件不存在时写入初始值
_init_file() {
    local path="$1"
    local content="$2"
    if [[ -d "${path}" ]]; then
        die "${path} 被误创建为目录，请删除后重试（应为 JSON 文件）"
    fi
    [[ -f "${path}" ]] || printf '%s\n' "${content}" > "${path}"
}

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
    mkdir -p config bin logs
    # 各文件写入正确的初始值（而非统一 {}）
    # test_order_state.json → {}（_load_state_file 缺失/空回退 _empty_state）
    _init_file "config/test_order_state.json" '{}'
    # dashboard_active_order.json → {}（_ensure_active_order_loaded 视为无活跃订单）
    _init_file "config/dashboard_active_order.json" '{}'
    # order_config.json / order_config.prod.json → DEFAULT_ORDER_CONFIG 序列化
    _init_file "config/order_config.json" "${DEFAULT_ORDER_CONFIG_JSON}"
    _init_file "config/order_config.prod.json" "${DEFAULT_ORDER_CONFIG_JSON}"
    # dashboard_settings.json → {}（仅首次创建，不在 reset 范围）
    _init_file "config/dashboard_settings.json" '{}'
    # users.json → 初始账号（仅首次创建，不随 reset 重置，密码登录后自动哈希化）
    _init_file "config/users.json" "${DEFAULT_USERS_JSON}"
    if [[ -d robot_keyboard.env ]]; then
        die "robot_keyboard.env 被误创建为目录，请删除后重试"
    fi
    [[ -f robot_keyboard.env ]] || touch robot_keyboard.env
}

ensure_feishu_rules_file() {
    local target="config/feishu_rules.json"
    [[ -d "${target}" ]] && die "${target} 被误创建为目录，请删除后重试（应为 JSON 文件）"
    [[ -f "${target}" ]] && return 0
    [[ -f "${APP_BIN}" ]] || return 0
    if python3 - "${APP_BIN}" "${target}" <<'PY'
import sys
import zipfile
from pathlib import Path

archive_path = Path(sys.argv[1])
target = Path(sys.argv[2])
try:
    with zipfile.ZipFile(archive_path) as archive:
        payload = archive.read("ksq/feishu/rules.json")
except (KeyError, OSError, zipfile.BadZipFile):
    raise SystemExit(2)
target.write_bytes(payload)
PY
    then
        echo "[INFO] 已从应用包初始化 config/feishu_rules.json"
    else
        echo "[WARN] 未找到内置飞书规则文件，当前应用可能不支持外部规则配置"
    fi
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

ensure_paths() {
    local missing=0
    [[ -d "${CONFIG_PNP_DIR}" ]] || { echo "[ERROR] CONFIG_PNP_DIR 不存在: ${CONFIG_PNP_DIR}"; missing=1; }
    [[ -d "${KNOWLEDGE_DIR}" ]] || { echo "[ERROR] KNOWLEDGE_DIR 不存在: ${KNOWLEDGE_DIR}"; missing=1; }
    if [[ "${missing}" -ne 0 ]]; then
        die "请设置: CONFIG_PNP_DIR=... KNOWLEDGE_DIR=... bash start.sh ${ACTION}"
    fi
    if ! compgen -G "${CONFIG_PNP_DIR}/sku-shelves*.csv" >/dev/null \
        && ! compgen -G "${CONFIG_PNP_DIR}/etm_sku_locations_cache*.csv" >/dev/null; then
        echo "[WARN] ${CONFIG_PNP_DIR} 中未找到库位表，查询功能将不可用"
    fi
    local default_knowledge="${KNOWLEDGE_DIR}/knowledge"
    if [[ ! -d "${default_knowledge}" ]]; then
        echo "[WARN] 默认 knowledge 目录不存在: ${default_knowledge}；可在页面填写 ${KNOWLEDGE_DIR} 下的其他目录"
    elif ! compgen -G "${default_knowledge}/*.json" >/dev/null; then
        echo "[WARN] ${default_knowledge} 中未找到 knowledge JSON，请确认目录内容"
    fi
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
    "ksq/config_pnp.py",
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
        if b"--config-pnp" not in archive.read("ksq/cli.py"):
            raise ValueError("应用包不支持 --config-pnp，不能使用当前挂载配置")
        if b"--knowledge-root" not in archive.read("ksq/cli.py"):
            raise ValueError("应用包不支持 --knowledge-root，不能使用当前 Knowledge 根目录挂载")
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
    if docker container inspect knowledge_shelf_query >/dev/null 2>&1; then
        echo "[INFO] 检测到旧容器 knowledge_shelf_query，正在停止并替换"
        docker stop knowledge_shelf_query >/dev/null
        docker rm knowledge_shelf_query >/dev/null
    fi
    compose_cli up -d
}

wait_for_service() {
    local attempt
    for attempt in $(seq 1 90); do
        if docker exec knowledge_shelf_query python3 -c \
            'import urllib.request; response = urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2); raise SystemExit(0 if response.status == 200 else 1)' \
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
    ensure_feishu_rules_file

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
    ensure_feishu_rules_file
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
        ensure_feishu_rules_file
        ensure_runtime_image
        restart_container
        wait_for_service || die "容器已创建，但服务未就绪，请执行 bash start.sh logs"
        show_version
        echo "[OK] 已启动，访问 http://<本机IP>:8765"
        ;;
    restart)
        ensure_docker
        ensure_files
        ensure_paths
        verify_app_bin "${APP_BIN}"
        ensure_feishu_rules_file
        ensure_runtime_image
        restart_container
        wait_for_service || die "容器已重建，但服务未就绪，请执行 bash start.sh logs"
        ;;
    update)
        ensure_docker
        ensure_paths
        ensure_runtime_image
        reset_state
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
    runtime-logs)
        ensure_files
        touch logs/knowledge_shelf_query.log
        tail -n 200 -f logs/knowledge_shelf_query.log
        ;;
    reset-state)
        reset_state
        ;;
    pull-runtime|pull)
        ensure_docker
        docker pull "${RUNTIME_IMAGE}"
        ;;
    *)
        die "用法: bash start.sh {start|restart|update <应用包.bin>|rollback|version|stop|logs|runtime-logs|pull-runtime|reset-state}"
        ;;
esac
