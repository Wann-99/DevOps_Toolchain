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
APP_BIN="$(readlink -f "${APP_BIN}")"
[[ "${SOCKET_DIR}" == /* ]] || SOCKET_DIR="${PWD}/${SOCKET_DIR}"
SOCKET_DIR="$(readlink -m "${SOCKET_DIR}")"
HOST_PYTHON="$(command -v "${HOST_PYTHON}")"
[[ "${HOST_PYTHON}" == /* ]] || HOST_PYTHON="${PWD}/${HOST_PYTHON}"
if [[ "${ACTION}" == start || "${ACTION}" == serve ]]; then
    if [[ "${EUID}" == 0 ]]; then
        install -d -m 700 -o "${HOST_USER}" -g "$(id -gn "${HOST_USER}")" "${SOCKET_DIR}"
    else
        [[ "${HOST_USER}" == "$(id -un)" ]] || { echo "[ERROR] 请以 ${HOST_USER} 账号启动。" >&2; exit 1; }
        mkdir -p "${SOCKET_DIR}"
        chmod 700 "${SOCKET_DIR}"
    fi
fi

run_agent() {
    local command=("${HOST_PYTHON}" -c '
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
' "${APP_BIN}" "$1" "${SOCKET_DIR}/host.sock")
    if [[ "${EUID}" == 0 && "${HOST_USER}" != root ]]; then
        runuser -u "${HOST_USER}" -- "${command[@]}"
    else
        "${command[@]}"
    fi
}

SERVICE="ksq-host-files.service"
UNIT="/etc/systemd/system/${SERVICE}"
if [[ "${ACTION}" == start || "${ACTION}" == stop ]] \
        && command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; then
    privilege=()
    if [[ "${EUID}" != 0 ]]; then
        echo "[INFO] 配置宿主机文件服务需要 sudo 权限；服务仍以 ${HOST_USER} 账号运行。"
        privilege=(sudo)
        sudo -v
    fi
    if [[ "${ACTION}" == stop ]]; then
        [[ ! -f "${UNIT}" ]] || "${privilege[@]}" systemctl disable --now "${SERVICE}"
        run_agent stop
        exit 0
    fi

    unit_file="$(mktemp)"
    trap 'rm -f "${unit_file}"' EXIT
    "${HOST_PYTHON}" - "${HOST_USER}" "${SCRIPT_DIR}/host-files.sh" "${APP_BIN}" "${SOCKET_DIR}" "${HOST_PYTHON}" > "${unit_file}" <<'PY'
import os, pwd, sys
user, script, binary, directory, python = sys.argv[1:]
account = pwd.getpwnam(user)
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r') + '"'
args = ['/bin/bash', script, 'serve', binary, directory]
environment = dict(HOME=account.pw_dir, SHELL=account.pw_shell or '/bin/bash', KSQ_HOST_PYTHON=python)
for name in ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH'):
    if name in os.environ:
        environment[name] = os.environ[name]
print('[Unit]\nDescription=KSQ host files and terminal\nAfter=local-fs.target\nStartLimitIntervalSec=0')
print('RequiresMountsFor=' + ' '.join(quote(path) for path in (script, binary, directory, account.pw_dir)))
print('\n[Service]\nType=simple\nUser=' + str(account.pw_uid))
print('WorkingDirectory=~')
print('Environment=' + ' '.join(quote(key + '=' + value) for key, value in environment.items()))
print('ExecStart=' + ' '.join(quote(arg).replace('$', '$$') for arg in args))
print('Restart=always\nRestartSec=3\nTimeoutStopSec=15\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target')
PY
    # Stop the old service before replacing its account/path; also adopt legacy detached agents.
    [[ ! -f "${UNIT}" ]] || "${privilege[@]}" systemctl stop "${SERVICE}"
    if [[ -f "${SOCKET_DIR}/agent.bin" ]]; then
        run_agent stop
    fi
    "${privilege[@]}" install -m 644 "${unit_file}" "${UNIT}"
    "${privilege[@]}" systemctl daemon-reload
    "${privilege[@]}" systemctl enable "${SERVICE}"
    "${privilege[@]}" systemctl restart "${SERVICE}"
    for attempt in {1..50}; do
        if run_agent status >/dev/null 2>&1; then
            echo "[OK] 宿主机文件服务已启动并启用开机自启（${HOST_USER}）。"
            exit 0
        fi
        sleep 0.2
    done
    echo "[ERROR] 宿主机文件服务未就绪，请检查 ${SOCKET_DIR}/agent.log。" >&2
    exit 1
fi

if [[ "${ACTION}" == start ]]; then
    echo "[WARN] 当前系统没有运行 systemd，仅启动本次连接；无法配置开机自启。" >&2
elif [[ "${ACTION}" == serve ]]; then
    exec >> "${SOCKET_DIR}/agent.log" 2>&1
fi
if [[ -n "${PATH:-}" ]]; then
    export PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
else
    export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
fi
run_agent "${ACTION}"
