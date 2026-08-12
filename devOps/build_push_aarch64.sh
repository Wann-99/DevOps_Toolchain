#!/usr/bin/env bash
set -euo pipefail

# 构建并推送固定 aarch64 (linux/arm64) 运行时镜像。
# 镜像不包含业务源码，仅运行环境变化时需要执行。
# 依赖: docker buildx + QEMU。若检测到不支持 arm64 会自动尝试注册 QEMU。
# 用法: bash build_push_aarch64.sh [tag]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PLATFORM="linux/arm64"
DOCKER_REGISTRY="${DOCKER_REGISTRY:-hub.noematrix.cn}"
IMAGE_NAME="${IMAGE_NAME:-hub.noematrix.cn/pharmacy/knowledge_shelf_query_runtime}"
TAG="${1:-v1.1.0}"
FULL_IMAGE="${IMAGE_NAME}:${TAG}"

log()  { echo -e "\n\033[32m════════ $* ════════\033[0m"; }
info() { echo -e "\033[36m  [INFO] $*\033[0m"; }
die()  { echo -e "\033[31m  [错误] $*\033[0m" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker buildx version >/dev/null 2>&1 || die "当前 docker 不支持 buildx（需要 Docker 19.03+）"

if ! docker buildx ls 2>/dev/null | grep -q "${PLATFORM}"; then
  info "buildx 暂不支持 ${PLATFORM}，尝试注册 QEMU 模拟器…"
  docker run --privileged --rm tonistiigi/binfmt --install all \
    || die "QEMU 注册失败，请手动执行：docker run --privileged --rm tonistiigi/binfmt --install all"
fi

if [[ -n "${DOCKER_USER:-}" && -n "${DOCKER_PASS:-}" ]]; then
  log "登录 ${DOCKER_REGISTRY}"
  echo "${DOCKER_PASS}" | docker login "${DOCKER_REGISTRY}" -u "${DOCKER_USER}" --password-stdin
else
  info "使用当前 Docker 登录状态（也可设置 DOCKER_USER / DOCKER_PASS）"
fi

log "构建并推送 ${FULL_IMAGE}（${PLATFORM}）"
info "上下文: ${APP_DIR}"
docker buildx build \
  --platform "${PLATFORM}" \
  -t "${FULL_IMAGE}" \
  -t "${IMAGE_NAME}:latest" \
  --push \
  "${APP_DIR}"

echo ""
info "完成: ${FULL_IMAGE}"
info "以及: ${IMAGE_NAME}:latest"
info "架构: ${PLATFORM}"
info "该镜像不包含应用源码；日常源码更新请构建 .bin 应用包"
