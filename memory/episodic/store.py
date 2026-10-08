"""
EpisodicMemory: structured, queryable record of everything AURA has
perceived — the Postgres side of memory.

v1 scope note: this is recency/filter-based recall only. Semantic vector
recall (pgvector + embeddings) is deliberately deferred — it needs its
own embeddings pipeline and isn't load-bearing for v1 to work end to
end. Adding it later means a new module alongside this one, not a
rewrite of it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import psycopg
from psycopg.rows import dict_row

from contracts import Percept

SCHEMA = """
CREATE TABLE IF NOT EXISTS percepts (
    id UUID PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    source_body_id TEXT NOT NULL,
    type TEXT NOT NULL,
    confidence REAL NOT NULL,
    payload JSONB NOT NULL,
    raw_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_percepts_ts ON percepts (ts DESC);
CREATE INDEX IF NOT EXISTS idx_percepts_type ON percepts (type);
"""

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = "5432"
_DEFAULT_USER = "postgres"
_DEFAULT_DB = "postgres"

# Bound every connect attempt. psycopg's default is 130 seconds, which is
# long enough that an unreachable-but-not-refusing host (firewall drop,
# sleeping machine, wrong subnet) stalls the caller for over two minutes.
# write() connects per row, so that stall lands on the percept drain path.
#
# This was 2 seconds, chosen to match the preflight check. That was too
# tight for this machine and it caused real damage:
#
#   - Session 6: run_aura.py's preflight reported "Postgres is not
#     reachable" while Postgres was up and answering in 1.34 s. The 2 s
#     limit had simply elapsed on a cold disk.
#   - Session 8: the memory writer died mid-run on the drain path, having
#     connected successfully moments earlier to create its schema. Two
#     candidates existed (a malformed message, or this timeout elapsing
#     under cold-start load). **The parse path is now ruled out**: the
#     `mind` consumer reads the same `percepts` stream with the same
#     `Percept` model (mind/reasoning_loop.py:46) and survived the same
#     run — same entries, same parse, no error. So the writer died on the
#     database side, and this timeout is the leading explanation.
#
# 10 s is a compromise, not a fix: it is long enough to survive the cold
# start this machine is documented to have, and short enough that a
# genuinely dead server is still reported in seconds rather than minutes.
# The real fix is an SSD. Do not lower this back to 2 without re-checking
# the two failures above.
_CONNECT_TIMEOUT = 10


def _build_dsn() -> str:
    """
    Builds the Postgres connection string from environment variables, so
    a real password never has to be hardcoded in source or pasted into a
    chat log. Set it once with:

        setx AURA_PG_PASSWORD "your-actual-password"     (PowerShell, persists across sessions)
        export AURA_PG_PASSWORD="your-actual-password"    (bash, current session only)

    AURA_PG_HOST / AURA_PG_PORT / AURA_PG_USER / AURA_PG_DB are also
    overridable the same way, for the (less common) case where any of
    those differ from a default local install.

    The password is percent-encoded before it is interpolated, so a
    password containing URI delimiters (@ : / # ?) can't misparse the
    connection string — an unencoded "@" would otherwise turn the rest
    of the password into the host.
    """
    host = os.environ.get("AURA_PG_HOST", _DEFAULT_HOST)
    port = os.environ.get("AURA_PG_PORT", _DEFAULT_PORT)
    user = os.environ.get("AURA_PG_USER", _DEFAULT_USER)
    db = os.environ.get("AURA_PG_DB", _DEFAULT_DB)
    password = os.environ.get("AURA_PG_PASSWORD")
    if not password:
        raise RuntimeError(
            "AURA_PG_PASSWORD is not set — refusing to fall back to a "
            "default. Set it once with:\n"
            '    setx AURA_PG_PASSWORD "your-actual-password"     '
            "(PowerShell, persists across sessions)\n"
            '    export AURA_PG_PASSWORD="your-actual-password"   '
            "(bash, current session only)"
        )
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{db}"


# Computed once at import time — used by run_aura.py's preflight check.
# EpisodicMemory itself recomputes fresh in __init__ (see below), so a
# password set after this module first loads is still picked up correctly.
DEFAULT_DSN = _build_dsn()


class EpisodicMemory:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or _build_dsn()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with psycopg.connect(
            self._dsn, connect_timeout=_CONNECT_TIMEOUT
        ) as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def write(self, percept: Percept) -> None:
        with psycopg.connect(
            self._dsn, connect_timeout=_CONNECT_TIMEOUT
        ) as conn:
            conn.execute(
                """
                INSERT INTO percepts (id, ts, source_body_id, type, confidence, payload, raw_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    str(percept.id),
                    percept.ts,
                    percept.source_body_id,
                    percept.type.value,
                    percept.confidence,
                    psycopg.types.json.Json(percept.payload),
                    percept.raw_ref,
                ),
            )
            conn.commit()

    def recent(self, within_seconds: int = 300, limit: int = 20) -> list[dict]:
        """Most recent percepts within a time window, newest first."""
        since = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        with psycopg.connect(
            self._dsn, row_factory=dict_row, connect_timeout=_CONNECT_TIMEOUT
        ) as conn:
            rows = conn.execute(
                """
                SELECT ts, type, confidence, payload
                FROM percepts
                WHERE ts >= %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (since, limit),
            ).fetchall()
        return rows

    def seen_recently(self, label: str, within_seconds: int = 30) -> bool:
        """Used by the Decision layer to avoid re-announcing the same object."""
        since = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        with psycopg.connect(
            self._dsn, connect_timeout=_CONNECT_TIMEOUT
        ) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM percepts
                WHERE ts >= %s AND payload->>'label' = %s
                LIMIT 1
                """,
                (since, label),
            ).fetchone()
        return row is not None
