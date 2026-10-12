"""Fixed RealMan web/API/WebSocket proxy for authenticated KSQ users."""

import asyncio
import anyio
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
import json
import re
from tempfile import SpooledTemporaryFile
import threading
from urllib.parse import urljoin, urlsplit
from urllib.request import Request as CookieRequest

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ksq.constants import PACKAGE_DIRECTORY
from ksq.runtime_logging import get_logger
from ksq.web import auth
from ksq.web.asgi import ResourceStream


router = APIRouter(tags=["arm"])
LOGGER = get_logger("arm")
ARM_HOST = "192.168.11.18"
HTTP_PORTS = {"/arm/": 80, "/arm-api/": 8090}
MAX_UPLOAD = 256 * 1024 * 1024
MAX_TEXT = 16 * 1024 * 1024
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade"}
_BRIDGE = PACKAGE_DIRECTORY / "web" / "static" / "arm-bridge.js"
_STATE_LOCK = threading.Lock()


def _denial(connection, arm_key=None):
    session = (auth.session_from_arm_token(arm_key) if arm_key is not None
               else auth.session_from_cookie(connection.headers.get("cookie", "")))
    if session is None:
        return HTMLResponse("<p>登录已过期，请重新登录工具后刷新机械臂页面。</p>", status_code=401)
    origin = connection.headers.get("origin")
    expected = str(connection.base_url).rstrip("/").replace("ws://", "http://").replace("wss://", "https://")
    if arm_key is not None:
        if origin not in {None, "null", expected}:
            return JSONResponse({"error": "拒绝跨站机械臂访问。"}, status_code=403)
        connection.scope["arm_session"] = session
        return None
    if connection.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
        return JSONResponse({"error": "拒绝跨站机械臂访问。"}, status_code=403)
    if (connection.scope["type"] == "websocket" or connection.method not in {"GET", "HEAD", "OPTIONS"}) and not origin:
        referer = urlsplit(connection.headers.get("referer", ""))
        if connection.scope["type"] == "websocket" or f"{referer.scheme}://{referer.netloc}" != expected:
            return JSONResponse({"error": "机械臂操作需要同源页面。"}, status_code=403)
    return None


def _cookies(session, upstream):
    request = CookieRequest(upstream)
    with _STATE_LOCK:
        session["arm_cookies"].add_cookie_header(request)
    return request.get_header("Cookie", "")


def _request_headers(request, upstream):
    excluded = _HOP_HEADERS | {"host", "cookie", "content-length", "accept-encoding", "origin", "referer"}
    excluded.update(name.strip().lower() for name in request.headers.get("connection", "").split(","))
    headers = {name: value for name, value in request.headers.items()
               if name.lower() not in excluded and not name.lower().startswith(("x-forwarded-", "sec-fetch-", "x-ksq-"))}
    headers["Accept-Encoding"] = "identity"
    if request.headers.get("origin"):
        headers["Origin"] = f"http://{ARM_HOST}"
    cookies = _cookies(request.scope["arm_session"], upstream)
    if cookies:
        headers["Cookie"] = cookies
    return headers


def _proxy_url(value, upstream, arm_key):
    parsed = urlsplit(urljoin(upstream, value))
    if parsed.hostname != ARM_HOST or parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("机械臂页面返回了不支持的跳转地址。")
    prefix = {80: "/arm", 8090: "/arm-api"}.get(parsed.port or 80)
    if prefix is None:
        raise ValueError("机械臂页面返回了不支持的端口。")
    return prefix + "/" + arm_key + (parsed.path or "/") + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")


def _browser_headers(request, arm_key):
    origin = str(request.base_url).rstrip("/")
    web, api = (origin + prefix + arm_key + "/" for prefix in ("/arm/", "/arm-api/"))
    ws = origin.replace("http", "ws", 1) + "/arm-ws/" + arm_key + "/"
    return {
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Allow-Origin": "null",
        "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "*, Authorization",
        "Content-Security-Policy": (
            "sandbox allow-scripts allow-forms allow-downloads allow-popups; "
            "default-src 'none'; "
            f"script-src {web} 'unsafe-inline' 'unsafe-eval' blob:; "
            f"style-src {web} 'unsafe-inline'; img-src {web} data: blob:; "
            f"font-src {web} data:; connect-src {web} {api} {ws}; "
            f"form-action {web} {api}; base-uri {web}; frame-ancestors 'self'"
        ),
    }


def _response_headers(response, upstream, rewritten, request, arm_key):
    excluded = _HOP_HEADERS | {"set-cookie", "location", "cache-control", "content-length",
                              "content-security-policy", "content-security-policy-report-only",
                              "x-frame-options", "referrer-policy", "x-content-type-options"}
    excluded.update(name.strip().lower() for name in (response.getheader("Connection") or "").split(","))
    if rewritten:
        excluded |= {"etag", "last-modified", "content-encoding", "content-md5"}
    headers = [(name.lower().encode("latin-1"), value.encode("latin-1"))
               for name, value in response.getheaders()
               if name.lower() not in excluded and not name.lower().startswith("access-control-")]
    headers.extend((name.lower().encode(), value.encode())
                   for name, value in _browser_headers(request, arm_key).items())
    location = response.getheader("Location")
    if location:
        headers.append((b"location", _proxy_url(location, upstream, arm_key).encode("latin-1")))
    with _STATE_LOCK:
        jar = request.scope["arm_session"]["arm_cookies"]
        jar.extract_cookies(response, CookieRequest(upstream))
        if len(jar) > 32 or sum(len(c.name) + len(c.value) for c in jar) > 64 * 1024:
            jar.clear()
            raise ValueError("机械臂登录状态过大。")
    return headers


def _rewrite_text(content, content_type, arm_key):
    text = content.decode("utf-8")
    # This firmware derives its API host from the URL and assumes port 80.
    # Keep the device identity intact: it is also used by Modbus settings.
    text = text.replace('window.location.href.slice(0,window.location.href.indexOf("/#"))', f'"http://{ARM_HOST}"')
    # Vite's preload helper prepends '/' to names that already contain js/css/.
    text = re.sub(r'(["\'])/(js|css|png|svg|assets|fonts|json|model|models|stl)/',
                  rf'\1/arm/{arm_key}/\2/', text)
    if "text/css" in content_type:
        text = re.sub(r'(url\(\s*["\']?)/(?!/|arm/)', rf'\1/arm/{arm_key}/', text)
    if "text/html" in content_type:
        text = text.replace("<head>", f'<head><script src="/arm/{arm_key}/__ksq_bridge.js"></script>', 1)
    return text.encode("utf-8")


def _forward(request, target, port, body, length, arm_key):
    connection = HTTPConnection(ARM_HOST, port, timeout=15)
    response = None
    streaming = False
    try:
        upstream = f"http://{ARM_HOST}:{port}{target}"
        headers = _request_headers(request, upstream)
        if length or request.method not in {"GET", "HEAD"}:
            headers["Content-Length"] = str(length)
        connection.request(request.method, target, body=body if length else None, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "").lower()
        rewritten = port == 80 and any(mime in content_type for mime in ("text/html", "javascript", "text/css"))
        response_headers = _response_headers(response, upstream, rewritten, request, arm_key)
        if request.method == "HEAD":
            result = Response(status_code=response.status)
        elif rewritten:
            content = response.read(MAX_TEXT + 1)
            if len(content) > MAX_TEXT:
                raise ValueError("机械臂页面资源过大。")
            result = Response(_rewrite_text(content, content_type, arm_key), status_code=response.status)
        else:
            def chunks():
                while chunk := response.read(64 * 1024):
                    yield chunk

            def close():
                response.close()
                connection.close()

            result = ResourceStream(chunks(), close, status_code=response.status)
            streaming = True
        result.raw_headers = response_headers
        return result
    finally:
        if not streaming:
            if response is not None:
                response.close()
            connection.close()


@router.api_route("/arm", methods=["GET", "HEAD"])
@router.api_route("/arm/", methods=["GET", "HEAD"])
async def arm_entry(request: Request):
    denial = _denial(request)
    if denial is not None:
        return denial
    key = auth.arm_session_token(auth.token_from_cookie(request.headers.get("cookie", "")))
    if not key:
        return HTMLResponse("<p>登录已过期，请重新登录。</p>", status_code=401)
    return RedirectResponse("/arm/" + key + "/", status_code=307, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.api_route("/arm/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@router.api_route("/arm-api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def arm_http(request: Request, path: str):
    arm_key, _, path = path.partition("/")
    denial = _denial(request, arm_key)
    if denial is not None:
        return denial
    prefix = "/arm-api/" if request.url.path.startswith("/arm-api/") else "/arm/"
    browser_headers = _browser_headers(request, arm_key)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=browser_headers)
    if prefix == "/arm/" and path == "__ksq_bridge.js":
        with _STATE_LOCK:
            storage = dict(request.scope["arm_session"]["arm_storage"])
        content = _BRIDGE.read_text().replace("const savedStorage = {};", "const savedStorage = " + json.dumps(storage) + ";")
        return Response(content, media_type="application/javascript", headers=browser_headers)
    if prefix == "/arm/" and path == "__ksq_storage" and request.method == "POST":
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 64 * 1024:
                return Response(status_code=413, headers=browser_headers)
            payload.extend(chunk)
        try:
            storage = json.loads(payload)
        except (ValueError, UnicodeError):
            return Response(status_code=400, headers=browser_headers)
        if not isinstance(storage, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in storage.items()):
            return Response(status_code=400, headers=browser_headers)
        with _STATE_LOCK:
            request.scope["arm_session"]["arm_storage"].clear()
            request.scope["arm_session"]["arm_storage"].update(storage)
        return Response(status_code=204, headers=browser_headers)
    raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
    target = "/" + raw_path[len(prefix) + len(arm_key) + 1:]
    if request.url.query:
        target += "?" + request.url.query
    try:
        with SpooledTemporaryFile(max_size=1024 * 1024) as body:
            length = 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > MAX_UPLOAD:
                    return JSONResponse({"error": "机械臂上传文件超过 256 MiB。"}, status_code=413, headers=browser_headers)
                await run_in_threadpool(body.write, chunk)
            body.seek(0)
            return await run_in_threadpool(_forward, request, target, HTTP_PORTS[prefix], body, length, arm_key)
    except (OSError, HTTPException, ValueError) as error:
        LOGGER.warning("机械臂访问失败 method=%s error=%s", request.method, type(error).__name__)
        if prefix == "/arm/" and ("text/html" in request.headers.get("accept", "") or not path):
            return HTMLResponse('<!doctype html><meta charset="utf-8"><title>机械臂连接失败</title>'
                                '<h2>无法连接机械臂</h2><p>请确认工具所在主机能访问 192.168.11.18，然后刷新重试。</p>', status_code=502, headers=browser_headers)
        return JSONResponse({"error": "无法连接机械臂，请确认工具所在主机能访问 192.168.11.18。"}, status_code=502, headers=browser_headers)


@router.websocket("/arm-ws/{path:path}")
async def arm_websocket(websocket: WebSocket, path: str):
    arm_key, _, path = path.partition("/")
    denial = _denial(websocket, arm_key)
    if denial is not None:
        await websocket.send_denial_response(denial)
        return
    target = "/" + websocket.scope.get("raw_path", websocket.url.path.encode()).decode("ascii")[len("/arm-ws/") + len(arm_key) + 1:]
    if websocket.url.query:
        target += "?" + websocket.url.query
    tasks = []
    accepted = False
    upstream = None
    try:
        cookies = _cookies(websocket.scope["arm_session"], f"http://{ARM_HOST}:8060{target}")
        headers = {"Cookie": cookies} if cookies else {}
        upstream = await connect(f"ws://{ARM_HOST}:8060{target}", origin=f"http://{ARM_HOST}",
                                 additional_headers=headers, subprotocols=websocket.scope.get("subprotocols") or None,
                                 proxy=None, open_timeout=5, close_timeout=2, max_size=16 * 1024 * 1024)
        await websocket.accept(subprotocol=upstream.subprotocol)
        accepted = True

        async def to_arm():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await upstream.send(message.get("bytes") if message.get("bytes") is not None else message["text"])

        async def to_browser():
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)

        async def watch_session():
            while True:
                await asyncio.sleep(1)
                session = auth.session_from_arm_token(arm_key)
                if session is None:
                    await websocket.close(code=1008)
                    return

        tasks = [asyncio.create_task(fn()) for fn in (to_arm, to_browser, watch_session)]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (OSError, WebSocketException, WebSocketDisconnect):
        if not accepted:
            await websocket.send_denial_response(JSONResponse({"error": "机械臂实时连接不可用。"}, status_code=502))
    finally:
        # TestClient and disconnected browsers may cancel the ASGI task first.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if upstream is not None:
                await upstream.close()
            if accepted:
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.close()
