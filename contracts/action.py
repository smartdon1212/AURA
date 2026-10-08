"""
Action: the single schema the Mind uses to express intent to a Body.

Design rule: the Mind never knows what a given Body can actually do with
an Action. A desktop Body might no-op on `move_toward`; a robot Body
routes it to motor controllers via its own adapter. Same Action emitted
by the Mind, different capability per Body — this is what makes the Mind
portable from desktop to robot without a rewrite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionIntent(str, Enum):
    SPEAK = "speak"
    EXPRESS = "express"
    MOVE_TOWARD = "move_toward"
    MANIPULATE = "manipulate"
    SWITCH_MODE = "switch_mode"


class ActionPriority(str, Enum):
    # REFLEX actions come from the fast reactive layer, not the LLM loop,
    # and preempt NORMAL/BACKGROUND actions at the Body's execution layer.
    REFLEX = "reflex"
    NORMAL = "normal"
    BACKGROUND = "background"


class Action(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    target_body_id: str
    intent: ActionIntent
    params: dict[str, Any] = Field(default_factory=dict)
    priority: ActionPriority = ActionPriority.NORMAL

    # Set true for actions the Mind needs confirmation actually executed
    # (e.g. a manipulation action on a robot) vs fire-and-forget (e.g. speak).
    requires_ack: bool = False

    model_config = {"frozen": True}
