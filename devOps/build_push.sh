#!/usr/bin/env bash
set -euo pipefail

# 构建并推送固定运行时镜像（本机架构）。
# 镜像不包含业务源码，仅运行环境变化时需要执行。
# 用法: bash build_push.sh [tag]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DOCKER_REGISTRY="${DOCKER_REGISTRY:-hub.noematrix.cn}"
IMAGE_NAME="${IMAGE_NAME:-hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime}"
TAG="${1:-v1.1.0}"
FULL_IMAGE="${IMAGE_NAME}:${TAG}"

log()  { echo -e "\n\033[32m════════ $* ════════\033[0m"; }
info() { echo -e "\033[36m  [INFO] $*\033[0m"; }

log "构建 ${FULL_IMAGE}"
info "上下文: ${APP_DIR}"
docker build -t "${FULL_IMAGE}" -t "${IMAGE_NAME}:latest" "${APP_DIR}"

if [[ -n "${DOCKER_USER:-}" && -n "${DOCKER_PASS:-}" ]]; then
  log "登录 ${DOCKER_REGISTRY}"
  echo "${DOCKER_PASS}" | docker login "${DOCKER_REGISTRY}" -u "${DOCKER_USER}" --password-stdin
else
  info "使用当前 Docker 登录状态（也可设置 DOCKER_USER / DOCKER_PASS）"
fi

log "推送 ${FULL_IMAGE}"
docker push "${FULL_IMAGE}"
docker push "${IMAGE_NAME}:latest"

echo ""
info "完成: ${FULL_IMAGE}"
info "以及: ${IMAGE_NAME}:latest"
info "该镜像不包含应用源码；日常源码更新请构建 .bin 应用包"
