"""Minimal Feishu Open API client for bitable record creation."""

from __future__ import annotations

import json
import mimetypes
import time
import uuid
import urllib.error
import urllib.request
from threading import Lock
from typing import Dict, List, Optional, Tuple


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, status_code: int, body: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


_TOKEN_LOCK = Lock()
_TOKEN_CACHE: Dict[str, object] = {
    "app_id": "",
    "app_secret": "",
    "token": "",
    "expire_at": 0.0,
}


def _request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, object]],
    token: Optional[str],
) -> Tuple[int, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = int(error.code)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = raw
        raise FeishuApiError(
            "飞书请求失败：%s %s → HTTP %s" % (method, url, status),
            status,
            body,
        ) from error
    except urllib.error.URLError as error:
        raise FeishuApiError(
            "无法连接飞书：%s（%s）" % (url, error.reason),
            0,
            {"error": str(error.reason)},
        ) from error

    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _ensure_ok(status: int, body: object, action: str) -> Dict[str, object]:
    if not isinstance(body, dict):
        raise FeishuApiError(
            "%s 响应格式无效。" % action,
            status,
            body,
        )
    code = body.get("code")
    if code not in (None, 0, "0"):
        msg = str(body.get("msg") or body.get("message") or "未知错误").strip()
        raise FeishuApiError(
            "%s 失败：%s（code=%s）" % (action, msg, code),
            status,
            body,
        )
    return body


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    app_id_text = str(app_id or "").strip()
    app_secret_text = str(app_secret or "").strip()
    if not app_id_text or not app_secret_text:
        raise FeishuApiError(
            "飞书 app_id / app_secret 未配置。",
            400,
            {"app_id": app_id_text},
        )

    now = time.time()
    with _TOKEN_LOCK:
        cached_id = str(_TOKEN_CACHE.get("app_id") or "")
        cached_secret = str(_TOKEN_CACHE.get("app_secret") or "")
        cached_token = str(_TOKEN_CACHE.get("token") or "")
        expire_at = float(_TOKEN_CACHE.get("expire_at") or 0.0)
        if (
            cached_id == app_id_text
            and cached_secret == app_secret_text
            and cached_token
            and now < expire_at - 60
        ):
            return cached_token

    status, body = _request_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id_text, "app_secret": app_secret_text},
        None,
    )
    payload = _ensure_ok(status, body, "获取飞书 tenant_access_token")
    token = str(payload.get("tenant_access_token") or "").strip()
    if not token:
        raise FeishuApiError("飞书 token 为空。", status, body)
    expire = payload.get("expire")
    try:
        expire_seconds = float(expire)
    except (TypeError, ValueError):
        expire_seconds = 7200.0
    with _TOKEN_LOCK:
        _TOKEN_CACHE["app_id"] = app_id_text
        _TOKEN_CACHE["app_secret"] = app_secret_text
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expire_at"] = now + expire_seconds
    return token


def _encode_multipart(
    fields: Dict[str, str], files: Dict[str, Tuple[str, bytes, str]]
) -> Tuple[bytes, str]:
    boundary = "----ksqFeishu%s" % uuid.uuid4().hex
    chunks: list = []
    for name, value in fields.items():
        chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
        chunks.append(
            ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode(
                "utf-8"
            )
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, (filename, content, content_type) in files.items():
        chunks.append(("--%s\r\n" % boundary).encode("utf-8"))
        chunks.append(
            (
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (name, filename)
            ).encode("utf-8")
        )
        chunks.append(("Content-Type: %s\r\n\r\n" % content_type).encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(chunks)
    return body, "multipart/form-data; boundary=%s" % boundary


def upload_bitable_media(
    app_id: str,
    app_secret: str,
    app_token: str,
    file_name: str,
    file_bytes: bytes,
) -> str:
    """Upload a file into the bitable and return file_token."""
    app_token_text = str(app_token or "").strip()
    name = str(file_name or "").strip() or "upload.bin"
    if not app_token_text:
        raise FeishuApiError("飞书 app_token 未配置，无法上传附件。", 400, {})
    if not file_bytes:
        raise FeishuApiError("上传附件内容为空。", 400, {"file_name": name})
    token = get_tenant_access_token(app_id, app_secret)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    body, content_type_header = _encode_multipart(
        {
            "file_name": name,
            "parent_type": "bitable_file",
            "parent_node": app_token_text,
            "size": str(len(file_bytes)),
        },
        {"file": (name, file_bytes, content_type)},
    )
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        data=body,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": content_type_header,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = int(error.code)
        try:
            err_body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            err_body = raw
        raise FeishuApiError(
            "上传飞书附件失败：HTTP %s" % status,
            status,
            err_body,
        ) from error
    except urllib.error.URLError as error:
        raise FeishuApiError(
            "无法连接飞书上传接口：%s" % error.reason,
            0,
            {"error": str(error.reason)},
        ) from error
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = raw
    ok = _ensure_ok(status, payload, "上传飞书附件")
    data = ok.get("data")
    if not isinstance(data, dict):
        raise FeishuApiError("上传附件响应缺少 data。", status, payload)
    file_token = str(data.get("file_token") or "").strip()
    if not file_token:
        raise FeishuApiError("上传附件未返回 file_token。", status, payload)
    return file_token


def list_bitable_fields(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
) -> List[Dict[str, object]]:
    """Return the raw field descriptors of a bitable table (name/type/property)."""
    app_token_text = str(app_token or "").strip()
    table_id_text = str(table_id or "").strip()
    if not app_token_text or not table_id_text:
        raise FeishuApiError(
            "飞书 app_token / table_id 未配置，无法读取字段。",
            400,
            {"app_token": app_token_text, "table_id": table_id_text},
        )
    token = get_tenant_access_token(app_id, app_secret)
    url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/fields?page_size=200"
        % (app_token_text, table_id_text)
    )
    status, body = _request_json("GET", url, None, token)
    payload = _ensure_ok(status, body, "读取飞书多维表格字段")
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise FeishuApiError("读取字段响应缺少 items。", status, payload)
    return [item for item in items if isinstance(item, dict)]


def list_bitable_form_fields(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
) -> List[Dict[str, object]]:
    """Return the *form view*'s own field list: {field_id, title, description, required, visible}.

    整表结构（list_bitable_fields）里什么都有 —— 副本列、研发才填的列、只读列。
    表单视图才是测试人员真正看到的那一份：字段范围、顺序、必填、帮助文案都由它定。
    表里没有表单视图就返回 []，调用方回退到整表结构，不是错误。
    """
    app_token_text = str(app_token or "").strip()
    table_id_text = str(table_id or "").strip()
    if not app_token_text or not table_id_text:
        return []
    token = get_tenant_access_token(app_id, app_secret)
    base = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s" % (
        app_token_text,
        table_id_text,
    )
    status, body = _request_json("GET", base + "/views?page_size=100", None, token)
    payload = _ensure_ok(status, body, "读取飞书多维表格视图")
    data = payload.get("data")
    views = data.get("items") if isinstance(data, dict) else None
    form_id = ""
    for view in views if isinstance(views, list) else []:
        if isinstance(view, dict) and str(view.get("view_type") or "") == "form":
            form_id = str(view.get("view_id") or "").strip()
            if form_id:
                break
    if not form_id:
        return []
    status, body = _request_json(
        "GET", "%s/forms/%s/fields?page_size=100" % (base, form_id), None, token
    )
    payload = _ensure_ok(status, body, "读取飞书表单视图字段")
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def list_bitable_records(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
) -> List[Dict[str, object]]:
    """Return {record_id, label} for a table, label = first non-empty text field.

    ponytail: 只取首页 500 条；被关联表超量再加分页。
    """
    app_token_text = str(app_token or "").strip()
    table_id_text = str(table_id or "").strip()
    if not app_token_text or not table_id_text:
        raise FeishuApiError(
            "飞书 app_token / table_id 未配置，无法读取记录。",
            400,
            {"app_token": app_token_text, "table_id": table_id_text},
        )
    token = get_tenant_access_token(app_id, app_secret)
    url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records?page_size=500"
        % (app_token_text, table_id_text)
    )
    status, body = _request_json("GET", url, None, token)
    payload = _ensure_ok(status, body, "读取飞书多维表格记录")
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    # 记录名认主字段（/fields 的第一条）。_record_label 那套「第一个非空字段」会撞上
    # 自动编号/公式列，读回来是个 0，下拉框里看不出是哪条，同名匹配也会失手。
    try:
        table_fields = list_bitable_fields(app_id, app_secret, app_token, table_id_text)
        primary = str((table_fields or [{}])[0].get("field_name") or "")
    except FeishuApiError:
        primary = ""
    records: List[Dict[str, object]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or "").strip()
        if not record_id:
            continue
        fields = item.get("fields")
        label = ""
        if primary and isinstance(fields, dict):
            label = _flatten_text(fields.get(primary))
        records.append(
            {"record_id": record_id, "label": label or _record_label(fields) or record_id}
        )
    return records


def _record_label(fields: object) -> str:
    """Best-effort primary-field text of a bitable record."""
    if not isinstance(fields, dict):
        return ""
    for value in fields.values():
        text = _flatten_text(value)
        if text:
            return text
    return ""


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    if isinstance(value, list):
        parts = [_flatten_text(entry) for entry in value]
        return " ".join(part for part in parts if part).strip()
    return ""


def list_bitable_select_options(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
    field_name: str,
) -> List[str]:
    """Return single-select option names for a bitable field."""
    field_name_text = str(field_name or "").strip()
    if not field_name_text:
        raise FeishuApiError("字段名为空，无法读取选项。", 400, {})
    items = list_bitable_fields(app_id, app_secret, app_token, table_id)
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("field_name") or "").strip() != field_name_text:
            continue
        property_data = item.get("property")
        raw_options = (
            property_data.get("options") if isinstance(property_data, dict) else None
        )
        if not isinstance(raw_options, list):
            raise FeishuApiError(
                "字段「%s」不是单选或没有选项。" % field_name_text,
                400,
                item,
            )
        names: List[str] = []
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            name = str(option.get("name") or "").strip()
            if name:
                names.append(name)
        if not names:
            raise FeishuApiError(
                "字段「%s」选项为空。" % field_name_text,
                400,
                item,
            )
        return names
    raise FeishuApiError(
        "未找到字段「%s」。" % field_name_text,
        404,
        {"field_name": field_name_text},
    )


def create_bitable_record(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
    fields: Dict[str, object],
) -> Dict[str, object]:
    app_token_text = str(app_token or "").strip()
    table_id_text = str(table_id or "").strip()
    if not app_token_text or not table_id_text:
        raise FeishuApiError(
            "飞书 app_token / table_id 未配置。",
            400,
            {"app_token": app_token_text, "table_id": table_id_text},
        )
    token = get_tenant_access_token(app_id, app_secret)
    url = (
        "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records"
        % (app_token_text, table_id_text)
    )
    status, body = _request_json(
        "POST",
        url,
        {"fields": fields},
        token,
    )
    payload = _ensure_ok(status, body, "写入飞书多维表格")
    data = payload.get("data")
    record = data.get("record") if isinstance(data, dict) else None
    record_id = ""
    if isinstance(record, dict):
        record_id = str(record.get("record_id") or "")
    return {
        "ok": True,
        "record_id": record_id,
        "response": payload,
    }
