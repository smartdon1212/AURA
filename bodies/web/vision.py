"""
The web Body's eyes: capture a frame, detect objects, publish Percepts,
and keep the newest annotated JPEG for the browser to stream.

This is perception/service.py's loop with the OpenCV preview window
replaced by a JPEG frame buffer. It deliberately mirrors that file rather
than sharing code with it, because the two differ in exactly one place
and sharing would mean threading a display abstraction through a 60-line
loop to save 40 lines — but the parts that matter are copied, not
reinvented:

  - the same bounded camera-failure handling (G-6): sleep between
    attempts, report the first failure of a run rather than one line per
    frame, and give up at a bounded count instead of spinning forever at
    full CPU on a dead device
  - the same `source_body_id` -> `actions:<body>` routing, so the Mind
    replies to `web-01` without knowing this Body exists

One deliberate difference: perception/service.py *raises* when the camera
gives up, because it is a dedicated service whose only job is perception.
Here the thread stops and says so. The web Body is also the UI — taking
it down because the camera was unplugged would leave the user staring at
a dead page with no way to ask what went wrong, which is strictly worse
than a face that admits it cannot see.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import cv2

from bus import PERCEPTS_STREAM, EventBus
from bodies.web.state import AuraState

BODY_ID = "web-01"

# See perception/service.py: shared values, kept identical on purpose.
MAX_CONSECUTIVE_FAILURES = 50
FAILURE_SLEEP_SECONDS = 0.1

JPEG_QUALITY = 75
STATS_INTERVAL_SECONDS = 1.0

# BGR. The same cyan the page uses for its chrome, so the boxes look like
# part of the interface rather than an OpenCV default.
MARK_COLOUR = (255, 232, 56)


def _corner_brackets(frame, x1: int, y1: int, x2: int, y2: int, thickness: int) -> None:
    """
    Four L-shaped marks instead of a full rectangle.

    A closed box hides whatever is under its edges and reads as a
    debugging overlay; brackets read as a targeting reticle, which is
    both the requested look and less obstructive of the actual picture.
    """
    arm_x = max(6, int((x2 - x1) * 0.28))
    arm_y = max(6, int((y2 - y1) * 0.28))

    corners = (
        (x1, y1, 1, 1),
        (x2, y1, -1, 1),
        (x1, y2, 1, -1),
        (x2, y2, -1, -1),
    )
    for cx, cy, sx, sy in corners:
        cv2.line(frame, (cx, cy), (cx + sx * arm_x, cy), MARK_COLOUR, thickness, cv2.LINE_AA)
        cv2.line(frame, (cx, cy), (cx, cy + sy * arm_y), MARK_COLOUR, thickness, cv2.LINE_AA)


def _draw_overlay(frame, percepts) -> None:
    for percept in percepts:
        bbox = percept.payload.get("bbox")
        if bbox is None:
            continue
        x1, y1, x2, y2 = (int(v) for v in bbox)
        _corner_brackets(frame, x1, y1, x2, y2, 2)

        label = percept.payload.get("label", "?")
        text = f"{label} {percept.confidence:.0%}"
        # Above the box normally, below it when the box starts at the top
        # edge and the label would be clipped off-screen.
        ty = y1 - 8 if y1 > 22 else min(y2 + 20, frame.shape[0] - 6)
        cv2.putText(
            frame,
            text,
            (x1, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            MARK_COLOUR,
            2,
            cv2.LINE_AA,
        )


def _encode_jpeg(frame) -> bytes | None:
    ok, buffer = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    )
    return buffer.tobytes() if ok else None


class VisionService:
    def __init__(
        self,
        state: AuraState,
        camera=None,
        detector=None,
        bus=None,
        body_id: str = BODY_ID,
    ) -> None:
        """
        `camera`, `detector` and `bus` are injectable so the loop can be
        tested without a webcam, a YOLO model or a live Redis — the same
        shape the existing writer tests use. Left as None, they are built
        on the vision thread.
        """
        self._state = state
        self._camera = camera
        self._detector = detector
        self._bus = bus
        self.body_id = body_id
        self._stop = threading.Event()
        self._jpeg: bytes | None = None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def latest_jpeg(self) -> bytes | None:
        """
        The most recent annotated frame, or None before the first one.

        No lock: this is a single reference rebind, which is atomic in
        CPython, and a reader can at worst get the previous frame. A lock
        here would serialise every MJPEG client against the capture loop
        for no correctness gain.
        """
        return self._jpeg

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Blocks until stop() is called or the camera gives up. Run on a
        daemon thread — see bodies/web/app.py."""
        camera = self._camera
        detector = self._detector

        if camera is None or detector is None:
            try:
                # Imported here rather than at module scope: ObjectDetector
                # pulls in torch and ultralytics, and the HTTP layer of
                # this Body is imported by tests and tooling that have no
                # interest in loading a model. Camera is cheap, but it is
                # built here too so both halves of "get me a camera and a
                # detector" live in one place.
                from perception.camera import Camera, CameraConfig
                from perception.detector import ObjectDetector

                camera = camera or Camera(CameraConfig())
                detector = detector or ObjectDetector(source_body_id=self.body_id)
            except Exception:
                self._fail(
                    "Camera unavailable — AURA cannot see anything. Close any "
                    "other program using the webcam (and make sure the desktop "
                    "perception service is not running) and start again."
                )
                return

        bus = self._bus
        if bus is None:
            try:
                bus = EventBus()
            except Exception:
                self._fail("Cannot reach Redis — AURA cannot see or be asked anything.")
                return

        print(f"Web body vision running as '{self.body_id}'. Ctrl+C to stop.")

        consecutive_failures = 0
        frames_since_stats = 0
        stats_window_started = time.monotonic()

        try:
            while not self._stop.is_set():
                frame = camera.read_frame()
                if frame is None:
                    consecutive_failures += 1
                    if consecutive_failures == 1:
                        print(
                            "Warning: camera read failed. Retrying up to "
                            f"{MAX_CONSECUTIVE_FAILURES} times before stopping."
                        )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        self._fail(
                            f"The camera returned no frame "
                            f"{MAX_CONSECUTIVE_FAILURES} times in a row — the "
                            "device is unplugged, or in use by another program. "
                            "AURA has stopped watching, but you can still talk "
                            "to her; restart with --web to bring vision back."
                        )
                        return
                    time.sleep(FAILURE_SLEEP_SECONDS)
                    continue
                consecutive_failures = 0

                percepts = list(detector.detect(frame))
                for percept in percepts:
                    bus.publish(PERCEPTS_STREAM, percept)

                _draw_overlay(frame, percepts)
                jpeg = _encode_jpeg(frame)
                if jpeg is not None:
                    self._jpeg = jpeg

                frames_since_stats += 1
                elapsed = time.monotonic() - stats_window_started
                if elapsed >= STATS_INTERVAL_SECONDS:
                    height, width = frame.shape[:2]
                    self._state.set_stats(
                        body_id=self.body_id,
                        width=width,
                        height=height,
                        fps=frames_since_stats / elapsed,
                        objects=len(percepts),
                    )
                    frames_since_stats = 0
                    stats_window_started = time.monotonic()
        except Exception as exc:
            traceback.print_exc()
            self._fail(f"Vision stopped unexpectedly ({exc!r}).")
        finally:
            try:
                camera.release()
            except Exception:
                traceback.print_exc()

    def _fail(self, message: str) -> None:
        """
        Say it twice: on the console for whoever launched AURA, and over
        SSE so the person looking at the browser sees it too. A vision
        failure that is only on a console the user is not watching reads
        as "AURA got quiet for no reason".
        """
        print(f"\nWARNING: {message}")
        self._state.broadcast("error", {"message": message})
        self._stop.set()
