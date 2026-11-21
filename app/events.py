"""A small thread-safe publish/subscribe bus for log events.

Drivers emit events; the UI consumes them over Server-Sent Events. `emit` is
synchronous and safe to call from worker threads (Selenium runs off-loop), so
delivery onto asyncio queues is marshalled with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    level: str
    message: str
    data: dict[str, Any] | None = None

    def sse(self) -> str:
        """Render as one SSE frame."""
        import json

        payload = {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "message": self.message,
            "data": self.data or {},
        }
        return f"event: log\ndata: {json.dumps(payload, default=str)}\n\n"


@dataclass
class LogBus:
    """Bounded history plus fan-out to live subscribers."""

    history_limit: int = 400
    queue_limit: int = 200
    _history: deque[Event] = field(init=False)
    _subscribers: set[asyncio.Queue[Event]] = field(init=False, default_factory=set)
    _seq: int = field(init=False, default=0)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _loop: asyncio.AbstractEventLoop | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._history = deque(maxlen=self.history_limit)

    # -- loop binding ------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- publishing --------------------------------------------------------
    def emit(self, level: str, message: str, **data: Any) -> Event:
        """Record an event and fan it out. Never raises."""
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                level=level,
                message=message,
                data=data or None,
            )
            self._history.append(event)
            subscribers = list(self._subscribers)

        for queue in subscribers:
            self._deliver(queue, event)
        return event

    def info(self, message: str, **data: Any) -> Event:
        return self.emit("info", message, **data)

    def warn(self, message: str, **data: Any) -> Event:
        return self.emit("warn", message, **data)

    def error(self, message: str, **data: Any) -> Event:
        return self.emit("error", message, **data)

    def step(self, message: str, **data: Any) -> Event:
        return self.emit("step", message, **data)

    # -- history -----------------------------------------------------------
    def history(self, since_seq: int = 0, limit: int = 200) -> list[Event]:
        """Return events with seq > since_seq, most recent last."""
        with self._lock:
            items = [e for e in self._history if e.seq > since_seq]
        return items[-limit:]

    # -- subscription ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.queue_limit)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- internals ---------------------------------------------------------
    def _deliver(self, queue: asyncio.Queue[Event], event: Event) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._put(queue, event)
            return
        try:
            loop.call_soon_threadsafe(self._put, queue, event)
        except RuntimeError:
            pass  # loop shutting down

    @staticmethod
    def _put(queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest so a slow consumer cannot wedge the producer.
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except Exception:  # pragma: no cover - defensive
                pass
