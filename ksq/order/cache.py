"""Broker response/token caches and their process-local locks."""

from __future__ import annotations

from threading import Lock
from typing import Dict, Optional, Tuple


# Broker 任务详情每次快照都实时拉一次（云端 HTTP 调用），是轮询延迟的主要来源。
# 同一任务 2 秒内视为新鲜：播报/确认这类日志驱动的交互得以即时弹出，工单状态
# 芯片最多滞后 2 秒，可接受。
_BROKER_ORDER_CACHE_TTL_SECONDS = 2.0


_BROKER_ORDER_CACHE: Dict[str, Tuple[float, Dict[str, object]]] = {}


_BROKER_ORDER_CACHE_LOCK = Lock()


def invalidate_broker_order_cache(task_id: str) -> None:
    """写操作成功后丢弃 Broker 详情缓存，下一次轮询立即重拉。

    传入具体 task_id 时按任务精准失效；传空串时全量清空（按 order_no
    直发的操作无法反查 task_id，只能整体作废）。
    """
    task_id = str(task_id or "").strip()
    with _BROKER_ORDER_CACHE_LOCK:
        if not task_id:
            _BROKER_ORDER_CACHE.clear()
            return
        stale = [key for key in _BROKER_ORDER_CACHE if key.endswith("|" + task_id)]
        for key in stale:
            _BROKER_ORDER_CACHE.pop(key, None)


TOKEN_LOCK = Lock()
order_access_token: Optional[str] = None
order_access_token_key: str = ""
order_access_tokens: Dict[str, str] = {}
