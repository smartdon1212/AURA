"""
Camera: cross-platform webcam capture.

Design rule: backend selection is config-driven, not hardcoded — you're
on Windows now and moving to Linux later, and the Body may eventually be
non-desktop hardware entirely. Nothing above this file should need to
change when the OS does.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

import cv2

_BACKEND_BY_OS = {
    "Windows": cv2.CAP_DSHOW,
    "Linux": cv2.CAP_V4L2,
    "Darwin": cv2.CAP_AVFOUNDATION,
}


@dataclass
class CameraConfig:
    device_index: int = 0
    width: int = 640
    height: int = 480
    backend: int | None = None  # None => auto-detect from the running OS


class Camera:
    """Thin wrapper around cv2.VideoCapture with OS-aware backend selection."""

    def __init__(self, config: CameraConfig | None = None) -> None:
        self.config = config or CameraConfig()
        backend = self.config.backend
        if backend is None:
            backend = _BACKEND_BY_OS.get(platform.system(), cv2.CAP_ANY)

        self._cap = cv2.VideoCapture(self.config.device_index, backend)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)

        if not self._cap.isOpened():
            raise RuntimeError(
                f"Could not open camera at index {self.config.device_index} "
                f"(backend={backend}). On Windows, check no other app has "
                f"the camera locked. On macOS, check camera permission is "
                f"granted in System Settings."
            )

    def read_frame(self):
        """Returns a BGR numpy frame, or None if a frame couldn't be read."""
        ok, frame = self._cap.read()
        return frame if ok else None

    def release(self) -> None:
        self._cap.release()

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
