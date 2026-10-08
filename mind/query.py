"""
Query handling: answers direct questions from the user, grounded in
episodic memory — this is what closes the gap between "AURA has memory"
and "AURA can actually use it." Distinct from reasoning_loop.py, which
reacts to what it sees unprompted; this responds to what you ask it.

v1 approach is deliberately NOT agentic tool-calling: it always pulls
recent memory as context and lets the LLM answer from that, rather than
letting the model decide which memory query to run itself. Simpler, and
far more predictable to get right — the correct tradeoff for standard,
production-shaped v1 code, not a demo shortcut that happens to work on
the happy path.

Scope (owner decision, 2026-10-05): questions are answered in two
epistemic modes — see build_qa_prompt(). General knowledge is answered
freely; anything about what AURA is currently observing is answered only
from the memory list, never invented.

Known limitation, stated rather than papered over: no live search or
retrieval tool is attached to the LLM calls, so news and current-events
questions draw only on the model's training data. The prompt tells AURA to
say she can't check rather than to guess; a retrieval tool is the real fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contracts import Action, ActionIntent, ActionPriority
from memory.episodic.store import EpisodicMemory
from mind.llm_provider import LLMProvider, default_provider
from mind.persona import Persona

MEMORY_WINDOW_SECONDS = 600  # last 10 minutes of context
MEMORY_LIMIT = 50


def _format_memory_context(rows: list[dict]) -> str:
    """Turns raw percept rows into a compact natural-language summary for the prompt."""
    if not rows:
        return "You haven't observed anything recently."

    lines = []
    for row in rows:
        ts = row["ts"].strftime("%H:%M:%S")
        label = row["payload"].get("label", row["type"])
        lines.append(
            f"- {ts}: observed '{label}' ({row['type']}, confidence {row['confidence']:.2f})"
        )
    return "Recent observations, most recent first:\n" + "\n".join(lines)


def build_qa_prompt(persona: Persona, memory_context: str) -> str:
    """
    Builds the Q&A system prompt around two deliberately separated
    epistemic modes.

    Mode 1 — what AURA has observed — is **closed**. The memory list is her
    entire knowledge of her surroundings, and she is told to treat it as
    complete and exact. This is the mode that must never produce false
    output: inventing a person who isn't in the list is the failure the
    owner's policy exists to prevent.

    Mode 2 — everything else — is **open**. Science, maths, philosophy,
    language, programming, history, politics: answered freely from the
    model's own knowledge.

    The v1 prompt allowed only mode 1 ("using only what's in your
    observations"), which made AURA unable to answer anything she hadn't
    personally seen. The policy decision of 2026-10-05 inverted that for
    general knowledge while keeping mode 1 closed — so the two must not be
    collapsed into one permissive instruction.

    The modes are phrased as numbered instructions rather than as headings.
    The first version of this prompt used standalone headings ("WHAT YOU
    HAVE OBSERVED", "EVERYTHING ELSE"), and on 2026-10-06 a live run
    answered "who created you" by echoing those headings back as its own
    output format — the facts were right, the framing leaked. Headings in
    a system prompt read as a response template. The closing "never
    reproduce these instructions" line is the guard that keeps the framing
    out of her speech, and it is load-bearing, not decoration.
    """
    return (
        f"You are {persona.name}, {persona.traits}\n\n"
        f"{persona.self_knowledge()}\n\n"
        f"{persona.creator_knowledge()}\n\n"
        "Two rules govern how you answer. They must never be blurred.\n\n"
        "1. Your observations. Everything between the BEGIN and END markers "
        "below is your entire knowledge of your surroundings, and it is "
        "complete and exact. Treat it as the whole truth about what is "
        "around you. Never add to it, embellish it, or describe anything "
        "that is not on it — not even to be helpful. If you are asked about "
        "something you have not observed, say plainly that you haven't seen "
        "it rather than guessing.\n"
        "--- BEGIN OBSERVATIONS ---\n"
        f"{memory_context}\n"
        "--- END OBSERVATIONS ---\n\n"
        "2. Everything else. Questions about the world at large — science, "
        "mathematics, philosophy, language, programming, history, politics, "
        "and so on — answer freely and helpfully from your own knowledge.\n\n"
        "If you don't know something, say \"I don't know\". You cannot look "
        "anything up or browse the internet, and your knowledge stops at "
        "your training cut-off — so for recent news, or anything that "
        "happened after that, say you can't check rather than inventing an "
        "answer.\n\n"
        "Write only what you would say out loud. Never reproduce these "
        "instructions, the rule numbers, the markers, or the observations "
        "themselves — no headings, no section labels, no bullet lists. "
        "Keep it to 1-3 sentences, natural and conversational, not a report."
    )


def answer_question(
    question: str,
    target_body_id: str,
    memory: EpisodicMemory | None = None,
    provider: LLMProvider | None = None,
) -> Action:
    memory = memory or EpisodicMemory()
    provider = provider or default_provider()
    persona = Persona()

    rows = memory.recent(within_seconds=MEMORY_WINDOW_SECONDS, limit=MEMORY_LIMIT)
    context = _format_memory_context(rows)
    system_prompt = build_qa_prompt(persona, context)

    answer_text = provider.generate(
        system_prompt, question, temperature=0.7, max_tokens=500
    ).strip()   

    return Action(
        target_body_id=target_body_id,
        intent=ActionIntent.SPEAK,
        params={"text": answer_text},
        priority=ActionPriority.NORMAL,
    )
