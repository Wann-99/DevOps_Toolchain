"""Fixed RealMan web/API/WebSocket proxy for authenticated KSQ users."""

import asyncio
import anyio
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import urljoin, urlsplit

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
COOKIE_PREFIX = "ksq_arm_"
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailer", "transfer-encoding", "upgrade"}
_BRIDGE = PACKAGE_DIRECTORY / "web" / "static" / "arm-bridge.js"


def _denial(connection):
    session = auth.session_from_cookie(connection.headers.get("cookie", ""))
    if session is None:
        return HTMLResponse("<p>登录已过期，请重新登录工具后刷新机械臂页面。</p>", status_code=401)
    origin = connection.headers.get("origin")
    expected = str(connection.base_url).rstrip("/").replace("ws://", "http://").replace("wss://", "https://")
    if connection.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != expected):
        return JSONResponse({"error": "拒绝跨站机械臂访问。"}, status_code=403)
    if (connection.scope["type"] == "websocket" or connection.method not in {"GET", "HEAD", "OPTIONS"}) and not origin:
        referer = urlsplit(connection.headers.get("referer", ""))
        if connection.scope["type"] == "websocket" or f"{referer.scheme}://{referer.netloc}" != expected:
            return JSONResponse({"error": "机械臂操作需要同源页面。"}, status_code=403)
    return None


def _cookies(headers):
    cookies = SimpleCookie()
    with suppress(Exception):
        cookies.load(headers.get("cookie", ""))
    return "; ".join(name[len(COOKIE_PREFIX):] + "=" + morsel.coded_value
                     for name, morsel in cookies.items() if name.startswith(COOKIE_PREFIX))


def _request_headers(request):
    excluded = _HOP_HEADERS | {"host", "cookie", "content-length", "accept-encoding", "origin", "referer"}
    excluded.update(name.strip().lower() for name in request.headers.get("connection", "").split(","))
    headers = {name: value for name, value in request.headers.items()
               if name.lower() not in excluded and not name.lower().startswith(("x-forwarded-", "sec-fetch-", "x-ksq-"))}
    headers["Accept-Encoding"] = "identity"
    if request.headers.get("origin"):
        headers["Origin"] = f"http://{ARM_HOST}"
    if _cookies(request.headers):
        headers["Cookie"] = _cookies(request.headers)
    return headers


def _proxy_url(value, upstream):
    parsed = urlsplit(urljoin(upstream, value))
    if parsed.hostname != ARM_HOST or parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("机械臂页面返回了不支持的跳转地址。")
    prefix = {80: "/arm", 8090: "/arm-api"}.get(parsed.port or 80)
    if prefix is None:
        raise ValueError("机械臂页面返回了不支持的端口。")
    return prefix + (parsed.path or "/") + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")


def _response_headers(response, upstream, rewritten):
    excluded = _HOP_HEADERS | {"set-cookie", "location", "cache-control", "content-length"}
    excluded.update(name.strip().lower() for name in (response.getheader("Connection") or "").split(","))
    if rewritten:
        excluded |= {"etag", "last-modified", "content-encoding", "content-md5"}
    headers = [(name.lower().encode("latin-1"), value.encode("latin-1"))
               for name, value in response.getheaders() if name.lower() not in excluded]
    headers.extend([(b"cache-control", b"no-store"), (b"x-frame-options", b"SAMEORIGIN")])
    location = response.getheader("Location")
    if location:
        headers.append((b"location", _proxy_url(location, upstream).encode("latin-1")))
    for name, value in response.getheaders():
        if name.lower() != "set-cookie":
            continue
        cookies = SimpleCookie()
        cookies.load(value)
        for cookie_name, morsel in cookies.items():
            target = SimpleCookie()
            target[COOKIE_PREFIX + cookie_name] = morsel.value
            for attr in ("expires", "max-age", "secure", "httponly", "samesite"):
                if morsel[attr]:
                    target[COOKIE_PREFIX + cookie_name][attr] = morsel[attr]
            target[COOKIE_PREFIX + cookie_name]["path"] = "/"
            headers.append((b"set-cookie", target.output(header="").strip().encode("latin-1")))
    return headers


def _rewrite_text(content, content_type):
    text = content.decode("utf-8")
    # This firmware derives its API host from the URL and assumes port 80.
    # Keep the device identity intact: it is also used by Modbus settings.
    text = text.replace('window.location.href.slice(0,window.location.href.indexOf("/#"))', f'"http://{ARM_HOST}"')
    # Vite's preload helper prepends '/' to names that already contain js/css/.
    text = re.sub(r'(["\'])/(js|css|png|svg|assets|fonts|json|model|models|stl)/', r'\1/arm/\2/', text)
    if "text/css" in content_type:
        text = re.sub(r'(url\(\s*["\']?)/(?!/|arm/)', r'\1/arm/', text)
    if "text/html" in content_type:
        text = text.replace("<head>", '<head><script src="/arm/__ksq_bridge.js"></script>', 1)
    return text.encode("utf-8")


def _forward(request, target, port, body, length):
    connection = HTTPConnection(ARM_HOST, port, timeout=15)
    response = None
    streaming = False
    try:
        headers = _request_headers(request)
        if length or request.method not in {"GET", "HEAD"}:
            headers["Content-Length"] = str(length)
        connection.request(request.method, target, body=body if length else None, headers=headers)
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "").lower()
        rewritten = port == 80 and any(mime in content_type for mime in ("text/html", "javascript", "text/css"))
        upstream = f"http://{ARM_HOST}:{port}{target}"
        response_headers = _response_headers(response, upstream, rewritten)
        if request.method == "HEAD":
            result = Response(status_code=response.status)
        elif rewritten:
            content = response.read(MAX_TEXT + 1)
            if len(content) > MAX_TEXT:
                raise ValueError("机械臂页面资源过大。")
            result = Response(_rewrite_text(content, content_type), status_code=response.status)
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
async def arm_entry(request: Request):
    return _denial(request) or RedirectResponse("/arm/", status_code=307)


@router.api_route("/arm/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@router.api_route("/arm-api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def arm_http(request: Request, path: str):
    denial = _denial(request)
    if denial is not None:
        return denial
    prefix = "/arm-api/" if request.url.path.startswith("/arm-api/") else "/arm/"
    if prefix == "/arm/" and path == "__ksq_bridge.js":
        return Response(_BRIDGE.read_bytes(), media_type="application/javascript", headers={"Cache-Control": "no-store"})
    raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
    target = "/" + raw_path[len(prefix):]
    if request.url.query:
        target += "?" + request.url.query
    try:
        with SpooledTemporaryFile(max_size=1024 * 1024) as body:
            length = 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > MAX_UPLOAD:
                    return JSONResponse({"error": "机械臂上传文件超过 256 MiB。"}, status_code=413)
                await run_in_threadpool(body.write, chunk)
            body.seek(0)
            return await run_in_threadpool(_forward, request, target, HTTP_PORTS[prefix], body, length)
    except (OSError, HTTPException, ValueError) as error:
        LOGGER.warning("机械臂访问失败 method=%s path=%s error=%s", request.method, request.url.path, type(error).__name__)
        if prefix == "/arm/" and ("text/html" in request.headers.get("accept", "") or not path):
            return HTMLResponse('<!doctype html><meta charset="utf-8"><title>机械臂连接失败</title>'
                                '<h2>无法连接机械臂</h2><p>请确认工具所在主机能访问 192.168.11.18，然后刷新重试。</p>', status_code=502)
        return JSONResponse({"error": "无法连接机械臂，请确认工具所在主机能访问 192.168.11.18。"}, status_code=502)


@router.websocket("/arm-ws/{path:path}")
async def arm_websocket(websocket: WebSocket, path: str):
    denial = _denial(websocket)
    if denial is not None:
        await websocket.send_denial_response(denial)
        return
    token = auth.token_from_cookie(websocket.headers.get("cookie", ""))
    target = websocket.scope.get("raw_path", websocket.url.path.encode()).decode("ascii")[len("/arm-ws"):]
    if websocket.url.query:
        target += "?" + websocket.url.query
    tasks = []
    accepted = False
    upstream = None
    try:
        headers = {"Cookie": _cookies(websocket.headers)} if _cookies(websocket.headers) else {}
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
                session = auth.get_session(token)
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
