#!/usr/bin/env bash
# Runs on the host, under the account that invoked deployment (before sudo).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-status}"
APP_BIN="${2:-${SCRIPT_DIR}/bin/knowledge_shelf_query.bin}"
SOCKET_DIR="${3:-${SCRIPT_DIR}/host-files}"
HOST_USER="${KSQ_HOST_FILES_USER:-${SUDO_USER:-$(id -un)}}"
HOST_PYTHON="${KSQ_HOST_PYTHON:-python3}"

if [[ "${ACTION}" == install-desktop ]]; then
    command -v apt-get >/dev/null || { echo "[ERROR] 自动安装仅支持 Debian/Ubuntu。" >&2; exit 1; }
    privilege=()
    [[ "${EUID}" == 0 ]] || privilege=(sudo)
    "${privilege[@]}" apt-get update
    "${privilege[@]}" apt-get install -y --no-install-recommends tigervnc-standalone-server novnc websockify openbox tint2 xauth dbus-x11 fonts-dejavu-core x11-xserver-utils
    echo "图形组件已安装。请执行 bash start.sh restart，再重新连接网页终端。"
    exit 0
fi

[[ -f "${APP_BIN}" ]] || { echo "[ERROR] 应用包不存在: ${APP_BIN}" >&2; exit 1; }
[[ "${SOCKET_DIR}" == /* ]] || SOCKET_DIR="${PWD}/${SOCKET_DIR}"
if [[ "${ACTION}" == start || "${ACTION}" == serve ]]; then
    if [[ "${EUID}" == 0 ]]; then
        install -d -m 700 -o "${HOST_USER}" -g "$(id -gn "${HOST_USER}")" "${SOCKET_DIR}"
    else
        [[ "${HOST_USER}" == "$(id -un)" ]] || { echo "[ERROR] 请以 ${HOST_USER} 账号启动。" >&2; exit 1; }
        mkdir -p "${SOCKET_DIR}"
        chmod 700 "${SOCKET_DIR}"
    fi
fi

command=("${HOST_PYTHON}" -c '
import shutil, sys, zipfile
from pathlib import Path
binary, action, socket = sys.argv[1:]
cache = Path(socket).with_name("agent.bin")
if action in {"start", "serve"}:
    with zipfile.ZipFile(binary) as archive:
        if "ksq/web/host_files.py" in archive.namelist():
            incoming = cache.with_suffix(".incoming")
            shutil.copyfile(binary, incoming)
            incoming.chmod(0o600)
            incoming.replace(cache)
if not cache.is_file():
    raise SystemExit("应用包不支持宿主机连接，请先更新完整部署包。")
sys.path.insert(0, str(cache))
sys.argv = [str(cache), action, "--socket", socket]
from ksq.web.host_files import main
main()
' "${APP_BIN}" "${ACTION}" "${SOCKET_DIR}/host.sock")
if [[ "${EUID}" == 0 && "${HOST_USER}" != root ]]; then
    exec runuser -u "${HOST_USER}" -- "${command[@]}"
fi
exec "${command[@]}"
