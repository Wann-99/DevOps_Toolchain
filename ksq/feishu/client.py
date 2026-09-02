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
    if not record_id:
        # HTTP 200/code=0 only means the request envelope was accepted.  A
        # record id is the durable acknowledgement that the row was created;
        # without it callers must be able to retry instead of persisting a
        # false success.
        raise FeishuApiError(
            "写入飞书多维表格成功响应缺少 record_id。",
            status,
            payload,
        )
    return {
        "ok": True,
        "record_id": record_id,
        "response": payload,
    }
