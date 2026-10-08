"""
Tests for the session-9 stabilization round: the message-parse guard
(plan step 10 / G-8), the memory writer surviving a transient database
failure, the connect timeout that was too tight for this machine, and the
interactive path surviving a failed exchange.

The common thread is a single rule: **a long-running service must not be
killable by one expected failure.** In the 2026-10-06 run, two of them
were: the memory writer died mid-run (cause still undiagnosed, both known
candidates guarded here), and a failed exchange in the conversation loop
would have propagated all the way out of run_aura.py and taken perception
and the Mind down with it.
"""

from __future__ import annotations

import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import psycopg
from pydantic import BaseModel
from tenacity import wait_none

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Stop(Exception):
    """Ends a consumer generator in a test without waiting for a block."""


class _Message(BaseModel):
    text: str


class _Exploding(BaseModel):
    """A model whose validation fails with something that is NOT a
    ValidationError — standing in for a genuine bug in a consumer's model."""

    text: str

    def __init__(self, **data):  # noqa: ANN003
        raise RuntimeError("a real bug in the model, not a bad message")


def _entry(entry_id: str, data: str) -> tuple[str, dict]:
    return entry_id, {"data": data}


class _FakeRedis:
    """Serves one batch of entries, then signals the end of the test."""

    def __init__(self, entries: list[tuple[str, dict]]) -> None:
        self._entries = entries
        self._served = False
        self.acked: list[tuple[str, str, str]] = []

    def xgroup_create(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def xreadgroup(self, group, consumer, streams, count, block):  # noqa: ANN001, ANN201
        if self._served:
            raise _Stop()
        self._served = True
        return [("percepts", self._entries)]

    def xack(self, stream, group, entry_id) -> None:  # noqa: ANN001
        self.acked.append((stream, group, entry_id))


class Step10MalformedParseTests(unittest.TestCase):
    """G-8 — one bad message must not kill a long-running consumer."""

    def _consume(self, entries, model_cls=_Message):
        from bus.stream import EventBus

        bus = EventBus.__new__(EventBus)  # skip the redis connection
        fake = _FakeRedis(entries)
        bus._redis = fake

        seen = []
        with self.assertRaises(_Stop):
            for entry_id, model in bus.consume("percepts", "g", "c", model_cls):
                seen.append((entry_id, model))
        return seen, fake

    def test_malformed_entry_is_dropped_and_the_valid_one_survives(self) -> None:
        entries = [
            _entry("1-0", "not json at all"),
            _entry("2-0", _Message(text="hello").model_dump_json()),
        ]

        with self.assertLogs("bus.stream", level="WARNING") as logs:
            seen, fake = self._consume(entries)

        self.assertEqual([model.text for _, model in seen], ["hello"])
        self.assertEqual(
            [call[2] for call in fake.acked],
            ["1-0"],
            "the malformed entry must be XACKed — it can never become "
            "parseable, so redelivering it is an infinite crash loop",
        )
        self.assertIn("1-0", "\n".join(logs.output))

    def test_valid_schema_but_wrong_shape_is_also_dropped(self) -> None:
        """Schema drift between producer and consumer is the G-8 scenario."""
        entries = [_entry("1-0", '{"wrong_field": 3}')]

        with self.assertLogs("bus.stream", level="WARNING"):
            seen, fake = self._consume(entries)

        self.assertEqual(seen, [])
        self.assertEqual([call[2] for call in fake.acked], ["1-0"])

    def test_missing_data_field_is_dropped(self) -> None:
        entries = [("1-0", {"unexpected": "field"})]

        with self.assertLogs("bus.stream", level="WARNING") as logs:
            seen, fake = self._consume(entries)

        self.assertEqual(seen, [])
        self.assertIn("no 'data' field", "\n".join(logs.output))
        self.assertEqual([call[2] for call in fake.acked], ["1-0"])

    def test_a_genuine_bug_still_raises(self) -> None:
        """
        The guard must be narrow. A real defect in a consumer's model is
        not 'a malformed message' and must not be quietly swallowed —
        CLAUDE.md's no-silent-swallow rule.
        """
        entries = [_entry("1-0", '{"text": "hello"}')]

        with self.assertRaises(RuntimeError):
            self._consume(entries, model_cls=_Exploding)


class MemoryWriterSurvivesDatabaseFailureTests(unittest.TestCase):
    """The writer died mid-run on 2026-10-06. It must not, whatever the
    cause: a transient DB error is the leading suspect, a poison message
    the other, and both are covered here."""

    def setUp(self) -> None:
        import memory.episodic.writer as writer

        self.writer = writer
        self._orig_wait = writer._write.retry.wait
        writer._write.retry.wait = wait_none()  # no real sleeping in tests
        self.addCleanup(self._restore_wait)

    def _restore_wait(self) -> None:
        self.writer._write.retry.wait = self._orig_wait

    def _run(self, write_side_effect, entries=None):
        import contracts

        entries = entries or [("1-0", mock.Mock(spec=contracts.Percept))]
        fake_bus = mock.Mock()
        fake_bus.consume.return_value = iter(entries)

        memory = mock.Mock()
        memory.write.side_effect = write_side_effect

        out = io.StringIO()
        with (
            mock.patch.object(self.writer, "EventBus", return_value=fake_bus),
            mock.patch.object(self.writer, "EpisodicMemory", return_value=memory),
            contextlib.redirect_stdout(out),
        ):
            self.writer.main()

        return fake_bus, memory, out.getvalue()

    def test_a_transient_failure_does_not_kill_the_writer(self) -> None:
        self._run(psycopg.OperationalError("connection timeout expired"))
        # Reaching here at all is the assertion: main() returned instead of
        # propagating.

    def test_the_percept_is_not_acked_when_the_write_fails(self) -> None:
        fake_bus, _, _ = self._run(psycopg.OperationalError("server closed"))

        fake_bus.ack.assert_not_called()
        self.assertEqual(
            fake_bus.ack.call_count,
            0,
            "an unwritten percept must stay in the PEL and be re-delivered "
            "on restart — losing observations silently is the one thing a "
            "memory service must not do",
        )

    def test_the_write_is_retried_before_giving_up(self) -> None:
        _, memory, _ = self._run(psycopg.OperationalError("busy"))
        self.assertEqual(memory.write.call_count, 3)

    def test_a_successful_write_is_not_retried(self) -> None:
        _, memory, _ = self._run(None)
        self.assertEqual(memory.write.call_count, 1)

    def test_a_non_connection_error_is_not_retried(self) -> None:
        """
        Only OperationalError is transient. A wrong statement is a bug;
        retrying it three times would just make the bug slower and noisier.
        """
        with self.assertRaises(psycopg.ProgrammingError):
            self._run(psycopg.ProgrammingError("syntax error"))

    def test_failure_is_reported_once_then_recovery(self) -> None:
        """One warning per outage, not one per percept — the stream carries
        several a second."""
        calls = {"n": 0}

        def flaky(percept):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] <= 6:  # two failing percepts, 3 attempts each
                raise psycopg.OperationalError("down")

        entries = [
            ("1-0", mock.Mock()),
            ("2-0", mock.Mock()),
            ("3-0", mock.Mock()),
        ]
        fake_bus, _, text = self._run(flaky, entries=entries)

        self.assertEqual(text.count("WARNING:"), 1)
        self.assertIn("recovered after", text)
        self.assertEqual(
            [call.args[2] for call in fake_bus.ack.call_args_list],
            ["3-0"],
            "only the percept that actually reached Postgres may be acked",
        )


class ConnectTimeoutTests(unittest.TestCase):
    """The 2 s timeout caused a false 'Postgres is not reachable' in
    session 6 and is the leading suspect for the writer death in session 8."""

    def test_timeout_is_above_the_value_that_failed(self) -> None:
        from memory.episodic import store

        self.assertGreater(
            store._CONNECT_TIMEOUT,
            2,
            "2 s is documented as too tight on this 5400 RPM disk — see the "
            "comment on the constant before lowering it",
        )

    def test_every_connect_site_uses_the_constant(self) -> None:
        from memory.episodic import store

        source = (REPO_ROOT / "memory" / "episodic" / "store.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        connects = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
        ]
        self.assertEqual(len(connects), 4, "expected four psycopg.connect sites")
        for call in connects:
            keywords = {k.arg for k in call.keywords}
            self.assertIn("connect_timeout", keywords)

    def test_preflight_shares_the_constant_rather_than_a_literal(self) -> None:
        """Two independent literals is how these drifted too tight together
        in the first place."""
        source = (REPO_ROOT / "run_aura.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        pg_connects = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "psycopg"
        ]
        self.assertEqual(len(pg_connects), 1, "expected one psycopg.connect")

        timeout = next(
            (k.value for k in pg_connects[0].keywords if k.arg == "connect_timeout"),
            None,
        )
        self.assertIsInstance(
            timeout,
            ast.Name,
            "the preflight must use the store's constant, not its own "
            "literal — that is how the two timers drifted apart before",
        )
        self.assertEqual(timeout.id, "_CONNECT_TIMEOUT")


class FailedExchangeDoesNotEndTheSessionTests(unittest.TestCase):
    """Pressing Enter at the wrong moment must not shut AURA down."""

    def _exchange_that_fails(self, failing_at: str):
        import bodies.desktop.conversation as conversation

        transcriber = mock.Mock()
        transcriber.transcribe.return_value = "what do you see"

        patches = {
            "answer": mock.patch.object(
                conversation,
                "answer_question",
                side_effect=psycopg.OperationalError("postgres is slow"),
            ),
            "record": mock.patch.object(
                conversation, "record_utterance", side_effect=OSError("no microphone")
            ),
        }

        out = io.StringIO()
        with (
            # One blank line: the first input() reads it as an Enter press,
            # the second hits EOF and ends the loop. Exactly one exchange.
            mock.patch.object(conversation.sys, "stdin", io.StringIO("\n")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(
                conversation, "default_transcription_provider", return_value=transcriber
            ),
            patches[failing_at],
        ):
            try:
                conversation.run_conversation(
                    mock.Mock(), conversation.InteractionGate()
                )
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"{type(exc).__name__} escaped run_conversation — one "
                    "failed exchange took the whole of AURA down"
                )

        return out.getvalue()

    def test_a_database_failure_while_answering_is_survivable(self) -> None:
        text = self._exchange_that_fails("answer")
        self.assertIn("still running", text)

    def test_a_microphone_failure_is_survivable(self) -> None:
        text = self._exchange_that_fails("record")
        self.assertIn("still running", text)


class GreetingFailureDoesNotKillTheBodyTests(unittest.TestCase):
    """The greeting is LLM-generated; a rate limit at startup used to be
    fatal before the conversation loop was ever reached."""

    def test_a_failed_greeting_still_starts_the_body(self) -> None:
        import bodies.desktop.run as body

        speaker = mock.Mock()
        out = io.StringIO()
        with (
            mock.patch.object(
                body,
                "generate_introduction",
                side_effect=RuntimeError("gemini is down"),
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            body._introduce(speaker)

        self.assertIn("starting anyway", out.getvalue())
        speaker.speak.assert_not_called()

    def test_a_working_greeting_is_spoken(self) -> None:
        import bodies.desktop.run as body

        speaker = mock.Mock()
        intro = mock.Mock()
        intro.params = {"text": "Hello, I am AURA."}

        out = io.StringIO()
        with (
            mock.patch.object(body, "generate_introduction", return_value=intro),
            contextlib.redirect_stdout(out),
        ):
            body._introduce(speaker)

        self.assertIn("Hello, I am AURA.", out.getvalue())
        speaker.speak.assert_called_once_with("Hello, I am AURA.")


if __name__ == "__main__":
    unittest.main()
