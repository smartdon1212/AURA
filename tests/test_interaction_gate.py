"""
Tests for the desktop Body's interaction policy (punch-list item 3).

Covers the two rules CLAUDE.md pins down for the merged Body:
  - while an exchange is active, ambient narration is dropped, not queued;
  - narration and answers never speak at the same time.

No hardware, no Redis and no LLM are involved: the bus and the speaker are
both stubbed, so this exercises the coordination logic on its own.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bodies.desktop import run as body_run  # noqa: E402
from bodies.desktop.conversation import InteractionGate  # noqa: E402
from contracts import Action, ActionIntent  # noqa: E402


class FakeSpeaker:
    """Records what was spoken, and can detect overlapping speak() calls."""

    def __init__(self, speak_seconds: float = 0.0) -> None:
        self.spoken: list[str] = []
        self.max_concurrent = 0
        self._speak_seconds = speak_seconds
        self._concurrent = 0
        self._state = threading.Lock()

    def speak(self, text: str) -> None:
        with self._state:
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            if self._speak_seconds:
                time.sleep(self._speak_seconds)
        finally:
            with self._state:
                self._concurrent -= 1
        self.spoken.append(text)


class FakeBus:
    """Yields a fixed list of entries, then ends — so the consumer returns."""

    def __init__(self, entries) -> None:
        self._entries = entries
        self.acked: list[str] = []

    def consume(self, stream, group, consumer, model_cls):
        yield from self._entries

    def ack(self, stream, group, entry_id) -> None:
        self.acked.append(entry_id)


def _speak_action(text: str) -> Action:
    return Action(
        target_body_id="desktop-01",
        intent=ActionIntent.SPEAK,
        params={"text": text},
    )


def _drain_consumer(speaker, gate, entries):
    """Runs the real consumer loop against a stubbed bus. Returns the bus."""
    bus = FakeBus(entries)
    with mock.patch.object(body_run, "EventBus", return_value=bus):
        body_run._consume_actions(speaker, gate)
    return bus


class AmbientNarrationTests(unittest.TestCase):
    def test_narration_speaks_and_acks_when_idle(self) -> None:
        bus = _drain_consumer(
            FakeSpeaker(), InteractionGate(), [("1-0", _speak_action("a cup"))]
        )
        self.assertEqual(bus.acked, ["1-0"])

    def test_narration_is_dropped_and_still_acked_during_exchange(self) -> None:
        """
        The core policy: suppressed means dropped, not queued. The ack is
        the part that makes the drop permanent — without it the entry stays
        in the consumer group's PEL and comes back later.
        """
        speaker = FakeSpeaker()
        gate = InteractionGate()

        with gate.exchange():
            bus = _drain_consumer(speaker, gate, [("1-0", _speak_action("a cup"))])

        self.assertEqual(speaker.spoken, [])
        self.assertEqual(bus.acked, ["1-0"])

    def test_narration_does_not_block_behind_an_active_exchange(self) -> None:
        """
        Regression: taking the speak lock before testing the flag makes an
        ambient call wait out the whole exchange and then speak. That is
        queuing under another name, and it was the first version of this
        code.
        """
        gate = InteractionGate()
        observed: list[bool] = []

        def drain() -> None:
            with gate.ambient_speech() as should_speak:
                observed.append(should_speak)

        with gate.exchange():
            worker = threading.Thread(target=drain, daemon=True)
            worker.start()
            worker.join(timeout=2.0)

            self.assertFalse(
                worker.is_alive(),
                "ambient_speech() blocked while an exchange was active",
            )

        self.assertEqual(observed, [False])

    def test_narration_resumes_once_the_exchange_ends(self) -> None:
        gate = InteractionGate()
        with gate.exchange():
            pass
        with gate.ambient_speech() as should_speak:
            self.assertTrue(should_speak)

    def test_non_speak_actions_are_acked_without_speaking(self) -> None:
        speaker = FakeSpeaker()
        action = Action(
            target_body_id="desktop-01",
            intent=ActionIntent.EXPRESS,
            params={},
        )
        bus = _drain_consumer(speaker, InteractionGate(), [("9-0", action)])
        self.assertEqual(speaker.spoken, [])
        self.assertEqual(bus.acked, ["9-0"])

    def test_two_narrations_never_speak_concurrently(self) -> None:
        """Speaker.speak() drives one SAPI5 voice; overlapping calls drop one."""
        gate = InteractionGate()
        speaker = FakeSpeaker(speak_seconds=0.05)
        entries = [("1-0", _speak_action("one")), ("2-0", _speak_action("two"))]

        workers = [
            threading.Thread(
                target=_drain_consumer, args=(speaker, gate, entries), daemon=True
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5.0)

        self.assertEqual(speaker.max_concurrent, 1)
        self.assertEqual(sorted(speaker.spoken), ["one", "one", "two", "two"])


class FlushStdinTests(unittest.TestCase):
    def test_flush_stdin_is_safe_when_no_console_is_attached(self) -> None:
        """
        Under a test runner stdin is not an interactive console, so this
        must be a no-op rather than raising.
        """
        from bodies.desktop.conversation import _flush_stdin

        _flush_stdin()


if __name__ == "__main__":
    unittest.main()
