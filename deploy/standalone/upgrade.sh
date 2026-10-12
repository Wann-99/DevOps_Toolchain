#!/usr/bin/env bash
# Install once on the host; upgrades run in their own service, outside the web/terminal service.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-status}"
APP_BIN="${2:-${SCRIPT_DIR}/bin/knowledge_shelf_query.bin}"
HOST_USER="${3:-${KSQ_HOST_FILES_USER:-${SUDO_USER:-$(id -un)}}}"
HOST_PYTHON="${4:-${KSQ_HOST_PYTHON:-python3}}"
SERVICE="ksq-upgrade.service"
UNIT="/etc/systemd/system/${SERVICE}"

case "${ACTION}" in
  status) systemctl status "${SERVICE}" --no-pager; exit ;;
  stop)
    if [[ "${EUID}" == 0 ]]; then systemctl disable --now "${SERVICE}"
    else sudo systemctl disable --now "${SERVICE}"; fi
    exit ;;
  install) ;;
  *) echo "用法: bash start.sh upgrade {install|status|stop}" >&2; exit 2 ;;
esac
command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || {
  echo "[ERROR] 在线升级需要 systemd；当前环境可继续使用命令行更新。" >&2; exit 1;
}
HOST_PYTHON="$(command -v "${HOST_PYTHON}")"
APP_BIN="$(readlink -f "${APP_BIN}")"
if [[ "${EUID}" != 0 ]]; then
  exec sudo /bin/bash "$0" install "${APP_BIN}" "${HOST_USER}" "${HOST_PYTHON}"
fi
UPGRADE_DIR="${SCRIPT_DIR}/upgrade"
[[ ! -L "${UPGRADE_DIR}" ]] || { echo "[ERROR] upgrade 目录不能是符号链接。" >&2; exit 1; }
install -d -m 700 -o root -g root "${UPGRADE_DIR}"
for name in service.env agent.bin agent.incoming; do
  [[ ! -L "${UPGRADE_DIR}/${name}" ]] || { echo "[ERROR] 升级服务文件不能是符号链接。" >&2; exit 1; }
done
touch "${UPGRADE_DIR}/service.env"
chown root:root "${UPGRADE_DIR}/service.env"
chmod 600 "${UPGRADE_DIR}/service.env"
"${HOST_PYTHON}" - "${UPGRADE_DIR}/service.env" <<'PY'
import json, subprocess, sys
from pathlib import Path
try:
    container = json.loads(subprocess.check_output(['docker', 'inspect', 'knowledge_shelf_query'], timeout=10))[0]
    mounts = {item['Destination']: item['Source'] for item in container['Mounts']}
    environment = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
    values = {'CONFIG_PNP_DIR': mounts['/data/config_pnp'], 'KNOWLEDGE_DIR': mounts['/data/templates'],
              'RUNTIME_IMAGE': container['Config']['Image']}
    values.update({key: environment[key] for key in ('TZ', 'KSQ_DESKTOP_URL') if key in environment})
except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
    raise SystemExit('无法读取当前部署配置，请先执行 bash start.sh start，再安装升级服务。')
path = Path(sys.argv[1])
lines = [line for line in path.read_text().splitlines() if line.split('=', 1)[0] not in values]
lines.extend(key + '=' + json.dumps(value, ensure_ascii=False) for key, value in values.items())
path.write_text('\n'.join(lines) + '\n')
PY
systemctl stop "${SERVICE}" 2>/dev/null || true
"${HOST_PYTHON}" - "${APP_BIN}" "${UPGRADE_DIR}/agent.bin" <<'PY'
import shutil, sys, zipfile
from pathlib import Path
source, target = map(Path, sys.argv[1:])
with zipfile.ZipFile(source) as archive:
    if 'ksq/upgrade.py' not in archive.namelist():
        raise SystemExit('应用包缺少升级服务，请先同步完整部署包。')
incoming = target.with_suffix('.incoming')
shutil.copyfile(source, incoming)
incoming.chmod(0o600)
incoming.replace(target)
PY
"${HOST_PYTHON}" - "${SCRIPT_DIR}" "${UPGRADE_DIR}" "${HOST_USER}" > "${UNIT}" <<'PY'
import pwd, subprocess, sys
deployment, directory, user = sys.argv[1:]
if any(character in deployment for character in '\n\r'):
    raise SystemExit('部署目录不能包含换行符。')
configured = subprocess.check_output(['systemctl', 'show', '-p', 'User', '--value', 'ksq-host-files.service'], timeout=10).decode().strip()
if configured:
    user = (pwd.getpwuid(int(configured)) if configured.isdigit() else pwd.getpwnam(configured)).pw_name
pwd.getpwnam(user)
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r') + '"'
bootstrap = 'import sys;sys.path.insert(0,sys.argv.pop(1));from ksq.upgrade import main;main()'
arguments = [sys.executable, '-c', bootstrap, directory + '/agent.bin', '--directory', directory,
             '--deployment', deployment, '--host-user', user]
print('[Unit]\nDescription=KSQ online application upgrade\nAfter=network-online.target docker.service\nWants=network-online.target')
print('\n[Service]\nType=simple\nUser=root\nUMask=0077\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=15')
# Scalar path settings keep quotation marks as part of the path.
print('WorkingDirectory=' + deployment.replace('%', '%%'))
print('EnvironmentFile=' + (directory + '/service.env').replace('%', '%%'))
print('ExecStart=' + ' '.join(quote(arg).replace('$', '$$') for arg in arguments))
print('\n[Install]\nWantedBy=multi-user.target')
PY
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
for attempt in {1..30}; do
  [[ ! -S "${UPGRADE_DIR}/upgrade.sock" ]] || { echo "[OK] 在线升级服务已安装并启动。"; exit 0; }
  sleep 1
done
echo "[ERROR] 升级服务未就绪，请检查 journalctl -u ${SERVICE}。" >&2
exit 1
