"""登录认证与角色权限：用户存储、会话管理与登录校验。

角色分为 admin（管理员，可执行全部操作）与 viewer（普通用户，只读，
不可执行编辑类操作）。用户存储在 users.json 中；为便于现场直接编辑，
明文密码会在首次读取时自动迁移为 PBKDF2 加盐哈希并回写文件。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Dict, List, Optional

from ksq.constants import USERS_FILE
from ksq.safe_io import safe_write_text

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLE_LABELS = {ROLE_ADMIN: "管理员", ROLE_VIEWER: "普通用户"}

SESSION_COOKIE = "ksq_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
_PBKDF2_ITERATIONS = 60000

# 普通用户仅禁止「编辑类」操作：库位编辑保存、导入；设置配置保存由 PUT 拦截。
# （工作模式切换由 handlers 单独做字段过滤放行；获取 Token 属下单运行时凭据
# 刷新、不改配置，放行）；其余操作一律正常。
VIEWER_FORBIDDEN_POST_PATHS = frozenset(
    {
        "/api/edit/save",
        "/api/edit/persist",
        "/api/import",
    }
)

_LOCK = threading.Lock()
_SESSIONS: Dict[str, Dict[str, object]] = {}

_DEFAULT_USERS: List[Dict[str, str]] = [
    {
        "username": "admin",
        "display_name": "管理员",
        "role": ROLE_ADMIN,
        "password": "noematrix",
    },
    {
        "username": "nvidia",
        "display_name": "普通用户",
        "role": ROLE_VIEWER,
        "password": "nvidia",
    },
]


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        _PBKDF2_ITERATIONS,
    ).hex()


def _normalise_entry(raw: object) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    username = str(raw.get("username") or "").strip()
    if not username:
        return None
    role = str(raw.get("role") or "").strip()
    if role not in ROLE_LABELS:
        role = ROLE_VIEWER
    entry = {
        "username": username,
        "display_name": str(raw.get("display_name") or username),
        "role": role,
    }
    plaintext = raw.get("password")
    if isinstance(plaintext, str) and plaintext:
        entry["password"] = plaintext
        return entry
    salt = str(raw.get("salt") or "")
    password_hash = str(raw.get("password_hash") or "")
    if salt and password_hash:
        entry["salt"] = salt
        entry["password_hash"] = password_hash
        return entry
    return None


def _write_users(entries: List[Dict[str, str]]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 注意：users.json 以 bind mount 单文件挂载进容器，不能 tmp+rename 覆盖
    # （挂载点 rename 会报 EBUSY）；safe_write_text 为原地截断写入。
    safe_write_text(
        USERS_FILE,
        json.dumps({"users": entries}, ensure_ascii=False, indent=2) + "\n",
        backup=False,
    )


def load_users() -> List[Dict[str, str]]:
    """读取用户列表；文件缺失/损坏时重建默认账号，明文密码自动迁移为哈希。"""
    with _LOCK:
        if not USERS_FILE.is_file():
            _write_users([dict(item) for item in _DEFAULT_USERS])
        try:
            payload = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            backup = USERS_FILE.with_name(
                USERS_FILE.name + ".broken-" + time.strftime("%Y%m%d%H%M%S")
            )
            try:
                backup.write_text(
                    USERS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except OSError:
                pass
            _write_users([dict(item) for item in _DEFAULT_USERS])
            payload = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        raw_users = payload.get("users") if isinstance(payload, dict) else None
        entries: List[Dict[str, str]] = []
        if isinstance(raw_users, list):
            for raw in raw_users:
                entry = _normalise_entry(raw)
                if entry is not None:
                    entries.append(entry)
        changed = False
        for entry in entries:
            plaintext = entry.pop("password", None)
            if plaintext:
                entry["salt"] = secrets.token_hex(16)
                entry["password_hash"] = _hash_password(plaintext, entry["salt"])
                changed = True
        if changed:
            _write_users(entries)
        return entries


def verify_credentials(username: str, password: str) -> Optional[Dict[str, str]]:
    username = username.strip()
    for entry in load_users():
        if entry["username"] != username:
            continue
        candidate = _hash_password(password, entry["salt"])
        if hmac.compare_digest(candidate, entry["password_hash"]):
            return dict(entry)
        return None
    return None


def create_session(entry: Dict[str, str]) -> str:
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {
            "username": entry["username"],
            "display_name": entry.get("display_name") or entry["username"],
            "role": entry.get("role") or ROLE_VIEWER,
            "expires": time.time() + SESSION_TTL_SECONDS,
        }
    return token


def get_session(token: str) -> Optional[Dict[str, object]]:
    if not token:
        return None
    with _LOCK:
        session = _SESSIONS.get(token)
        if session is None:
            return None
        now = time.time()
        if float(session["expires"]) < now:
            _SESSIONS.pop(token, None)
            return None
        # 滑动续期：活跃会话不会因固定时长掉线。
        session["expires"] = now + SESSION_TTL_SECONDS
        return dict(session)


def destroy_session(token: str) -> None:
    if not token:
        return
    with _LOCK:
        _SESSIONS.pop(token, None)


def token_from_cookie(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return ""


def session_from_cookie(header: str) -> Optional[Dict[str, object]]:
    return get_session(token_from_cookie(header))


def session_cookie_header(token: str) -> str:
    return (
        f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
        f"Max-Age={SESSION_TTL_SECONDS}"
    )


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def public_user(session: Dict[str, object]) -> Dict[str, object]:
    role = str(session.get("role") or ROLE_VIEWER)
    return {
        "username": str(session.get("username") or ""),
        "display_name": str(session.get("display_name") or ""),
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
    }
