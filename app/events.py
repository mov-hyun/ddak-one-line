from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any


class EventHub:
    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=history_size))
        self._lock = asyncio.Lock()

    async def publish(
        self, channel: str, payload: dict[str, Any], *, store: bool = True
    ) -> None:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            **payload,
        }
        if store:
            self._history[channel].append(event)
        async with self._lock:
            subscribers = tuple(self._subscribers[channel])
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def subscribe(self, channel: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[channel].add(queue)
        return queue

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers[channel].discard(queue)

    def history(self, channel: str) -> list[dict[str, Any]]:
        return list(self._history[channel])


event_hub = EventHub()
