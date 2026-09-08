"""Per-request loading progress, readable without taking the dataset lock."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, local
from typing import Iterator
from uuid import UUID, uuid4


_LOCK = Lock()
_LOCAL = local()
_PROGRESS: dict[str, dict[str, object]] = {}
LOAD_ENDPOINTS = frozenset({
    "/load-paths", "/load-auto", "/load-upload", "/api/import", "/api/reload",
})


@contextmanager
def track(raw_id: str, owner: str) -> Iterator[None]:
    load_id = str(UUID(raw_id)) if raw_id else str(uuid4())
    with _LOCK:
        if load_id in _PROGRESS:
            raise ValueError("加载请求编号已使用，请重新发起加载。")
        # ponytail: retain the latest 64 requests in memory; durable jobs only
        # become necessary if loading must survive a server restart.
        while len(_PROGRESS) >= 64:
            completed = next((key for key, value in _PROGRESS.items()
                              if value["status"] != "running"), None)
            if completed is None:
                raise ValueError("正在加载的请求过多，请稍后重试。")
            _PROGRESS.pop(completed)
        _PROGRESS[load_id] = {
            "id": load_id, "owner": owner, "status": "running",
            "stage": "waiting", "message": "等待加载", "percent": None,
            "completed": 0, "total": 0,
        }
    _LOCAL.load_id = load_id
    try:
        yield
    except Exception as error:
        finish(str(error))
        raise
    else:
        finish()
    finally:
        _LOCAL.load_id = None


def update(stage: str, message: str, completed: int = 0, total: int = 0) -> None:
    load_id = getattr(_LOCAL, "load_id", None)
    with _LOCK:
        progress = _PROGRESS.get(load_id)
        if progress is not None and progress["status"] == "running":
            progress.update(stage=stage, message=message, completed=completed,
                            total=total, percent=round(completed * 100 / total)
                            if total else None)


def finish(error: str = "") -> None:
    with _LOCK:
        progress = _PROGRESS.get(getattr(_LOCAL, "load_id", None))
        if progress is not None and progress["status"] == "running":
            progress.update(status="error" if error else "done",
                            stage="error" if error else "done",
                            message=error or "加载完成", percent=None if error else 100)


def snapshot(load_id: str, owner: str) -> dict[str, object]:
    with _LOCK:
        progress = _PROGRESS.get(load_id)
        if progress is None or progress["owner"] != owner:
            return {"id": load_id, "status": "waiting", "stage": "waiting",
                    "message": "等待服务器接收", "percent": None,
                    "completed": 0, "total": 0}
        return {key: value for key, value in progress.items() if key != "owner"}
