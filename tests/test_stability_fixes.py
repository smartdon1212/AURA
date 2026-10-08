"""
Tests for the stabilization fixes — plan steps 5 through 9.

These cover the failure that actually took AURA down in a live run on
2026-10-06: `httpx.RemoteProtocolError: Server disconnected without
sending a response` propagating out of GeminiProvider.generate() and
killing the process on the question "what is your name".

Two things learned in that run are encoded here, because both contradict
the original plan:

1. `httpx.RemoteProtocolError` is **not** a subclass of `ConnectError` or
   `TimeoutException`, so step 7's originally-planned exception tuple
   would have missed the exact failure observed. The catch is
   `httpx.TransportError`, the common base.
2. The SDK's own retry (step 5) does **not** cover `RemoteProtocolError`
   either — `_HTTPX_TRANSIENT_EXC` is only `TimeoutException` and
   `ConnectError`. Step 5 is still worth having, but it is step 7's
   per-model fallback that survives the observed case.

`perception.detector` is stubbed into `sys.modules` before importing
`perception.service`: the real detector imports ultralytics and torch,
which cost minutes to import on this machine's 5400 RPM disk and are
irrelevant to the loop logic under test.
"""

from __future__ import annotations

import ast
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

# Stub the detector before anything imports perception.service, so the
# ultralytics/torch import never happens.
_detector_stub = types.ModuleType("perception.detector")


class _StubDetector:
    def __init__(self, source_body_id: str | None = None) -> None:
        self.source_body_id = source_body_id

    def detect(self, frame):  # noqa: ANN001, ANN201 - test double
        return []


_detector_stub.ObjectDetector = _StubDetector


def _stub_select_model_weights(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """Only needed because perception/__init__.py re-exports it."""
    return None


_detector_stub.select_model_weights = _stub_select_model_weights
sys.modules.setdefault("perception.detector", _detector_stub)

# Same reasoning for voice: perception/__init__.py imports it, and the real
# module pulls in audio devices that no test here needs.
_voice_stub = types.ModuleType("perception.voice")
_voice_stub.record_utterance = lambda *a, **k: None  # noqa: E731
sys.modules.setdefault("perception.voice", _voice_stub)


class Step5SdkRetryTests(unittest.TestCase):
    """Step 5 — the SDK's retry is off by default and must be switched on."""

    def test_client_is_constructed_with_retry_options(self) -> None:
        from mind.llm_provider.gemini_provider import GeminiProvider

        with mock.patch(
            "mind.llm_provider.gemini_provider.genai.Client"
        ) as client_cls:
            GeminiProvider(api_key="test-key")

        http_options = client_cls.call_args.kwargs.get("http_options")
        self.assertIsNotNone(
            http_options,
            "genai.Client was constructed without http_options — the SDK's "
            "retry stays off (stop_after_attempt(1)) and a transient "
            "transport failure reaches the user immediately.",
        )
        self.assertEqual(http_options.retry_options.attempts, 3)


class Step7TransportFallbackTests(unittest.TestCase):
    """Step 7 — a transport failure on one model must advance to the next."""

    def _provider(self, models: list[str]):
        from mind.llm_provider.gemini_provider import GeminiProvider

        with mock.patch("mind.llm_provider.gemini_provider.genai.Client"):
            return GeminiProvider(model=models, api_key="test-key")

    def test_remote_protocol_error_is_a_transport_error(self) -> None:
        """
        The regression at the heart of this file: the observed failure is
        a TransportError but not a ConnectError/TimeoutException.
        """
        self.assertTrue(issubclass(httpx.RemoteProtocolError, httpx.TransportError))
        self.assertFalse(issubclass(httpx.RemoteProtocolError, httpx.ConnectError))
        self.assertFalse(
            issubclass(httpx.RemoteProtocolError, httpx.TimeoutException)
        )

    def test_fallback_advances_past_a_remote_protocol_error(self) -> None:
        """The exact live failure: model 1 disconnects, model 2 answers."""
        provider = self._provider(["m1", "m2", "m3"])
        attempted: list[str] = []

        def fake_call(model, *args, **kwargs):  # noqa: ANN001, ANN202
            attempted.append(model)
            if model == "m1":
                raise httpx.RemoteProtocolError("Server disconnected")
            return f"answered by {model}"

        with mock.patch.object(provider, "_call_generate", side_effect=fake_call):
            result = provider.generate("system", "who created you")

        self.assertEqual(result, "answered by m2")
        self.assertEqual(attempted, ["m1", "m2"])

    def test_fallback_advances_past_a_connect_error(self) -> None:
        provider = self._provider(["m1", "m2"])
        attempted: list[str] = []

        def fake_call(model, *args, **kwargs):  # noqa: ANN001, ANN202
            attempted.append(model)
            if model == "m1":
                raise httpx.ConnectError("dns lookup failed")
            return "ok"

        with mock.patch.object(provider, "_call_generate", side_effect=fake_call):
            self.assertEqual(provider.generate("s", "u"), "ok")
        self.assertEqual(attempted, ["m1", "m2"])

    def test_transcription_falls_back_too(self) -> None:
        provider = self._provider(["m1", "m2"])
        attempted: list[str] = []

        def fake_call(model, *args, **kwargs):  # noqa: ANN001, ANN202
            attempted.append(model)
            if model == "m1":
                raise httpx.RemoteProtocolError("Server disconnected")
            return "transcribed"

        with mock.patch.object(provider, "_call_transcribe", side_effect=fake_call):
            self.assertEqual(provider.transcribe(b"audio"), "transcribed")
        self.assertEqual(attempted, ["m1", "m2"])

    def test_every_model_failing_still_raises(self) -> None:
        """Widening the catch must not turn a total outage into silence."""
        provider = self._provider(["m1", "m2"])

        def always_disconnects(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise httpx.RemoteProtocolError("Server disconnected")

        with mock.patch.object(
            provider, "_call_generate", side_effect=always_disconnects
        ):
            with self.assertRaises(httpx.RemoteProtocolError):
                provider.generate("s", "u")

    def test_unrelated_errors_are_not_swallowed(self) -> None:
        """
        CLAUDE.md: no silent `except Exception` swallowing real bugs as
        expected failures. A programming error must still surface.
        """
        provider = self._provider(["m1", "m2"])

        def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise ValueError("a real bug")

        with mock.patch.object(provider, "_call_generate", side_effect=broken):
            with self.assertRaises(ValueError):
                provider.generate("s", "u")


class _FakeProc:
    def __init__(self, alive: bool, exitcode: int | None = None) -> None:
        self._alive = alive
        self.exitcode = exitcode

    def is_alive(self) -> bool:
        return self._alive


class Step6DeadWorkerTests(unittest.TestCase):
    """Step 6 — a dead service must be reported once, and plainly."""

    def test_dead_worker_is_warned_about_once(self) -> None:
        import run_aura

        processes = {
            "perception": _FakeProc(alive=True),
            "mind": _FakeProc(alive=False, exitcode=1),
        }
        warned: set[str] = set()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            first = run_aura._warn_for_dead_workers(processes, warned)
            second = run_aura._warn_for_dead_workers(processes, warned)

        self.assertEqual(first, ["mind"])
        self.assertEqual(second, [], "a service that stays down must not re-warn")

        text = out.getvalue()
        self.assertEqual(text.count("DEGRADED"), 1)
        self.assertIn("will NOT be restarted", text)
        self.assertIn("perception", text)  # the still-running list

    def test_live_workers_are_never_warned_about(self) -> None:
        import run_aura

        processes = {"perception": _FakeProc(alive=True)}
        warned: set[str] = set()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = run_aura._warn_for_dead_workers(processes, warned)

        self.assertEqual(result, [])
        self.assertEqual(out.getvalue(), "")


class Step8DuplicateGuardTests(unittest.TestCase):
    """Step 8 — the second `__main__` guard in perception/service.py."""

    def _main_guard_count(self, path: Path) -> int:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        count = 0
        for node in tree.body:  # module level only
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                count += 1
        return count

    def test_service_has_exactly_one_main_guard(self) -> None:
        path = REPO_ROOT / "perception" / "service.py"
        self.assertEqual(
            self._main_guard_count(path),
            1,
            "the duplicate module-level __main__ guard is back",
        )

    def test_run_local_has_exactly_one_main_guard(self) -> None:
        path = REPO_ROOT / "perception" / "run_local.py"
        self.assertEqual(self._main_guard_count(path), 1)


class _FakeCamera:
    """Feeds `read_fn(index)` back as the frame for each read."""

    def __init__(self, read_fn) -> None:  # noqa: ANN001
        self._read_fn = read_fn
        self.reads = 0

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc):  # noqa: ANN002, ANN204
        return False

    def read_frame(self):  # noqa: ANN201
        frame = self._read_fn(self.reads)
        self.reads += 1
        return frame


class _FakeCv2:
    # Constants `_draw_overlay` reads off the module.
    FONT_HERSHEY_SIMPLEX = 0
    LINE_AA = 16

    def __init__(self, quit_after: int = 10**9) -> None:
        self.wait_key_calls = 0
        self._quit_after = quit_after

    def putText(self, *a, **k) -> None:  # noqa: N802, ANN002, ANN003
        pass

    def imshow(self, *a, **k) -> None:  # noqa: ANN002, ANN003
        pass

    def destroyAllWindows(self) -> None:  # noqa: N802
        pass

    def waitKey(self, delay):  # noqa: N802, ANN001, ANN201
        self.wait_key_calls += 1
        return ord("q") if self.wait_key_calls >= self._quit_after else -1


class Step9CameraFailureTests(unittest.TestCase):
    """Step 9 — a dead camera must sleep, log once, and give up."""

    def _run_service(self, read_fn, threshold=3, quit_after=10**9):
        import perception.service as service

        fake_camera = _FakeCamera(read_fn)
        fake_cv2 = _FakeCv2(quit_after=quit_after)

        out = io.StringIO()
        with (
            mock.patch.object(service, "MAX_CONSECUTIVE_FAILURES", threshold),
            mock.patch.object(service, "Camera", lambda cfg: fake_camera),
            mock.patch.object(service, "CameraConfig", lambda: object()),
            # Patched on the service module, not left to the sys.modules
            # stub above: under `unittest discover` another test module can
            # import the real `perception` package first, which makes the
            # stub a no-op and hands service.py the ultralytics detector.
            mock.patch.object(service, "ObjectDetector", _StubDetector),
            mock.patch.object(service, "cv2", fake_cv2),
            mock.patch.object(service, "EventBus", lambda: mock.Mock()),
            mock.patch.object(service.time, "sleep") as sleeper,
            contextlib.redirect_stdout(out),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                service.main()

        return fake_camera, sleeper, out.getvalue(), ctx.exception

    def test_gives_up_at_the_threshold(self) -> None:
        camera, sleeper, text, exc = self._run_service(lambda i: None)

        self.assertEqual(
            camera.reads,
            3,
            "the loop must stop at exactly MAX_CONSECUTIVE_FAILURES reads",
        )
        self.assertIn("times in a row", str(exc))
        self.assertGreaterEqual(
            sleeper.call_count, 1, "a failed read must not spin without sleeping"
        )

    def test_warns_once_not_once_per_frame(self) -> None:
        """The old behaviour flooded stdout with one line per frame."""
        _, _, text, _ = self._run_service(lambda i: None)
        self.assertEqual(text.count("Warning:"), 1)

    def test_counter_resets_after_a_good_frame(self) -> None:
        """
        Two failures, one good frame, repeating — the counter must reset,
        so a camera that stutters is never mistaken for a dead one.
        """
        import perception.service as service

        frames = [None, None, "FRAME", None, None, "FRAME", None, None, "FRAME"]
        fake_camera = _FakeCamera(lambda i: frames[i] if i < len(frames) else "FRAME")
        fake_cv2 = _FakeCv2(quit_after=len(frames))

        with (
            mock.patch.object(service, "MAX_CONSECUTIVE_FAILURES", 3),
            mock.patch.object(service, "Camera", lambda cfg: fake_camera),
            mock.patch.object(service, "CameraConfig", lambda: object()),
            mock.patch.object(service, "ObjectDetector", _StubDetector),
            mock.patch.object(service, "cv2", fake_cv2),
            mock.patch.object(service, "EventBus", lambda: mock.Mock()),
            mock.patch.object(service.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            service.main()  # must return normally, not raise

        self.assertGreaterEqual(fake_camera.reads, len(frames))


if __name__ == "__main__":
    unittest.main()
