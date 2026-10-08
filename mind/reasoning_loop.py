"""
Reasoning loop: the Mind's main process. Consumes Percepts from the bus,
filters them through the Decision layer's novelty check, and — for
anything notable — asks the LLM to produce a short in-character reaction,
emitted as a SPEAK Action back to the Body that originated the Percept.

Run with:  uv run python mind/reasoning_loop.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus import PERCEPTS_STREAM, EventBus, actions_stream
from contracts import Action, ActionIntent, ActionPriority, Percept, PerceptType
from decision import NoveltyFilter
from mind.llm_provider import default_provider
from mind.persona import Persona, random_style_seed

GROUP = "mind"
CONSUMER = "mind-1"


def build_reaction_prompt(persona: Persona, style_seed: str, percept: Percept) -> tuple[str, str]:
    system = persona.system_prompt(style_seed) + (
        "\n\nYou are not introducing yourself right now — you're reacting "
        "to something you just noticed, briefly and naturally, like an "
        "aside, not a report. One short sentence."
    )
    label = percept.payload.get("label", "something")
    user = f"You just noticed: {label} (confidence {percept.confidence:.2f}). React briefly."
    return system, user


def main() -> None:
    bus = EventBus()
    provider = default_provider()
    persona = Persona()
    novelty = NoveltyFilter(cooldown_seconds=30.0)

    print("Mind reasoning loop running. Ctrl+C to stop.")

    for entry_id, percept in bus.consume(PERCEPTS_STREAM, GROUP, CONSUMER, Percept):
        if percept.type != PerceptType.OBJECT_DETECTED:
            bus.ack(PERCEPTS_STREAM, GROUP, entry_id)
            continue

        label = percept.payload.get("label", "")
        if not novelty.is_notable(label):
            bus.ack(PERCEPTS_STREAM, GROUP, entry_id)
            continue

        style_seed = random_style_seed()
        system, user = build_reaction_prompt(persona, style_seed, percept)
        text = provider.generate(system, user, temperature=1.0, max_tokens=150).strip()

        action = Action(
            target_body_id=percept.source_body_id,
            intent=ActionIntent.SPEAK,
            params={"text": text, "style_seed": style_seed},
            priority=ActionPriority.NORMAL,
        )
        bus.publish(actions_stream(percept.source_body_id), action)
        print(f"-> {label}: {text}")

        bus.ack(PERCEPTS_STREAM, GROUP, entry_id)


if __name__ == "__main__":
    main()
