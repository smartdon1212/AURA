"""
Tests for the web Body.

Three things are worth testing here, and they are the three that would be
expensive to find by hand:

  - **The gate policy survived the move and the second Body.** Ambient
    narration is dropped, not queued, while an exchange is active — and
    the dropped action is still acked, because leaving it unacked parks it
    in the consumer group's PEL and redelivers it later, which is queuing
    under a different name. Both Bodies now share one InteractionGate, and
    the desktop Body must still be able to import it from where it always
    did.

  - **The vision loop is still perception/service.py's loop.** Same
    bounded camera-failure handling (G-6), same `source_body_id` -> body
    routing. The web Body is a new file, and a new file is exactly where
    a fix that was already paid for gets quietly lost.

  - **`--web` really does swap the Body and drop perception.** Two
    processes cannot both hold the camera on Windows, so if this is wrong
    the failure is a camera that opens for one service and not the other.

Nothing here starts a camera, a YOLO model, Redis, Postgres or Gemini.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import threading
import unittest
from pathlib import Path
from unittest import mock

from contracts import Action, ActionIntent, ActionPriority, Percept, PerceptType

REPO_ROOT = Path(__file__).resolve().parent.parent

WEB_BODY_ID = "web-01"


class _Stop(Exception):
    """Ends a consumer generator in a test without waiting for a block."""


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


class GateMoveTests(unittest.TestCase):
    def test_the_desktop_body_still_imports_the_gate_it_always_did(self) -> None:
        """tests/test_interaction_gate.py imports it from here, and so does
        run.py. The move must be invisible to both."""
        from bodies.desktop.conversation import InteractionGate as FromConversation
        from bodies.gate import InteractionGate as FromGate

        self.assertIs(FromConversation, FromGate)

    def test_only_one_definition_exists(self) -> None:
        """Two copies is how the desktop and web Bodies drift apart on the
        drop-don't-queue policy without anyone noticing."""
        source = (
            REPO_ROOT / "bodies" / "desktop" / "conversation.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        ]
        self.assertNotIn(
            "InteractionGate",
            classes,
            "conversation.py must re-export InteractionGate, not redefine it",
        )

    def test_begin_and_end_open_and_close_an_exchange(self) -> None:
        from bodies.gate import InteractionGate

        gate = InteractionGate()
        self.assertFalse(gate.is_active)

        gate.begin_exchange()
        self.assertTrue(gate.is_active)

        gate.end_exchange()
        self.assertFalse(gate.is_active)

    def test_a_fresh_exchange_is_not_expired(self) -> None:
        from bodies.gate import InteractionGate

        gate = InteractionGate()
        gate.begin_exchange()

        self.assertFalse(gate.expire_stale_exchange(max_age_seconds=120))
        self.assertTrue(gate.is_active)

    def test_a_stale_exchange_is_released(self) -> None:
        """
        A tab closed mid-answer leaves the flag set. Without the expiry
        that suppresses narration for the rest of the session, and the
        only symptom is AURA quietly stopping commenting on anything.
        """
        from bodies.gate import InteractionGate

        gate = InteractionGate()
        gate.begin_exchange()

        self.assertTrue(gate.expire_stale_exchange(max_age_seconds=0))
        self.assertFalse(gate.is_active)

    def test_expiring_an_idle_gate_is_not_an_event(self) -> None:
        from bodies.gate import InteractionGate

        gate = InteractionGate()
        self.assertFalse(gate.expire_stale_exchange(max_age_seconds=0))

    def test_the_context_manager_still_sets_and_clears(self) -> None:
        """exchange() is now built on begin/end; its behaviour must not
        have changed for the desktop Body."""
        from bodies.gate import InteractionGate

        gate = InteractionGate()
        with gate.exchange():
            self.assertTrue(gate.is_active)
        self.assertFalse(gate.is_active)


# --------------------------------------------------------------------------
# Narration policy
# --------------------------------------------------------------------------


class _FakeBus:
    def __init__(self, entries) -> None:
        self._entries = entries
        self.acked: list[str] = []

    def consume(self, stream, group, consumer, model):  # noqa: ANN001, ANN201
        for entry_id, action in self._entries:
            yield entry_id, action
        raise _Stop()

    def ack(self, stream, group, entry_id) -> None:  # noqa: ANN001
        self.acked.append(entry_id)


def _speak(text: str = "there is a cup") -> Action:
    return Action(
        target_body_id=WEB_BODY_ID,
        intent=ActionIntent.SPEAK,
        params={"text": text},
        priority=ActionPriority.NORMAL,
    )


class NarrationPolicyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _subscribe(state):
        """A subscriber is primed with the current state on connect, so the
        first thing in the queue is never the event under test."""
        sub = state.subscribe()
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                sub.queue.get_nowait()
        return sub

    async def _run_narration(self, actions, gate, state):
        from bodies.web.narration import NarrationService

        bus = _FakeBus([(f"{i}-0", a) for i, a in enumerate(actions)])
        service = NarrationService(state, gate, bus=bus)

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(_Stop):
                service.run()

        # broadcast() hands work to the loop through call_soon_threadsafe;
        # let the loop run the callbacks before reading the queues.
        await asyncio.sleep(0)
        return bus

    async def test_narration_reaches_the_browser_when_idle(self) -> None:
        from bodies.gate import InteractionGate
        from bodies.web.state import AuraState

        state = AuraState()
        sub = self._subscribe(state)

        bus = await self._run_narration([_speak("a cup")], InteractionGate(), state)

        event, data = sub.queue.get_nowait()
        self.assertEqual(event, "narration")
        self.assertEqual(data["text"], "a cup")
        self.assertFalse(data["suppressed"])
        self.assertEqual(bus.acked, ["0-0"])

    async def test_narration_is_dropped_but_still_acked_during_an_exchange(
        self,
    ) -> None:
        """
        The agreed policy: dropped, not queued. Acked, because an unacked
        action sits in the PEL and is redelivered after the exchange — the
        same thing as queuing, one Redis restart later.
        """
        from bodies.gate import InteractionGate
        from bodies.web.state import AuraState

        state = AuraState()
        sub = self._subscribe(state)

        gate = InteractionGate()
        gate.begin_exchange()
        bus = await self._run_narration([_speak("a chair")], gate, state)

        event, data = sub.queue.get_nowait()
        self.assertEqual(event, "narration")
        self.assertTrue(
            data["suppressed"],
            "narration during an exchange must be marked suppressed",
        )
        self.assertEqual(
            bus.acked,
            ["0-0"],
            "a suppressed action must still be acked — leaving it unacked "
            "puts it in the PEL and redelivers it later, which is queuing",
        )

    async def test_a_busy_exchange_is_released_before_the_next_narration(
        self,
    ) -> None:
        """The expiry has to run on the path where it matters: the moment
        narration arrives and finds the gate still held."""
        from bodies.gate import InteractionGate
        from bodies.web.state import AuraState

        state = AuraState()
        gate = InteractionGate()
        gate.begin_exchange()

        bus = _FakeBus([])
        from bodies.web.narration import NarrationService

        service = NarrationService(state, gate, bus=bus)

        # One action, with the exchange already older than the limit.
        bus._entries = [("0-0", _speak("a lamp"))]
        with mock.patch.object(
            gate, "expire_stale_exchange", wraps=gate.expire_stale_exchange
        ) as expiry:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(_Stop):
                    service.run()

        expiry.assert_called()

    async def test_a_non_speak_action_is_acked_and_not_broadcast(self) -> None:
        from bodies.gate import InteractionGate
        from bodies.web.state import AuraState

        state = AuraState()
        sub = self._subscribe(state)

        other = Action(
            target_body_id=WEB_BODY_ID,
            intent=ActionIntent.EXPRESS,
            params={"emotion": "curious"},
            priority=ActionPriority.NORMAL,
        )
        bus = await self._run_narration([other], InteractionGate(), state)

        self.assertEqual(bus.acked, ["0-0"])
        # Acked, and therefore gone: an unacked action would sit in the
        # consumer group's PEL and be redelivered later, which is queuing
        # under a different name. Nothing is broadcast either — the page
        # has no UI for an EXPRESS action, so it would be a silent no-op
        # event. `_subscribe` already drained the priming event.
        self.assertEqual(sub.queue.qsize(), 0)


# --------------------------------------------------------------------------
# Vision
# --------------------------------------------------------------------------


class _FakeCamera:
    def __init__(self, frames) -> None:
        self._frames = list(frames)
        self.reads = 0
        self.released = False

    def read_frame(self):
        self.reads += 1
        return self._frames.pop(0) if self._frames else None

    def release(self) -> None:
        self.released = True


class _EchoDetector:
    """Returns one Percept per call, stamped with the body id it was given
    — which is what the Mind routes the reply on."""

    def __init__(self, body_id: str) -> None:
        self.body_id = body_id

    def detect(self, frame):  # noqa: ANN001
        return [
            Percept(
                source_body_id=self.body_id,
                type=PerceptType.OBJECT_DETECTED,
                confidence=0.9,
                payload={"label": "cup", "bbox": [4.0, 4.0, 24.0, 24.0]},
            )
        ]


class _FakeBusPublish:
    def __init__(self) -> None:
        self.published: list[Percept] = []

    def publish(self, stream, percept) -> None:  # noqa: ANN001
        self.published.append(percept)


def _frame():
    import numpy as np

    return np.zeros((48, 64, 3), dtype=np.uint8)


class VisionLoopTests(unittest.TestCase):
    def _run(self, camera, detector, state=None):
        from bodies.web.state import AuraState
        from bodies.web.vision import VisionService

        state = state or AuraState()
        bus = _FakeBusPublish()
        service = VisionService(state, camera=camera, detector=detector, bus=bus)

        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            mock.patch("bodies.web.vision.MAX_CONSECUTIVE_FAILURES", 2),
            mock.patch("bodies.web.vision.FAILURE_SLEEP_SECONDS", 0),
        ):
            service.run()
        return service, bus, out.getvalue()

    def test_percepts_are_published_under_the_web_body_id(self) -> None:
        """
        This is the whole reason the web Body needed no change to the Mind:
        mind/reasoning_loop.py publishes to actions_stream(percept
        .source_body_id), so 'web-01' in means actions:web-01 out.
        """
        from bodies.web.vision import BODY_ID

        camera = _FakeCamera([_frame()])
        _, bus, _ = self._run(camera, _EchoDetector(BODY_ID))

        self.assertEqual(len(bus.published), 1)
        self.assertEqual(bus.published[0].source_body_id, WEB_BODY_ID)
        self.assertEqual(BODY_ID, WEB_BODY_ID)

    def test_the_default_detector_is_built_with_this_body_id(self) -> None:
        """
        The behavioural test above injects a detector, so it cannot see
        which id the *default* construction uses. Read it instead of
        importing it — perception.detector pulls in torch and ultralytics,
        which is exactly what bodies/web/vision.py defers.

        If this drifts, AURA answers into actions:desktop-01 from the web
        page and nothing is ever heard — a silent failure with no error.
        """
        source = (REPO_ROOT / "bodies" / "web" / "vision.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        built = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ObjectDetector"
        ]
        self.assertEqual(len(built), 1)
        keyword = next(
            (k.value for k in built[0].keywords if k.arg == "source_body_id"), None
        )
        self.assertIsInstance(keyword, ast.Attribute)
        self.assertEqual(keyword.attr, "body_id")

    def test_a_dead_camera_gives_up_and_says_so_once(self) -> None:
        """G-6, reused: bounded failures, one warning per run, no spin."""
        camera = _FakeCamera([])

        from bodies.web.state import AuraState

        state = AuraState()
        with mock.patch.object(state, "broadcast") as broadcast:
            service, bus, text = self._run(
                camera, _EchoDetector(WEB_BODY_ID), state=state
            )

        self.assertEqual(camera.reads, 2, "it must stop at the bound")
        self.assertTrue(service.stopped)
        self.assertTrue(camera.released)
        self.assertEqual(bus.published, [])
        self.assertEqual(
            text.count("Warning: camera read failed"),
            1,
            "one warning per failure run, not one per frame",
        )
        self.assertIn("no frame", text)

        errors = [c for c in broadcast.call_args_list if c.args[0] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("camera", errors[0].args[1]["message"].lower())

    def test_the_camera_is_released_even_when_the_loop_fails(self) -> None:
        """A camera held open by a crashed loop locks out the desktop
        perception service until the process dies."""
        camera = _FakeCamera([_frame(), _frame()])
        detector = mock.Mock()
        detector.detect.side_effect = RuntimeError("model exploded")

        from bodies.web.state import AuraState

        state = AuraState()
        with (
            mock.patch.object(state, "broadcast") as broadcast,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            service, _, _ = self._run(camera, detector, state=state)

        self.assertTrue(camera.released)
        self.assertTrue(
            service.stopped,
            "a crashed vision loop must not keep claiming to be watching",
        )
        self.assertTrue(
            any(c.args[0] == "error" for c in broadcast.call_args_list),
            "the browser has to be told, or AURA silently goes blind",
        )


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


def _web_body(transcriber=None, answerer=None):
    from bodies.gate import InteractionGate
    from bodies.web.body import WebBody
    from bodies.web.state import AuraState
    from bodies.web.vision import VisionService

    state = AuraState()
    return WebBody(
        state=state,
        gate=InteractionGate(),
        vision=VisionService(state),
        transcriber=transcriber or mock.Mock(),
        answerer=answerer or mock.Mock(),
    )


class AskEndpointTests(unittest.TestCase):
    AUDIO = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(range(64))

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from bodies.web.app import create_app

        self.transcriber = mock.Mock()
        self.transcriber.transcribe.return_value = "what do you see"

        action = mock.Mock()
        action.params = {"text": "a cup and a lamp"}
        self.answerer = mock.Mock(return_value=action)

        self.body = _web_body(self.transcriber, self.answerer)
        self.client = TestClient(create_app(self.body))

    def test_the_posted_bytes_reach_the_transcriber_unmodified(self) -> None:
        """
        The WAV the browser builds is handed straight to Gemini. If
        anything between here and there re-encodes or truncates it, the
        transcribe call starts returning empty strings, and the symptom
        looks like a broken microphone.
        """
        response = self.client.post(
            "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
        )

        self.assertEqual(response.status_code, 200)
        self.transcriber.transcribe.assert_called_once()
        sent = self.transcriber.transcribe.call_args.args[0]
        self.assertEqual(sent, self.AUDIO)

    def test_the_answer_comes_back_with_the_transcript(self) -> None:
        response = self.client.post(
            "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
        )
        payload = response.json()

        self.assertEqual(payload["transcript"], "what do you see")
        self.assertEqual(payload["text"], "a cup and a lamp")
        self.answerer.assert_called_once_with(
            "what do you see", target_body_id=WEB_BODY_ID
        )

    def test_an_empty_body_is_rejected_rather_than_transcribed(self) -> None:
        response = self.client.post("/ask", content=b"")
        self.assertEqual(response.status_code, 400)
        self.transcriber.transcribe.assert_not_called()

    def test_a_failure_is_reported_to_the_browser(self) -> None:
        """The user pressed a button. Silently returning nothing would
        leave them watching an orb that never answers."""
        self.transcriber.transcribe.side_effect = RuntimeError("gemini is down")

        with contextlib.redirect_stderr(io.StringIO()):
            response = self.client.post(
                "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("gemini is down", response.json()["error"])

    def test_the_state_returns_to_observing_after_answering(self) -> None:
        self.client.post(
            "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
        )
        self.assertEqual(self.body.state.state, "observing")

    def test_a_browser_held_exchange_outlives_the_answer(self) -> None:
        """
        The page opens the exchange before recording and closes it once
        speech finishes. If answering ended it, narration could talk over
        the reply while the browser was still reading it out.
        """
        self.client.post("/exchange/begin")
        self.assertEqual(self.body.gate.is_active, True)

        self.client.post(
            "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
        )
        self.assertTrue(
            self.body.gate.is_active,
            "the answer must not close an exchange the browser is holding",
        )

        self.client.post("/exchange/end")
        self.assertFalse(self.body.gate.is_active)

    def test_an_answer_without_an_exchange_is_still_protected(self) -> None:
        """A client that skipped /exchange/begin must not let narration
        talk over the question."""
        from fastapi.testclient import TestClient

        from bodies.web.app import create_app

        seen: list[bool] = []
        action = mock.Mock()
        action.params = {"text": "an answer"}
        holder: dict = {}

        def answerer(question, target_body_id):  # noqa: ANN001
            seen.append(holder["body"].gate.is_active)
            return action

        body = _web_body(self.transcriber, answerer)
        holder["body"] = body
        client = TestClient(create_app(body))

        client.post(
            "/ask", content=self.AUDIO, headers={"Content-Type": "audio/wav"}
        )

        self.assertEqual(seen, [True], "the question must be asked with the gate held")
        self.assertFalse(body.gate.is_active, "and released afterwards")


# --------------------------------------------------------------------------
# The launcher
# --------------------------------------------------------------------------


class LauncherWebModeTests(unittest.TestCase):
    def _targets_in_web_branch(self) -> set[str]:
        """
        Reads the `targets` dict out of the `if args.web:` branch of
        main(), without running it.
        """
        source = (REPO_ROOT / "run_aura.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        def is_args_web(node: ast.expr) -> bool:
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "web"
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
            )

        branches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and is_args_web(node.test)
            and any(
                isinstance(inner, ast.Assign)
                and isinstance(inner.targets[0], ast.Name)
                and inner.targets[0].id == "targets"
                for inner in node.body
            )
        ]
        self.assertEqual(len(branches), 1, "expected one `if args.web:` branch")

        assignment = next(
            inner
            for inner in branches[0].body
            if isinstance(inner, ast.Assign)
            and isinstance(inner.targets[0], ast.Name)
            and inner.targets[0].id == "targets"
        )
        return {key.value for key in assignment.value.keys}

    def test_web_mode_drops_perception_and_the_desktop_body(self) -> None:
        """
        Windows gives OpenCV exclusive access to the camera. If perception
        and the web Body both start, one of them gets no frames — and it
        is not obviously which.
        """
        targets = self._targets_in_web_branch()

        self.assertEqual(targets, {"memory-writer", "mind", "web-body"})
        self.assertNotIn("perception", targets)

    def test_desktop_mode_is_unchanged(self) -> None:
        source = (REPO_ROOT / "run_aura.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigned = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "targets"
            and isinstance(node.value, ast.Dict)
            and "perception" in {k.value for k in node.value.keys}
        ]
        self.assertEqual(len(assigned), 1, "desktop targets dict must still exist")
        self.assertEqual(
            {k.value for k in assigned[0].value.keys},
            {"perception", "memory-writer", "mind"},
        )

    def test_the_flag_parses(self) -> None:
        import run_aura

        self.assertTrue(run_aura._parse_args(["--web"]).web)
        self.assertFalse(run_aura._parse_args([]).web)


# --------------------------------------------------------------------------
# The SSE fan-out
# --------------------------------------------------------------------------


class AuraStateFanOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_subscriber_is_primed_with_the_current_state(self) -> None:
        """A page opened while AURA is thinking must show thinking, not sit
        on the orb's default until something changes — which, if she has
        gone quiet, may be a long time."""
        from bodies.web.state import AuraState

        state = AuraState()
        state.set_state("thinking")

        sub = state.subscribe()
        event, data = sub.queue.get_nowait()

        self.assertEqual(event, "state")
        self.assertEqual(data["state"], "thinking")

    async def test_every_subscriber_gets_every_event(self) -> None:
        from bodies.web.state import AuraState

        state = AuraState()
        first = state.subscribe()
        second = state.subscribe()
        first.queue.get_nowait()  # discard the priming event
        second.queue.get_nowait()

        state.broadcast("narration", {"text": "a cup", "suppressed": False})
        await asyncio.sleep(0)

        self.assertEqual(first.queue.get_nowait()[1]["text"], "a cup")
        self.assertEqual(second.queue.get_nowait()[1]["text"], "a cup")

    async def test_an_unsubscribed_client_stops_receiving(self) -> None:
        from bodies.web.state import AuraState

        state = AuraState()
        sub = state.subscribe()
        sub.queue.get_nowait()

        state.unsubscribe(sub)
        state.broadcast("narration", {"text": "a cup", "suppressed": False})
        await asyncio.sleep(0)

        self.assertTrue(sub.queue.empty())
        self.assertEqual(state.subscriber_count, 0)

    async def test_a_stalled_client_cannot_stall_aura(self) -> None:
        """
        A throttled background tab fills its queue. The producer is the
        vision thread, which must never block on a browser.
        """
        from bodies.web.state import AuraState

        state = AuraState(queue_size=1)
        sub = state.subscribe()
        sub.queue.get_nowait()

        for i in range(20):
            state.broadcast("narration", {"text": f"line {i}", "suppressed": False})

        await asyncio.sleep(0)  # must not raise QueueFull

        self.assertEqual(sub.queue.qsize(), 1)

    async def test_broadcasting_from_another_thread_is_safe(self) -> None:
        """The producers are OS threads; the queues belong to uvicorn's
        loop. This is the whole reason for call_soon_threadsafe."""
        from bodies.web.state import AuraState

        state = AuraState()
        sub = state.subscribe()
        sub.queue.get_nowait()

        thread = threading.Thread(
            target=state.broadcast, args=("narration", {"text": "from a thread"})
        )
        thread.start()
        thread.join()

        await asyncio.sleep(0)
        self.assertEqual(sub.queue.get_nowait()[1]["text"], "from a thread")


if __name__ == "__main__":
    unittest.main()
