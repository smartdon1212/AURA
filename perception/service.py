"""
Perception service: the production perception loop. Captures frames,
runs detection, publishes every Percept to the bus, and shows a live
preview window with detections overlaid.

The preview window is purely visual — it never speaks and never feeds
into the reasoning/novelty pipeline. What AURA reacts to out loud is
controlled entirely by decision/policy.py's NoveltyFilter, independent
of what's shown here.

Run with:  uv run python perception/service.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from bus import PERCEPTS_STREAM, EventBus
from perception.camera import Camera, CameraConfig
from perception.detector import ObjectDetector

BODY_ID = "desktop-01"
WINDOW_TITLE = "AURA"
BLUE = (255, 100, 0)  # BGR — a clear blue, not pure primary blue

# Camera read failures (G-6). A disconnected or busy device used to spin
# this loop at full CPU with no delay at all. Sleep between attempts,
# report the first failure of a run rather than one line per frame, and
# give up at a bounded count instead of looping forever on a dead device.
# Shared with run_local.py, which mirrors this behaviour.
MAX_CONSECUTIVE_FAILURES = 50
FAILURE_SLEEP_SECONDS = 0.1


def _draw_overlay(frame, percepts) -> None:
    cv2.putText(
        frame,
        "AURA",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        BLUE,
        2,
        cv2.LINE_AA,
    )
    for percept in percepts:
        label = percept.payload.get("label", "?")
        bbox = percept.payload.get("bbox")
        if bbox is None:
            continue
        x1, y1, x2, y2 = (int(v) for v in bbox)
        cv2.rectangle(frame, (x1, y1), (x2, y2), BLUE, 2)
        text = f"{label} {percept.confidence:.0%}"
        cv2.putText(
            frame,
            text,
            (x1, max(y1 - 8, 15)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            BLUE,
            2,
            cv2.LINE_AA,
        )


def main() -> None:
    bus = EventBus()
    show_visuals = True
    with Camera(CameraConfig()) as camera:
        detector = ObjectDetector(source_body_id=BODY_ID)
        print(
            "Perception service running. "
            "'c' to toggle the preview window, 'q' or Ctrl+C to stop."
        )
        consecutive_failures = 0
        while True:
            frame = camera.read_frame()
            if frame is None:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    print(
                        "Warning: camera read failed. Retrying up to "
                        f"{MAX_CONSECUTIVE_FAILURES} times before stopping."
                    )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Camera returned no frame {MAX_CONSECUTIVE_FAILURES} "
                        "times in a row — the device is unplugged, or in use "
                        "by another program. Stopping perception."
                    )
                time.sleep(FAILURE_SLEEP_SECONDS)
                continue
            consecutive_failures = 0

            # Detection and publishing always run, regardless of the
            # preview window — vision and memory never depend on whether
            # anyone is watching the screen.
            percepts = list(detector.detect(frame))
            for percept in percepts:
                bus.publish(PERCEPTS_STREAM, percept)

            if show_visuals:
                _draw_overlay(frame, percepts)
                cv2.imshow(WINDOW_TITLE, frame)
            else:
                cv2.destroyAllWindows()

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("c"):
                show_visuals = not show_visuals

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()