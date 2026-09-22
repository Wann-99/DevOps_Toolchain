"""FastAPI transport for the shared HTTP routes and validation rules."""

from __future__ import annotations

from http import HTTPStatus
from itertools import chain
from tempfile import SpooledTemporaryFile
from urllib.parse import urlparse
import html
import logging
import os

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, Response, StreamingResponse
import anyio

from ksq.robot import logs
from ksq.web.handlers import LOGGER, RequestDispatcher
from ksq.web.pages import resolve_static_file


class ResourceStream(StreamingResponse):
    """Close files/upstream streams on normal completion and disconnect."""

    def __init__(self, content, close, **kwargs):
        super().__init__(content, **kwargs)
        self.close_resource = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            raise
        except Exception:
            LOGGER.exception("响应流异常 path=%s", scope.get("path", ""))
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.close_resource)


class RequestContext(RequestDispatcher):
    """Adapt response writes; sockets and HTTP framing belong to Uvicorn."""

    def __init__(self, request: Request):
        self.command = request.method
        raw_path = request.scope.get("raw_path", request.url.path.encode())
        self.path = raw_path.decode("ascii", errors="surrogateescape")
        if request.url.query:
            self.path += "?" + request.url.query
        self.headers = request.headers
        self.connection = None
        self.rfile = SpooledTemporaryFile(max_size=1024 * 1024)
        self.wfile = SpooledTemporaryFile(max_size=1024 * 1024)
        self.status = 200
        self.response_headers = []
        self.response = None
        self.response_closer = None
        self.form = {}

    def send_response(self, status):
        self.status = int(status)
        self.response_headers = []

    def send_header(self, name, value):
        # Uvicorn owns connection framing, including chunking for streaming.
        if name.lower() not in {"connection", "transfer-encoding"}:
            self.response_headers.append((name.lower().encode("latin-1"), str(value).encode("latin-1")))

    def end_headers(self):
        pass

    def send_error(self, status, message=""):
        if urlparse(self.path).path.startswith("/api/"):
            self._send_json(status, {"error": message})
        else:
            self._send_html(status, "<!doctype html><title>Error</title><p>" + html.escape(message) + "</p>")

    def _send_static(self, relative_path):
        path = resolve_static_file(relative_path)
        if path is None:
            self.send_error(404, "Static file not found")
        else:
            self.response = FileResponse(path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    def _send_log_stream(self, service, tail, last_event_id):
        events = logs.stream_log_events(service, tail, last_event_id)
        try:
            first = next(events)
        except logs.LogServiceError as error:
            events.close()
            self._send_json(HTTPStatus(error.status_code), {"error": str(error)})
            return

        def frames():
            try:
                for event in chain((first,), events):
                    yield logs.encode_sse_event(event)
            finally:
                events.close()

        self.response = ResourceStream(
            frames(), events.close, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
        self.response_closer = events.close

    def stream_forward_response(self, response, connection):
        self._stream_resource(response, connection.close)

    def stream_file(self, stream):
        # The synchronous caller closes its own descriptor on return.
        source = os.fdopen(os.dup(stream.fileno()), "rb")
        self._stream_resource(source, source.close)

    def _stream_resource(self, source, close):
        def chunks():
            while chunk := source.read(64 * 1024):
                yield chunk
        self.response = ResourceStream(chunks(), close, status_code=self.status)
        self.response.raw_headers = self.response_headers
        self.response_closer = close

    def close(self):
        if self.response_closer is not None:
            self.response_closer()
            self.response_closer = None
        self.rfile.close()
        self.wfile.close()

    def build_response(self):
        status = self.response.status_code if self.response is not None else self.status
        # Reads include frequent dashboard, health, map and terminal polling.
        level = logging.DEBUG if self.command == "GET" and status < 400 else logging.INFO
        LOGGER.log(
            level,
            "HTTP method=%s path=%s status=%s", self.command,
            urlparse(self.path).path, status,
        )
        if self.response is not None:
            self.response_closer = None  # The response now owns its resource.
            self.close()
            return self.response
        self.rfile.close()
        self.wfile.seek(0)

        def chunks():
            try:
                while chunk := self.wfile.read(64 * 1024):
                    yield chunk
            finally:
                self.wfile.close()

        response = ResourceStream(chunks(), self.wfile.close, status_code=self.status)
        response.raw_headers = self.response_headers
        return response


async def dispatch(request: Request) -> Response:
    context = RequestContext(request)
    form = None
    try:
        # Authorize writes before accepting uploads. The same guard is used by
        # the synchronous dispatcher, so the two transports cannot diverge.
        if request.method in {"POST", "PUT"}:
            allowed = await run_in_threadpool(context.authorize_write)
            if not allowed:
                return context.build_response()
            path = urlparse(context.path).path
            if path in {"/api/import", "/load-upload"}:
                form = await request.form()
                for name, upload in form.multi_items():
                    if isinstance(upload, UploadFile):
                        context.form.setdefault(name, []).append(upload)
            else:
                length = 0
                async for chunk in request.stream():
                    await run_in_threadpool(context.rfile.write, chunk)
                    length += len(chunk)
                await run_in_threadpool(context.rfile.seek, 0)
                # JSON requests sent with HTTP chunking still need their actual
                # size at the shared body reader; file routes retain the header
                # checks which reject chunked or missing-length uploads.
                if not path.startswith(("/api/files/", "/api/terminal/")):
                    values = dict(context.headers)
                    values["content-length"] = str(length)
                    context.headers = Headers(values)
        await run_in_threadpool(getattr(context, "do_" + request.method))
        return context.build_response()
    except (ValueError, OSError, HTTPException) as error:
        context._send_json(400, {"error": str(error.detail) if isinstance(error, HTTPException) else str(error)})
        return context.build_response()
    except BaseException as error:
        if isinstance(error, Exception):
            LOGGER.exception("请求未处理异常 method=%s path=%s", request.method, urlparse(context.path).path)
        context.close()
        raise
    finally:
        if form is not None:
            await form.close()
