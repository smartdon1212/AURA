"""
Introduction behavior: the first thing AURA says when a Body comes online.

Randomization happens at the *style* level (see persona.py), not by
picking from a fixed list of pre-written greetings — the actual sentence
is generated fresh by the LLM, constrained to that style, every time.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contracts import Action, ActionIntent, ActionPriority
from mind.llm_provider import LLMProvider, default_provider
from mind.persona import Persona, random_style_seed


def generate_introduction(
    target_body_id: str, provider: LLMProvider | None = None
) -> Action:
    provider = provider or default_provider()
    persona = Persona()
    style_seed = random_style_seed()

    text = provider.generate(
        system_prompt=persona.system_prompt(style_seed),
        user_prompt="Introduce yourself. This is the start of a new session.",
        temperature=1.0,
    )

    return Action(
        target_body_id=target_body_id,
        intent=ActionIntent.SPEAK,
        params={"text": text.strip(), "style_seed": style_seed},
        priority=ActionPriority.NORMAL,
    )
