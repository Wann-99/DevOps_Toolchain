"""Authenticated WebSocket bridge to the existing Unix-socket host agent."""

from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
import asyncio
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from ksq.web import auth, desktop, host_files


router = APIRouter(tags=["desktop"])


@router.websocket("/desktop/{path:path}")
async def proxy_desktop(websocket: WebSocket, path: str):
    token = auth.token_from_cookie(websocket.headers.get("cookie", ""))
    session = auth.get_session(token)
    if session is None:
        await websocket.send_denial_response(RedirectResponse("/login", status_code=302))
        return
    origin = urlsplit(websocket.headers.get("origin", ""))
    if (websocket.headers.get("sec-fetch-site") == "cross-site"
            or origin.scheme not in {"http", "https"}
            or origin.netloc != websocket.headers.get("host")):
        await websocket.send_denial_response(JSONResponse({"error": "拒绝跨站桌面连接。"}, status_code=403))
        return
    if not host_files._DESKTOP_CONNECTIONS.acquire(blocking=False):
        await websocket.send_denial_response(JSONResponse({"error": "桌面连接已达上限，请关闭其他桌面标签页。"}, status_code=503))
        return
    accepted = False
    tasks = []
    try:
        agent = os.environ.get("KSQ_HOST_FILES_SOCKET")
        socket_path = Path(agent or await run_in_threadpool(desktop.connection_path))
        target = websocket.scope.get("raw_path", websocket.url.path.encode()).decode("ascii")
        if not agent:
            target = target[len("/desktop"):]
        if websocket.url.query:
            target += "?" + websocket.url.query
        headers = {"Cookie": auth.SESSION_COOKIE + "=" + host_files.owner_key(token), "X-KSQ-Request": "1"}
        # The deployment directory can exceed the Unix socket path limit.
        directory = os.open(socket_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            connection = unix_connect(
                path=f"/proc/self/fd/{directory}/{socket_path.name}", uri="ws://localhost" + target,
                additional_headers=headers, subprotocols=websocket.scope.get("subprotocols") or None,
                max_size=None, open_timeout=10,
            )
            async with connection as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)
                accepted = True

                async def to_upstream():
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
                        current = auth.get_session(token)
                        if current is None:
                            await websocket.close(code=1008)
                            return

                tasks = [asyncio.create_task(fn()) for fn in (to_upstream, to_browser, watch_session)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            os.close(directory)
    except (OSError, WebSocketException, WebSocketDisconnect):
        if not accepted:
            await websocket.send_denial_response(JSONResponse({"error": "宿主机连接不可用，请在宿主机部署目录执行 bash start.sh host-files start。"}, status_code=503))
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if accepted:
            with suppress(RuntimeError, WebSocketDisconnect, ConnectionClosed):
                await websocket.close()
        host_files._DESKTOP_CONNECTIONS.release()
