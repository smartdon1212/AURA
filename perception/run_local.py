"""
Manual test runner for the perception pipeline.

This is the first thing to run on your actual machine — it proves the
camera opens, YOLO runs, and real Percepts come out the other end, before
we wire anything to the event bus or the Mind.

Run with:   uv run python perception/run_local.py
Quit with:  'q' in the video window
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from perception.camera import Camera, CameraConfig
from perception.detector import ObjectDetector
from perception.service import FAILURE_SLEEP_SECONDS, MAX_CONSECUTIVE_FAILURES


def main() -> None:
    with Camera(CameraConfig()) as camera:
        detector = ObjectDetector(source_body_id="desktop-01")

        print("Starting capture. Press 'q' in the video window to quit.")
        consecutive_failures = 0
        while True:
            frame = camera.read_frame()
            if frame is None:
                # G-6: this used to print on every frame and continue with
                # no delay, flooding stdout at CPU speed. One line per
                # failure run, then a bounded number of retries.
                consecutive_failures += 1
                if consecutive_failures == 1:
                    print(
                        "Warning: failed to read frame. Retrying up to "
                        f"{MAX_CONSECUTIVE_FAILURES} times before stopping."
                    )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Camera returned no frame {MAX_CONSECUTIVE_FAILURES} "
                        "times in a row — the device is unplugged, or in use "
                        "by another program. Stopping capture."
                    )
                time.sleep(FAILURE_SLEEP_SECONDS)
                continue
            consecutive_failures = 0

            percepts = detector.detect(frame)
            for p in percepts:
                print(
                    f"[{p.ts.isoformat()}] {p.payload['label']} "
                    f"(confidence={p.confidence:.2f})"
                )

            cv2.imshow("AURA perception (press q to quit)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
