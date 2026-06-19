"""
Signal queue — in-memory for Days 1-10.
Replaced with Upstash Redis on Day 11 (Signal Queue + Scoring sprint).

Usage:
    from app.queue import signal_queue
    signal_queue.push(signal_dict)
    signal = signal_queue.pop()
    count = signal_queue.size()
"""
import asyncio
import hashlib
import json
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class InMemorySignalQueue:
    def __init__(self):
        self._queue: deque = deque()
        self._seen_hashes: set = set()
        self._lock = asyncio.Lock()

    async def push(self, signal: dict) -> bool:
        """Add signal to queue. Returns False if duplicate (already seen)."""
        signal_hash = signal.get("signal_hash") or _hash_signal(signal)
        async with self._lock:
            if signal_hash in self._seen_hashes:
                return False
            self._seen_hashes.add(signal_hash)
            self._queue.append(signal)
            return True

    async def pop(self) -> Optional[dict]:
        async with self._lock:
            return self._queue.popleft() if self._queue else None

    async def pop_batch(self, n: int = 50) -> list[dict]:
        async with self._lock:
            batch = []
            for _ in range(min(n, len(self._queue))):
                batch.append(self._queue.popleft())
            return batch

    def size(self) -> int:
        return len(self._queue)

    async def clear(self):
        async with self._lock:
            self._queue.clear()


def _hash_signal(signal: dict) -> str:
    key = signal.get("source_url") or signal.get("raw_text", "")
    return hashlib.sha256(key.encode()).hexdigest()


signal_queue = InMemorySignalQueue()
