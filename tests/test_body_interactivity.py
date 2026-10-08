"""
Tests for the fix to the 2026-10-06 interactivity failure.

In the live run, AURA started, announced itself, began narrating ambient
observations — and then printed:

    Press Enter to talk...
    No input available — conversation loop stopping.

The conversation loop died on the very first prompt because the Body was
launched as one of `run_aura.py`'s spawned child processes, and a spawned
child on Windows does not reliably inherit a usable console stdin. The
structural fix is that the Body now runs on the launcher's *main thread*
and only the three bus services are children.

Two things are asserted here, because either one alone would leave the
failure half-fixed:

  - `run_aura.py` no longer starts the Body as a child, and supervises
    the remaining three from a daemon thread instead of the main thread
    (which the Body now owns).
  - The Body degrades rather than dying: if stdin really is unusable, the
    conversation loop says so with the evidence, and ambient narration
    keeps running.
"""

from __future__ import annotations

import ast
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in {path}")


class LauncherOwnsTheConsoleTests(unittest.TestCase):
    """run_aura.py — the Body must run on the main thread."""

    def setUp(self) -> None:
        self.path = REPO_ROOT / "run_aura.py"
        self.main = _function(self.path, "main")

    def test_body_is_not_a_child_process(self) -> None:
        targets = None
        for node in ast.walk(self.main):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "targets" for t in node.targets
            ):
                targets = node.value

        self.assertIsNotNone(targets, "targets dict not found in main()")
        keys = {
            k.value
            for k in targets.keys  # type: ignore[attr-defined]
            if isinstance(k, ast.Constant)
        }
        self.assertEqual(
            keys,
            {"perception", "memory-writer", "mind"},
            "the Body must not be a spawned child — it needs the console's "
            "stdin, and a spawn child does not reliably have one",
        )

    def test_body_runs_on_the_main_thread_and_supervision_does_not(self) -> None:
        """Both halves matter: if _run_body went to a thread it would block
        nothing, and if supervision stayed on the main thread the Body could
        never start."""
        direct_calls = {
            node.func.id
            for node in ast.walk(self.main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }
        self.assertIn(
            "_run_body",
            direct_calls,
            "the Body must be called directly in main(), i.e. on the main "
            "thread — not handed to a thread or a process",
        )

        thread_targets: set[str] = set()
        started_a_thread = False
        for node in ast.walk(self.main):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Attribute)
                and node.func.value.func.attr == "Thread"
            ):
                started_a_thread = True
                for keyword in node.func.value.keywords:
                    if keyword.arg == "target" and isinstance(
                        keyword.value, ast.Name
                    ):
                        thread_targets.add(keyword.value.id)

        self.assertTrue(started_a_thread, "no thread is started in main()")
        self.assertIn(
            "_supervise",
            thread_targets,
            "supervision must move to a daemon thread — the main thread is "
            "now blocked forever reading stdin for the Body",
        )

    def test_children_are_terminated_on_the_way_out(self) -> None:
        """
        The main thread is now the Body's, so a Ctrl+C there has to be what
        stops the children — previously the supervisor loop caught it.
        """
        source = ast.unparse(self.main)
        self.assertIn("_run_body()", source)

        tries = [n for n in ast.walk(self.main) if isinstance(n, ast.Try)]
        self.assertTrue(tries, "main() has no try block around the Body")
        self.assertTrue(
            any(node.finalbody for node in tries),
            "shutting the children down must be in a finally block — the "
            "Body can now return (dead stdin) as well as be interrupted, "
            "and both have to stop the services",
        )
        self.assertIn("terminate()", source)


class ConversationDegradesGracefullyTests(unittest.TestCase):
    """conversation.py — a dead stdin must be explained, not just announced."""

    def test_dead_stdin_reports_that_it_is_not_a_terminal(self) -> None:
        import bodies.desktop.conversation as conversation

        out = io.StringIO()
        with (
            mock.patch.object(conversation.sys, "stdin", io.StringIO("")),
            contextlib.redirect_stdout(out),
            # The real provider would need a GEMINI_API_KEY; it is never
            # reached, but it is constructed before the loop.
            mock.patch.object(
                conversation, "default_transcription_provider", mock.Mock
            ),
        ):
            conversation.run_conversation(mock.Mock(), conversation.InteractionGate())

        text = out.getvalue()
        self.assertIn("stdin:", text, "the actual stdin object must be shown")
        self.assertIn("isatty=False", text)
        self.assertIn("not a terminal", text)

    def test_no_stdin_at_all_is_called_out(self) -> None:
        import bodies.desktop.conversation as conversation

        out = io.StringIO()
        with (
            mock.patch.object(conversation.sys, "stdin", None),
            contextlib.redirect_stdout(out),
        ):
            conversation._explain_dead_stdin()

        self.assertIn("no stdin at all", out.getvalue())

    def test_a_real_terminal_going_away_says_so(self) -> None:
        """The third case must not be reported as the second."""
        import bodies.desktop.conversation as conversation

        terminal = mock.Mock()
        terminal.isatty.return_value = True

        out = io.StringIO()
        with (
            mock.patch.object(conversation.sys, "stdin", terminal),
            contextlib.redirect_stdout(out),
        ):
            conversation._explain_dead_stdin()

        text = out.getvalue()
        self.assertIn("isatty=True", text)
        self.assertNotIn("not a terminal", text)

    def test_the_loop_returns_rather_than_raising(self) -> None:
        """
        Regression: EOFError must not escape run_conversation, or it takes
        run_aura.py's finally-block path and stops the whole of AURA.
        """
        import bodies.desktop.conversation as conversation

        with (
            mock.patch.object(conversation.sys, "stdin", io.StringIO("")),
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(
                conversation, "default_transcription_provider", mock.Mock
            ),
        ):
            try:
                conversation.run_conversation(
                    mock.Mock(), conversation.InteractionGate()
                )
            except EOFError:
                self.fail("EOFError escaped run_conversation")


class BodySurvivesALostKeyboardTests(unittest.TestCase):
    """run.py — losing stdin must not also lose ambient narration."""

    def test_main_joins_the_listener_after_conversation_ends(self) -> None:
        path = REPO_ROOT / "bodies" / "desktop" / "run.py"
        main = _function(path, "main")

        calls = [
            node.value.func
            for node in main.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        names = [
            func.attr
            if isinstance(func, ast.Attribute)
            else getattr(func, "id", "")
            for func in calls
        ]
        self.assertIn("run_conversation", names)
        self.assertIn(
            "join",
            names,
            "run_conversation returning must not end the process — the "
            "ambient narration thread is still running",
        )
        self.assertLess(
            names.index("run_conversation"),
            names.index("join"),
            "the listener is joined after the conversation loop returns",
        )

    def test_stdin_reexport_is_available(self) -> None:
        """_explain_dead_stdin reads sys.stdin; conversation.py must import sys."""
        import bodies.desktop.conversation as conversation

        self.assertTrue(hasattr(conversation, "sys"))
        self.assertIs(conversation.sys, sys)


if __name__ == "__main__":
    unittest.main()
