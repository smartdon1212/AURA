"""
Decision/Policy layer (v1 slice): decides which Percepts are worth
sending to the Mind at all.

Without this, the Mind would be asked to react to every single video
frame at camera framerate — expensive, and it would narrate constantly
instead of acting like a companion. This is deliberately small: notify
only on a label not seen within a cooldown window. The bigger
reflex-vs-deliberative arbitration from the architecture stays a stub
until there's a Body with actual reflexes to arbitrate — a desktop app
doesn't need one yet.
"""

from __future__ import annotations

import time


# Ambient narration should stay quiet by default — only truly notable
# events get an unprompted reaction. Everything else is answered only
# when directly asked (via conversation.py), not narrated on sight.
ALWAYS_QUIET_UNLESS_ASKED = frozenset({"person"})


class NoveltyFilter:
    def __init__(
        self,
        cooldown_seconds: float = 30.0,
        reactive_labels: frozenset[str] = ALWAYS_QUIET_UNLESS_ASKED,
    ) -> None:
        self._cooldown = cooldown_seconds
        self._reactive_labels = reactive_labels
        self._last_seen: dict[str, float] = {}

    def is_notable(self, label: str) -> bool:
        if label not in self._reactive_labels:
            return False
        now = time.monotonic()
        last = self._last_seen.get(label)
        if last is not None and (now - last) < self._cooldown:
            return False
        self._last_seen[label] = now
        return True
