"""
Percept: the single schema every Body uses to report sensory data to the Mind.

Design rule: nothing downstream of this file should ever know or care
which Body produced a Percept, or what hardware/model generated it.
Perception services (YOLO, MediaPipe, STT, ...) all normalize into this
shape before publishing to the event bus.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PerceptType(str, Enum):
    OBJECT_DETECTED = "object_detected"
    FACE_DETECTED = "face_detected"
    POSE = "pose"
    GESTURE = "gesture"
    SPEECH = "speech"
    OCR_TEXT = "ocr_text"
    SCENE_CHANGE = "scene_change"


class Percept(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Which Body produced this. "desktop-01" today, "robot-01" later —
    # the Mind treats them identically.
    source_body_id: str

    type: PerceptType
    confidence: float = Field(ge=0.0, le=1.0)

    # Shape depends on `type`. Kept loose here on purpose; typed sub-models
    # can be layered on top per-type once perception services are real.
    payload: dict[str, Any] = Field(default_factory=dict)

    # Pointer to a stored frame/audio clip (blob storage), so Memory can
    # later resolve "show me what you saw" without embedding raw bytes here.
    raw_ref: str | None = None

    model_config = {"frozen": True}
