"""
Episodic writer service: consumes every Percept from the bus and
persists it to Postgres. Runs as its own long-lived process with its own
consumer group — independent of the Mind's reasoning loop, so restarting
one never affects the other.

Requires a Postgres server reachable at the DSN that
memory/episodic/store.py builds from the AURA_PG_* environment variables.
The password comes from AURA_PG_PASSWORD and is never written into
source — see README.md for the one-time setup.

Run with:  uv run python memory/episodic/writer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import psycopg
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bus import PERCEPTS_STREAM, EventBus
from contracts import Percept
from memory.episodic.store import EpisodicMemory

GROUP = "memory-writer"
CONSUMER = "writer-1"

# Bounded retry around a single write. `write()` opens a fresh connection
# per percept, so a transient connection failure is expected on a loaded
# or busy server rather than exceptional — and on this machine the disk is
# slow enough that connecting can genuinely take seconds (see
# store._CONNECT_TIMEOUT).
#
# Only OperationalError is retried. That is psycopg's connection-level
# class: the server refused, dropped, or timed out. An IntegrityError or
# ProgrammingError means the statement or the data is wrong, which retrying
# cannot fix and which must not be hidden — those still raise.
#
# This mirrors the retry idiom already used in
# mind/llm_provider/gemini_provider.py: tenacity, bounded attempts,
# exponential wait, and a narrow exception type.
@retry(
    retry=retry_if_exception_type(psycopg.OperationalError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _write(memory: EpisodicMemory, percept: Percept) -> None:
    memory.write(percept)


def main() -> None:
    bus = EventBus()
    memory = EpisodicMemory()
    print("Episodic memory writer running. Ctrl+C to stop.")

    # One warning per outage, not one per percept: the stream carries
    # several percepts a second, so per-percept reporting would bury the
    # console. Same shape as the camera-failure handling in
    # perception/service.py.
    consecutive_failures = 0

    for entry_id, percept in bus.consume(PERCEPTS_STREAM, GROUP, CONSUMER, Percept):
        try:
            _write(memory, percept)
        except psycopg.OperationalError as exc:
            # The percept is deliberately NOT acked. It stays in this
            # consumer group's PEL, so a restart re-delivers it once
            # Postgres is back — losing observations of the world is the
            # one thing a memory service must not do quietly. (The insert
            # is ON CONFLICT DO NOTHING, so a redelivery is free.)
            #
            # This is also why a transient failure must not propagate:
            # before this guard, any database error killed the writer
            # outright and memory silently stopped recording for the rest
            # of the session. That is what happened on 2026-10-06 — the
            # writer printed its banner, wrote for a while, then died with
            # the Mind still running. The Mind consumes the same stream
            # with the same Percept model and was unaffected, which rules
            # the parse path out and points at the database.
            consecutive_failures += 1
            if consecutive_failures == 1:
                print(
                    f"\nWARNING: cannot write percepts to Postgres ({exc}). "
                    "Retrying; percepts are being held, not lost. Memory is "
                    "NOT being recorded until this clears."
                )
            continue

        if consecutive_failures:
            print(
                f"Memory writer recovered after {consecutive_failures} "
                "failed write(s). Held percepts are re-delivered on restart."
            )
            consecutive_failures = 0

        bus.ack(PERCEPTS_STREAM, GROUP, entry_id)


if __name__ == "__main__":
    main()
