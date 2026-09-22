"""Dashboard settings validation and atomic file persistence."""

from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse
import json
import re

from ksq.constants import DASHBOARD_SETTINGS_FILE, DEFAULT_ETM_BASE_URL, ROBOT_KEYBOARD_ENV_FILE
from ksq.feishu.rules import normalize_rule_id, public_rules
from ksq.runtime_logging import get_logger
from ksq.safe_io import safe_write_text


LOGGER = get_logger("dashboard")


_DEFAULT_KEYBOARD_DEVICE = "/dev/input/event1"


_DEFAULT_DASHBOARD_MODE = "test"


_KEYBOARD_DEVICE_RE = re.compile(r"^/dev/input/event\d+$")


_DASHBOARD_MODES = frozenset({"test", "prod"})


_SETTINGS_LOCK = Lock()


_SETTINGS_BACKUP_KEEP_DAYS = 2


def resolve_dashboard_mode(mode: object) -> str:
    value = str(mode or "").strip().lower()
    if value in {"test", "prod"}:
        return value
    settings_mode = str(load_dashboard_settings().get("mode") or _DEFAULT_DASHBOARD_MODE)
    return "prod" if settings_mode == "prod" else "test"


def _normalize_dashboard_mode(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in _DASHBOARD_MODES:
        raise ValueError("mode 仅支持 test 或 prod。")
    return value


def _normalize_etm_base_url(raw: object) -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return DEFAULT_ETM_BASE_URL
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("etm_base_url 必须以 http:// 或 https:// 开头。")
    return value


def _normalize_keyboard_device(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return _DEFAULT_KEYBOARD_DEVICE
    if not _KEYBOARD_DEVICE_RE.match(value):
        raise ValueError(
            "keyboard_device 格式无效，应为 /dev/input/eventN。"
        )
    return value


def _normalize_bool(raw: object, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{field} 必须是布尔值。")
    return raw


def _default_feishu_settings() -> Dict[str, object]:
    return {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "forms": [],
        "selected_form": "",
        "ai": {
            "enabled": False,
            "endpoint": "",
            "api_key": "",
            "model": "gpt-4o-mini",
            "max_tokens": 180,
        },
    }


def _normalize_feishu_ai(
    raw: object, previous: object = None, strict: bool = False
) -> Dict[str, object]:
    current = {
        "enabled": False,
        "endpoint": "",
        "api_key": "",
        "model": "gpt-4o-mini",
        "max_tokens": 180,
    }
    if isinstance(previous, dict):
        if isinstance(previous.get("enabled"), bool):
            current["enabled"] = previous["enabled"]
        for key in ("endpoint", "model"):
            if key in previous:
                current[key] = str(previous.get(key) or "").strip()
        if str(previous.get("api_key") or "").strip():
            current["api_key"] = str(previous.get("api_key") or "").strip()
        try:
            current["max_tokens"] = max(
                64, min(1000, int(previous.get("max_tokens") or 180))
            )
        except (TypeError, ValueError):
            pass
    if not isinstance(raw, dict):
        if strict and raw is not None:
            raise ValueError("feishu.ai 必须是对象。")
        return current
    if "enabled" in raw:
        if strict:
            current["enabled"] = _normalize_bool(
                raw.get("enabled"), "feishu.ai.enabled"
            )
        elif isinstance(raw.get("enabled"), bool):
            current["enabled"] = raw["enabled"]
    for key in ("endpoint", "model"):
        if key in raw:
            current[key] = str(raw.get(key) or "").strip()
    if "api_key" in raw:
        value = str(raw.get("api_key") or "").strip()
        if value:
            current["api_key"] = value
    if "max_tokens" in raw:
        try:
            current["max_tokens"] = max(64, min(1000, int(raw.get("max_tokens") or 180)))
        except (TypeError, ValueError):
            pass
    if not str(current.get("model") or "").strip():
        current["model"] = "gpt-4o-mini"
    return current


def _parse_feishu_link(raw: object) -> Tuple[str, str]:
    """Extract the Bitable app/table identifiers from a pasted Feishu URL."""
    value = str(raw or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        base_index = parts.index("base")
    except ValueError:
        base_index = -1
    app_token = parts[base_index + 1].strip() if base_index >= 0 and len(parts) > base_index + 1 else ""
    table_id = (parse_qs(parsed.query, keep_blank_values=True).get("table") or [""])[0].strip()
    if not app_token or not table_id:
        raise ValueError("飞书多维表格链接无效，应包含 /base/{app_token}?table={table_id}。")
    return app_token, table_id


def _canonical_feishu_link(app_token: str, table_id: str) -> str:
    return "https://feishu.cn/base/%s?table=%s" % (
        quote(app_token, safe=""),
        quote(table_id, safe=""),
    )


def _normalize_feishu_forms(raw: object, strict: bool = False) -> List[Dict[str, str]]:
    """Configured forms: a pasted Feishu link plus one registered payload rule."""
    forms: List[Dict[str, str]] = []
    seen = set()
    seen_names = set()
    if strict and not isinstance(raw, list):
        raise ValueError("飞书 forms 必须是数组。")
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            if strict:
                raise ValueError("飞书表单配置格式无效。")
            continue
        link = str(entry.get("url") or "").strip()
        app_token = ""
        table_id = ""
        if link:
            try:
                app_token, table_id = _parse_feishu_link(link)
            except ValueError:
                if strict:
                    raise
                continue
        else:
            # Keep old settings readable; newly saved settings use url.
            app_token = str(entry.get("app_token") or "").strip()
            table_id = str(entry.get("table_id") or "").strip()
            if app_token and table_id:
                link = _canonical_feishu_link(app_token, table_id)
        name = str(entry.get("name") or "").strip()
        form_id = str(entry.get("id") or "").strip() or name
        if not form_id or not name or not app_token or not table_id:
            if strict:
                raise ValueError("请完整填写表单名称和飞书多维表格链接。")
            continue
        if form_id in seen or name in seen_names:
            if strict:
                raise ValueError("飞书表单名称或 ID 重复：%s" % name)
            continue
        seen.add(form_id)
        seen_names.add(name)
        forms.append(
            {
                "id": form_id,
                "name": name,
                "url": link,
                "app_token": app_token,
                "table_id": table_id,
                "rule": normalize_rule_id(entry.get("rule")),
            }
        )
    return forms


def _normalize_feishu_settings(
    raw: object, previous: Optional[Dict[str, object]], strict: bool = False
) -> Dict[str, object]:
    current = _default_feishu_settings()
    if isinstance(previous, dict):
        if isinstance(previous.get("enabled"), bool):
            current["enabled"] = previous["enabled"]
        current["app_id"] = str(previous.get("app_id") or "").strip()
        current["app_secret"] = str(previous.get("app_secret") or "").strip()
        current["forms"] = _normalize_feishu_forms(previous.get("forms"))
        current["selected_form"] = str(previous.get("selected_form") or "").strip()
        current["ai"] = _normalize_feishu_ai(previous.get("ai"))
    if not isinstance(raw, dict):
        if strict and raw is not None:
            raise ValueError("feishu 必须是对象。")
        current["forms"] = _normalize_feishu_forms(current.get("forms"))
        current["ai"] = _normalize_feishu_ai(None, current.get("ai"))
        return current
    if "enabled" in raw:
        if strict:
            current["enabled"] = _normalize_bool(raw.get("enabled"), "feishu.enabled")
        elif isinstance(raw.get("enabled"), bool):
            current["enabled"] = raw["enabled"]
    if "app_id" in raw:
        current["app_id"] = str(raw.get("app_id") or "").strip()
    if "app_secret" in raw:
        secret = str(raw.get("app_secret") or "").strip()
        if secret:
            current["app_secret"] = secret
    if "forms" in raw:
        forms = _normalize_feishu_forms(raw.get("forms"), strict)
    else:
        forms = _normalize_feishu_forms(current.get("forms"))
    current["forms"] = forms

    if "selected_form" in raw:
        current["selected_form"] = str(raw.get("selected_form") or "").strip()
    if strict and "ai" in raw and raw.get("ai") is not None and not isinstance(raw.get("ai"), dict):
        raise ValueError("feishu.ai 必须是对象。")
    current["ai"] = _normalize_feishu_ai(
        raw.get("ai"), current.get("ai"), strict=strict
    )
    known = {str(form.get("id") or "") for form in current["forms"]}
    if str(current.get("selected_form") or "") not in known:
        current["selected_form"] = str(current["forms"][0]["id"]) if current["forms"] else ""
    if strict and current["enabled"]:
        if not str(current.get("app_id") or "").strip() or not str(current.get("app_secret") or "").strip():
            raise ValueError("启用飞书表单前请配置 App ID 和 App Secret。")
        if not current["forms"]:
            raise ValueError("启用飞书表单前请新增并选择一个表单。")
    return current


def _public_feishu_settings(feishu: Mapping[str, object]) -> Dict[str, object]:
    ai = feishu.get("ai") if isinstance(feishu.get("ai"), Mapping) else {}
    return {
        "enabled": bool(feishu.get("enabled")),
        "app_id": str(feishu.get("app_id") or ""),
        "has_app_secret": bool(str(feishu.get("app_secret") or "").strip()),
        "forms": deepcopy(feishu.get("forms") or []),
        "selected_form": str(feishu.get("selected_form") or ""),
        "form_rules": public_rules(),
        "ai": {
            "enabled": bool(ai.get("enabled")),
            "endpoint": str(ai.get("endpoint") or ""),
            "model": str(ai.get("model") or "gpt-4o-mini"),
            "max_tokens": int(ai.get("max_tokens") or 180),
            "has_api_key": bool(str(ai.get("api_key") or "").strip()),
        },
    }


def load_dashboard_settings() -> Dict[str, object]:
    settings: Dict[str, object] = {
        "keyboard_device": _DEFAULT_KEYBOARD_DEVICE,
        "mode": _DEFAULT_DASHBOARD_MODE,
        "etm_base_url": DEFAULT_ETM_BASE_URL,
        "auto_confirm": False,
        "feishu": _default_feishu_settings(),
    }
    path = DASHBOARD_SETTINGS_FILE
    payload: object = {}
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            # A damaged settings file must not take down every dashboard/status
            # endpoint.  Defaults keep the UI usable while the log identifies
            # the repair target.
            LOGGER.warning("读取仪表板配置失败，使用默认值 path=%s error=%s", path, error)
        if isinstance(payload, dict):
            try:
                settings["keyboard_device"] = _normalize_keyboard_device(
                    payload.get("keyboard_device")
                )
            except ValueError:
                settings["keyboard_device"] = _DEFAULT_KEYBOARD_DEVICE
            try:
                settings["mode"] = _normalize_dashboard_mode(
                    payload.get("mode") or _DEFAULT_DASHBOARD_MODE
                )
            except ValueError:
                settings["mode"] = _DEFAULT_DASHBOARD_MODE
            try:
                settings["etm_base_url"] = _normalize_etm_base_url(
                    payload.get("etm_base_url")
                )
            except ValueError:
                settings["etm_base_url"] = DEFAULT_ETM_BASE_URL
            if isinstance(payload.get("auto_confirm"), bool):
                settings["auto_confirm"] = payload["auto_confirm"]
            settings["feishu"] = _normalize_feishu_settings(
                payload.get("feishu"), None
            )
    # Prefer mounted robot env file when present.
    env_path = ROBOT_KEYBOARD_ENV_FILE
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text or text.startswith("#") or "=" not in text:
                    continue
                key, value = text.split("=", 1)
                if key.strip() == "PNP_KEYBOARD_DEVICE":
                    settings["keyboard_device"] = _normalize_keyboard_device(
                        value.strip().strip('"').strip("'")
                    )
                    break
        except (OSError, ValueError):
            pass
    return settings


def _write_robot_keyboard_env(device: str) -> bool:
    env_path = ROBOT_KEYBOARD_ENV_FILE
    try:
        safe_write_text(
            env_path,
            f"PNP_KEYBOARD_DEVICE={device}\n",
            keep_days=_SETTINGS_BACKUP_KEEP_DAYS,
        )
        return True
    except OSError:
        return False


def update_settings(payload: Dict[str, object]) -> Tuple[Dict[str, object], bool, str]:
    """Serialize settings validation and persistence; caller owns device actions."""
    with _SETTINGS_LOCK:
        current = load_dashboard_settings()
        previous_mode = str(current.get("mode") or _DEFAULT_DASHBOARD_MODE)
        if "keyboard_device" in payload:
            current["keyboard_device"] = _normalize_keyboard_device(
                payload.get("keyboard_device")
            )
        if "mode" in payload:
            current["mode"] = _normalize_dashboard_mode(payload.get("mode"))
        if "etm_base_url" in payload:
            current["etm_base_url"] = _normalize_etm_base_url(
                payload.get("etm_base_url")
            )
        if "auto_confirm" in payload:
            current["auto_confirm"] = _normalize_bool(
                payload.get("auto_confirm"), "auto_confirm"
            )
        if "feishu" in payload:
            previous_feishu = current.get("feishu")
            if not isinstance(previous_feishu, dict):
                previous_feishu = _default_feishu_settings()
            current["feishu"] = _normalize_feishu_settings(
                payload.get("feishu"), previous_feishu, strict=True
            )
        feishu_settings = current.get("feishu")
        if not isinstance(feishu_settings, dict):
            feishu_settings = _default_feishu_settings()
        settings = {
            "keyboard_device": current["keyboard_device"],
            "mode": current["mode"],
            "etm_base_url": current["etm_base_url"],
            "auto_confirm": bool(current.get("auto_confirm")),
            "feishu": feishu_settings,
        }
        # Preserve the internal version marker used by state_reset.py so
        # that a user-initiated settings save does not wipe it and cause a
        # spurious reset on the next restart.
        try:
            if DASHBOARD_SETTINGS_FILE.is_file():
                _existing_settings = json.loads(
                    DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8")
                )
                if isinstance(_existing_settings, dict) and (
                    "_app_version_marker" in _existing_settings
                ):
                    settings["_app_version_marker"] = _existing_settings[
                        "_app_version_marker"
                    ]
        except (OSError, json.JSONDecodeError):
            pass
        safe_write_text(
            DASHBOARD_SETTINGS_FILE,
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            keep_days=_SETTINGS_BACKUP_KEEP_DAYS,
        )
        env_written = _write_robot_keyboard_env(str(settings["keyboard_device"]))
    return settings, env_written, previous_mode
