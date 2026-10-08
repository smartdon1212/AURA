"""
Manual test runner for the event bus — no camera needed.

Publishes a fake Percept, then consumes it back via a consumer group,
proving the Redis round trip works before we wire real perception output
into it.

Requires a Redis-compatible server running on localhost:6379
(Docker Desktop or Memurai on Windows).

Run with:   uv run python bus/run_local.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import PERCEPTS_STREAM, EventBus
from contracts import Percept, PerceptType

GROUP = "test-group"
CONSUMER = "test-consumer"


def consume_loop(bus: EventBus, stop_after: int) -> None:
    seen = 0
    for entry_id, percept in bus.consume(PERCEPTS_STREAM, GROUP, CONSUMER, Percept):
        print(f"Consumed: {percept.payload.get('label')} (id={entry_id})")
        bus.ack(PERCEPTS_STREAM, GROUP, entry_id)
        seen += 1
        if seen >= stop_after:
            break


def main() -> None:
    bus = EventBus()

    consumer_thread = threading.Thread(target=consume_loop, args=(bus, 1), daemon=True)
    consumer_thread.start()

    time.sleep(0.5)  # let the consumer group register before publishing

    test_percept = Percept(
        source_body_id="desktop-01",
        type=PerceptType.OBJECT_DETECTED,
        confidence=0.87,
        payload={"label": "test-object", "bbox": [0, 0, 10, 10]},
    )
    entry_id = bus.publish(PERCEPTS_STREAM, test_percept)
    print(f"Published: {test_percept.payload['label']} (id={entry_id})")

    consumer_thread.join(timeout=5)
    if consumer_thread.is_alive():
        print("FAILED — consumer never received the message. Is Redis running?")
    else:
        print("OK — bus round trip works.")


if __name__ == "__main__":
    main()
