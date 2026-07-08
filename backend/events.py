"""Async event bus that fans out booth events to all connected WebSocket clients.

Events drive the kiosk UI state machine (idle / countdown / capturing / review …).
`publish()` is safe to call from background threads (GPIO/gesture) via the stored
event loop.
"""
from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_state: dict[str, Any] = {"type": "idle"}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; drop this frame

    def publish(self, event: dict) -> None:
        """Thread-safe publish. Remembers the latest 'state' event for new clients."""
        if event.get("type") not in ("ping",):
            self.last_state = event
        if self._loop is None:
            return
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(event), self._loop)
        else:
            self._loop.run_until_complete(self._broadcast(event))


bus = EventBus()
