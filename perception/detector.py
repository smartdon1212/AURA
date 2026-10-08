"""
ObjectDetector: wraps a YOLO model and emits Percepts.

Model size is config-driven: nano weights by default (CPU-safe, matches
the "runs on any computer" requirement), auto-upgraded to larger weights
if a GPU is detected. Nothing outside this file should know or care which
YOLO variant is actually running underneath.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch
from ultralytics import YOLO

from contracts import Percept, PerceptType

_MODEL_BY_TIER = {
    "cpu": "yolov8n.pt",
    "gpu": "yolov8s.pt",
}


def select_model_weights() -> str:
    """Auto-upgrades model size if a CUDA GPU is available; CPU-safe otherwise."""
    return _MODEL_BY_TIER["gpu"] if torch.cuda.is_available() else _MODEL_BY_TIER["cpu"]


class ObjectDetector:
    def __init__(self, source_body_id: str, weights: str | None = None) -> None:
        self.source_body_id = source_body_id
        self.model = YOLO(weights or select_model_weights())

    def detect(self, frame) -> list[Percept]:
        """Runs detection on a single BGR frame; one Percept per detected object."""
        results = self.model(frame, verbose=False)[0]
        percepts: list[Percept] = []

        for box in results.boxes:
            label = results.names[int(box.cls[0])]
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]

            percepts.append(
                Percept(
                    source_body_id=self.source_body_id,
                    type=PerceptType.OBJECT_DETECTED,
                    confidence=confidence,
                    payload={"label": label, "bbox": [x1, y1, x2, y2]},
                )
            )
        return percepts
