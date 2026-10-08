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


if __name__ == "__main__":
    unittest.main()
