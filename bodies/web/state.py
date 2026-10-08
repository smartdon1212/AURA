"""
AuraState: the one place the web Body's background threads write to and
its HTTP handlers read from.

Two things live here, and they are here together because they have the
same lifetime and the same access pattern — the vision thread and the
narration thread both push, every connected browser pulls:

  - the current state name ("observing", "listening", "thinking",
    "speaking"), which drives the orb and the colour of the whole page
  - the fan-out to connected browsers, so one stalled tab cannot stall
    AURA

The fan-out crosses a thread boundary: the producers are OS threads, the
consumers are asyncio tasks inside uvicorn's loop. `asyncio.Queue` is not
thread-safe, so each subscriber captures the loop it was created on and
every push goes through `call_soon_threadsafe`. That is the standard
pattern for this (asyncio's own "concurrency and multithreading" docs) and
it is why there is no polling loop anywhere in the event path.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

# Per-connection backlog. A browser on a slow link, or a tab that is
# throttled in the background, can fall behind. Past this many unread
# events we drop rather than grow without bound.
QUEUE_SIZE = 128


@dataclass
class Subscriber:
    """One connected browser. Bound to the loop its queue belongs to."""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class AuraState:
    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        self._queue_size = queue_size
        self._state = "observing"
        self._stats: dict[str, Any] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def set_state(self, name: str) -> None:
        """Set and broadcast, but only when it actually changed — the orb
        redraws on every state event, and the vision path is chatty."""
        with self._lock:
            if name == self._state:
                return
            self._state = name
        self.broadcast("state", {"state": name})

    def set_stats(self, **values: Any) -> None:
        with self._lock:
            self._stats.update(values)
            snapshot = dict(self._stats)
        self.broadcast("stats", snapshot)

    def subscribe(self) -> Subscriber:
        """
        Register a browser and prime it with the current state.

        Priming matters: a page opened while AURA is mid-answer must show
        `thinking` immediately, not sit on the orb's default until the
        next change — which, if AURA has gone quiet, may be a long time.
        """
        sub = Subscriber(loop=asyncio.get_running_loop(), queue=asyncio.Queue(maxsize=self._queue_size))

        with self._lock:
            self._subscribers.append(sub)
            current = self._state
            stats = dict(self._stats)

        sub.queue.put_nowait(("state", {"state": current}))
        if stats:
            sub.queue.put_nowait(("stats", stats))
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass  # already gone; never an error

    def broadcast(self, event: str, data: dict) -> None:
        """
        Push to every connected browser.

        Safe to call from any thread, and safe to call after the event
        loop has gone away — a Body shutting down mid-broadcast must not
        raise inside the vision thread.
        """
        with self._lock:
            subscribers = list(self._subscribers)

        for sub in subscribers:
            try:
                sub.loop.call_soon_threadsafe(self._offer, sub, event, data)
            except RuntimeError:
                # The loop is closed. That browser is gone; drop it rather
                # than keep pushing into a dead queue on every frame.
                self.unsubscribe(sub)

    @staticmethod
    def _offer(sub: Subscriber, event: str, data: dict) -> None:
        """Runs on the subscriber's own loop, so the queue is only ever
        touched from one thread."""
        try:
            sub.queue.put_nowait((event, data))
        except asyncio.QueueFull:
            # Drop the newest, not the oldest. Dropping the oldest would
            # deliver a stale state change *after* a fresh one and leave
            # the orb showing the wrong thing; dropping this one matches
            # the drop-don't-queue policy the rest of the system uses for
            # ambient output. A client this far behind has bigger
            # problems than a missing narration line.
            pass
