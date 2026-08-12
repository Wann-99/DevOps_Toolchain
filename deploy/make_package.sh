#!/usr/bin/env bash
set -euo pipefail

# 生成标准部署包 ksq_deploy_<tag>.tar.gz（固定运行时镜像 + 单文件应用包）
# 业务数据（config_pnp / knowledge）由设备自带，不打入包内。
# 用法:
#   bash make_package.sh <tag>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

TAG="${1:-}"
[[ -n "${TAG}" ]] || { echo "用法: bash make_package.sh <tag>" >&2; exit 1; }

PKG_NAME="ksq_deploy_${TAG}"
DIST_DIR="${SCRIPT_DIR}/dist"
STAGE="${DIST_DIR}/${PKG_NAME}"

echo "════════ 组装部署包 ${PKG_NAME} ════════"
rm -rf "${STAGE}"
mkdir -p "${STAGE}/config" "${STAGE}/bin"

# 1. 启动脚本与 compose
cp "${SCRIPT_DIR}/standalone/start.sh" "${STAGE}/start.sh"
cp "${SCRIPT_DIR}/standalone/docker-compose.yml" "${STAGE}/docker-compose.yml"
chmod +x "${STAGE}/start.sh"

# 2. 构建单文件应用包；该文件也是后续增量更新的唯一交付物
APP_BIN_DIST="${DIST_DIR}/knowledge_shelf_query_${TAG}.bin"
python3 "${SCRIPT_DIR}/build_app_bin.py" "${TAG}" --output "${APP_BIN_DIST}"
cp "${APP_BIN_DIST}" "${STAGE}/bin/knowledge_shelf_query.bin"

# 3. 机器人键盘环境（可选，位于 devOps/ 目录）
[[ -f "${APP_DIR}/devOps/robot_keyboard.env" ]] \
  && cp "${APP_DIR}/devOps/robot_keyboard.env" "${STAGE}/robot_keyboard.env" \
  || touch "${STAGE}/robot_keyboard.env"

# 4. 状态/配置文件：项目根目录有现成的就带上（含已有配置），否则初始化为空模板
for f in dashboard_settings.json dashboard_active_order.json \
         test_order_state.json order_config.json order_config.prod.json; do
    if [[ -f "${APP_DIR}/${f}" ]]; then
        cp "${APP_DIR}/${f}" "${STAGE}/config/${f}"
        echo "  [config] 携带现有配置: ${f}"
    else
        echo '{}' > "${STAGE}/config/${f}"
        echo "  [config] 初始化空模板: ${f}"
    fi
done

# 5. 打包
cd "${DIST_DIR}"
tar czf "${PKG_NAME}.tar.gz" "${PKG_NAME}"
echo ""
echo "════════ 完成 ════════"
echo "部署包: ${DIST_DIR}/${PKG_NAME}.tar.gz"
echo "增量包: ${APP_BIN_DIST}"
du -h "${PKG_NAME}.tar.gz" | awk '{print "大小:   " $1}'
echo ""
echo "目标设备使用:"
echo "  tar xzf ${PKG_NAME}.tar.gz && cd ${PKG_NAME} && bash start.sh"
echo "后续仅更新源码:"
echo "  bash start.sh update /path/to/knowledge_shelf_query_${TAG}.bin"
