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

# 初始账号（与 standalone/start.sh 的 DEFAULT_USERS_JSON 保持一致）
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

# 4. 状态/配置文件：始终写入干净初始值（不携带项目根目录的历史状态）
#    部署包应包含空白状态，设备首次启动时由 start.sh ensure_files 或
#    state_reset.py 处理初始化。
printf '{}\n' > "${STAGE}/config/dashboard_settings.json"
printf '{}\n' > "${STAGE}/config/dashboard_active_order.json"
printf '{}\n' > "${STAGE}/config/test_order_state.json"
printf '%s\n' "${DEFAULT_ORDER_CONFIG_JSON}" > "${STAGE}/config/order_config.json"
printf '%s\n' "${DEFAULT_ORDER_CONFIG_JSON}" > "${STAGE}/config/order_config.prod.json"
printf '%s\n' "${DEFAULT_USERS_JSON}" > "${STAGE}/config/users.json"
cp "${APP_DIR}/ksq/feishu/rules.json" "${STAGE}/config/feishu_rules.json"
echo "  [config] 已写入干净初始值，并携带飞书规则文件"

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
echo "  tar xzf ${PKG_NAME}.tar.gz && cd ${PKG_NAME} && ./start.sh"
echo "后续仅更新源码:"
echo "  bash start.sh update /path/to/knowledge_shelf_query_${TAG}.bin"
