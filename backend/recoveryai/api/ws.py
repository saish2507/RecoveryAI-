"""WebSocket fan-out for live console updates.

One endpoint, one broadcaster. The previous build ran two parallel WebSocket
connections that duplicated every message and drifted out of sync.

Messages are cache-invalidation hints plus a small payload, not the full state:
the console re-fetches through the REST API, so there is exactly one source of
truth for what a case looks like and no risk of the socket and the API
disagreeing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class Broadcaster:
    """Tracks connected consoles and pushes events to all of them.

    Also callable from non-async contexts — the scheduler's decisions arrive on
    an APScheduler thread with no running event loop, and `publish_threadsafe`
    is how they reach the socket without blocking that thread.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("console connected", extra={"connections": len(self._connections)})

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("console disconnected", extra={"connections": len(self._connections)})

    async def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if not self._connections:
            return
        message = json.dumps({"type": event_type, "payload": payload or {}}, default=str)

        async with self._lock:
            targets = list(self._connections)

        dead: list[WebSocket] = []
        for connection in targets:
            try:
                await connection.send_text(message)
            except Exception:
                # A console that closed mid-broadcast is normal. Collect and drop
                # it rather than letting one dead socket abort the fan-out.
                dead.append(connection)

        if dead:
            async with self._lock:
                for connection in dead:
                    self._connections.discard(connection)

    def publish_threadsafe(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Fire-and-forget publish from a worker thread."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(event_type, payload), self._loop)
        except Exception:
            logger.debug("threadsafe publish dropped", exc_info=True)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
