"""
Tests for memory/episodic/store.py.

Step 1 (H-2): _build_dsn() must fail loudly when AURA_PG_PASSWORD is
missing, rather than silently substituting a hardcoded fallback.

Step 2 (DSN escaping): a password containing URI delimiters must not be
able to misparse the DSN.

Step 3 (H-4): every psycopg.connect call must pass a bound connect_timeout.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# store.py computes DEFAULT_DSN at import time, so the variable has to be
# present before the module is imported. Set a placeholder here so the suite
# is independent of the ambient environment on any given machine.
os.environ.setdefault("AURA_PG_PASSWORD", "test-only-password")

from memory.episodic import store  # noqa: E402
from memory.episodic.store import _build_dsn  # noqa: E402


class _PasswordEnvMixin:
    """Restores AURA_PG_PASSWORD around each test."""

    def setUp(self) -> None:
        self._saved = os.environ.get("AURA_PG_PASSWORD")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("AURA_PG_PASSWORD", None)
        else:
            os.environ["AURA_PG_PASSWORD"] = self._saved


class BuildDsnTests(_PasswordEnvMixin, unittest.TestCase):
    """Step 1 — H-2."""

    def test_raises_when_password_absent(self) -> None:
        os.environ.pop("AURA_PG_PASSWORD", None)
        with self.assertRaises(RuntimeError) as ctx:
            _build_dsn()
        message = str(ctx.exception)
        self.assertIn("AURA_PG_PASSWORD", message)
        # The error must be actionable, for both supported shells.
        self.assertIn("setx", message)
        self.assertIn("export", message)

    def test_password_appears_in_dsn_when_present(self) -> None:
        os.environ["AURA_PG_PASSWORD"] = "test-only-password"
        self.assertIn("test-only-password", _build_dsn())

    def test_hardcoded_fallback_constant_is_gone(self) -> None:
        self.assertFalse(hasattr(store, "_FALLBACK_PASSWORD"))


class DsnEscapingTests(_PasswordEnvMixin, unittest.TestCase):
    """Step 2 — the password must be percent-encoded into the DSN."""

    def test_password_with_uri_delimiters_keeps_host_intact(self) -> None:
        import psycopg

        for password in ("p@ssw0rd", "p:ss", "p/ss", "p#ss", "p?ss"):
            with self.subTest(password=password):
                os.environ["AURA_PG_PASSWORD"] = password
                os.environ["AURA_PG_HOST"] = "db.example.invalid"
                try:
                    parsed = psycopg.conninfo.conninfo_to_dict(_build_dsn())
                finally:
                    os.environ.pop("AURA_PG_HOST", None)
                self.assertEqual(parsed["host"], "db.example.invalid")
                self.assertEqual(parsed["password"], password)


class ConnectTimeoutTests(_PasswordEnvMixin, unittest.TestCase):
    """Step 3 — H-4: every connect must be bounded by connect_timeout."""

    def _connect_calls(self, exercise) -> tuple[list, list]:
        """
        Runs `exercise(memory)` against a stubbed psycopg.connect and returns
        (construction_calls, exercise_calls).

        The two are separated because EpisodicMemory.__init__ calls
        _ensure_schema, which connects. That connect is itself under test by
        test_ensure_schema_connect_is_bounded, so it has to stay addressable
        rather than being trimmed away as noise.
        """
        from unittest import mock

        with mock.patch.object(store.psycopg, "connect") as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchall.return_value = []
            conn.execute.return_value.fetchone.return_value = None

            memory = store.EpisodicMemory(dsn="postgresql://u:p@h:5432/d")
            during_construction = len(connect.call_args_list)

            exercise(memory)
            return (
                connect.call_args_list[:during_construction],
                connect.call_args_list[during_construction:],
            )

    def _assert_all_bounded(self, calls) -> None:
        self.assertEqual(len(calls), 1, f"expected one connect, got {calls}")
        kwargs = calls[0].kwargs
        self.assertIn("connect_timeout", kwargs)
        self.assertEqual(kwargs["connect_timeout"], store._CONNECT_TIMEOUT)
        self.assertGreater(store._CONNECT_TIMEOUT, 0)

    def test_ensure_schema_connect_is_bounded(self) -> None:
        # Construction calls _ensure_schema — that connect is the one under test.
        construction, _ = self._connect_calls(lambda memory: None)
        self._assert_all_bounded(construction)

    def test_write_connect_is_bounded(self) -> None:
        from contracts import Percept, PerceptType

        percept = Percept(
            source_body_id="test-body",
            type=PerceptType.OBJECT_DETECTED,
            confidence=0.9,
            payload={"label": "cup"},
        )
        _, exercise = self._connect_calls(lambda memory: memory.write(percept))
        self._assert_all_bounded(exercise)

    def test_recent_connect_is_bounded(self) -> None:
        _, exercise = self._connect_calls(lambda memory: memory.recent())
        self._assert_all_bounded(exercise)

    def test_seen_recently_connect_is_bounded(self) -> None:
        _, exercise = self._connect_calls(lambda memory: memory.seen_recently("cup"))
        self._assert_all_bounded(exercise)

    def test_no_bare_connect_remains_in_source(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent
            / "memory"
            / "episodic"
            / "store.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("psycopg.connect(self._dsn)", source)
        self.assertIn("_CONNECT_TIMEOUT", source)


class ConversationStoreTests(_PasswordEnvMixin, unittest.TestCase):
    """
    The conversation_turns table, added 2026-10-08.

    Same stub as above — no Postgres is touched. What is being pinned is
    that the new table is created alongside `percepts`, that its connects
    are bounded like every other one, and that it is read forwards.
    """

    def _memory_under_stub(self):
        from unittest import mock

        patcher = mock.patch.object(store.psycopg, "connect")
        connect = patcher.start()
        self.addCleanup(patcher.stop)
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = []
        conn.execute.return_value.fetchone.return_value = None
        return connect, conn, store.EpisodicMemory(dsn="postgresql://u:p@h:5432/d")

    def test_schema_creates_the_conversation_table(self) -> None:
        _, conn, _ = self._memory_under_stub()
        executed = " ".join(str(call.args[0]) for call in conn.execute.call_args_list)
        self.assertIn("conversation_turns", executed)
        self.assertIn("percepts", executed, "the original table must still be created")

    def test_the_two_tables_are_separate(self) -> None:
        """A said thing must not be stored where a seen thing is queried from."""
        self.assertIn("conversation_turns", store.CONVERSATION_SCHEMA)
        self.assertNotIn("conversation_turns", store.SCHEMA)
        self.assertNotIn("percepts", store.CONVERSATION_SCHEMA)

    def test_record_turn_connect_is_bounded(self) -> None:
        connect, _, memory = self._memory_under_stub()
        memory.record_turn("q", "a", "desktop-01")
        self.assertEqual(
            connect.call_args_list[-1].kwargs["connect_timeout"], store._CONNECT_TIMEOUT
        )

    def test_recent_turns_connect_is_bounded(self) -> None:
        connect, _, memory = self._memory_under_stub()
        memory.recent_turns()
        self.assertEqual(
            connect.call_args_list[-1].kwargs["connect_timeout"], store._CONNECT_TIMEOUT
        )

    def test_record_turn_writes_the_exchange(self) -> None:
        _, conn, memory = self._memory_under_stub()
        memory.record_turn("what is a prime number?", "a number with two factors", "web-01")
        params = conn.execute.call_args_list[-1].args[1]
        self.assertEqual(params[2], "web-01")
        self.assertEqual(params[3], "what is a prime number?")
        self.assertEqual(params[4], "a number with two factors")

    def test_recent_turns_reads_oldest_first(self) -> None:
        """
        The query takes the newest rows; the caller gets them reversed, so
        the prompt never describes an answer before its question.
        """
        _, conn, memory = self._memory_under_stub()
        conn.execute.return_value.fetchall.return_value = [
            {"ts": "t2", "question": "second", "answer": "b"},
            {"ts": "t1", "question": "first", "answer": "a"},
        ]
        rows = memory.recent_turns()
        self.assertEqual([row["question"] for row in rows], ["first", "second"])

    def test_recent_turns_passes_the_limit_through(self) -> None:
        _, conn, memory = self._memory_under_stub()
        memory.recent_turns(limit=7)
        self.assertEqual(conn.execute.call_args_list[-1].args[1], (7,))


if __name__ == "__main__":
    unittest.main()
