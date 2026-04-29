"""Pub/sub event hub.

The runner publishes one event per tick (sample + any anomalies). Multiple
async subscribers (currently: dashboard WebSocket clients) receive each event
through a bounded queue. If a subscriber falls behind, we *drop* events
rather than block the publisher — the monitor loop must never stall waiting
for a slow consumer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("netmon.hub")


class Hub:
    QUEUE_MAX = 200

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        # Synchronous fan-out — never awaits, never raises.
        dropped = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
        if dropped:
            log.debug("hub dropped event for %d slow subscribers", dropped)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
