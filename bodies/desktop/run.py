"""
Desktop Body: the process that makes AURA actually speak.

This is the single Body entry point. It does two things at once, which is
what makes AURA interactive rather than just ambient:

  - A background thread consumes Actions meant for this Body from the bus
    and speaks every SPEAK intent that arrives unprompted — the ambient
    narration the Mind emits when it notices something.
  - The main thread runs the press-Enter-to-talk conversation loop.

These were previously two separate entry points (run.py and
conversation.py), each blocking forever on a different resource, so only
one could run at a time. They now share one process and one Speaker, which
they interleave through InteractionGate.

This is the only place in the whole system that produces audible output —
everything upstream only ever emits typed Action objects; this is where
intent becomes sound.

Note on imports: this file is run directly (`uv run python
bodies/desktop/run.py`), so it uses absolute imports throughout, not
relative ones — a relative import here would raise ImportError the
moment this file is executed as a script rather than imported as part
of a package.

Run with:  uv run python bodies/desktop/run.py
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bodies.desktop.conversation import InteractionGate, run_conversation
from bodies.desktop.speaker import Speaker
from bus import EventBus, actions_stream
from contracts import Action, ActionIntent
from mind.greeting import generate_introduction

BODY_ID = "desktop-01"
GROUP = "body-desktop"
CONSUMER = "desktop-1"


def _consume_actions(speaker: Speaker, gate: InteractionGate) -> None:
    """
    Ambient narration loop, driven on a background thread by main().

    The loop body is guarded, and that is the point: this is a *daemon*
    thread, so if it raises, it dies without a trace and without stopping
    anything else — the process stays up, the conversation loop keeps
    working, and AURA simply goes silent about what she can see, with no
    indication why. A silent failure in the one thread that makes her
    present is worse than a loud one.

    Warned once per failure run rather than once per action, because the
    action stream carries several entries a second and a persistent fault
    would otherwise bury the console. Same shape as the camera-failure and
    memory-writer handling.
    """
    bus = EventBus()
    stream = actions_stream(BODY_ID)
    consecutive_failures = 0

    for entry_id, action in bus.consume(stream, GROUP, CONSUMER, Action):
        try:
            if action.intent != ActionIntent.SPEAK:
                bus.ack(stream, GROUP, entry_id)
                continue

            text = action.params.get("text", "")
            with gate.ambient_speech() as should_speak:
                if should_speak:
                    print(f"AURA: {text}")
                    speaker.speak(text)
                else:
                    print(f"(narration suppressed during conversation: {text})")

            # Acked either way. Suppressed means dropped, not queued: leaving
            # it unacked would park it in this consumer group's PEL and have
            # it redelivered later, which is queuing under a different name.
            bus.ack(stream, GROUP, entry_id)

        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures == 1:
                print(
                    f"\nWARNING: ambient narration hit an error ({exc!r}) "
                    "and is still running. AURA will not describe what she "
                    "sees until this clears; the conversation loop is "
                    "unaffected."
                )
            traceback.print_exc()
            continue

        if consecutive_failures:
            print(
                f"Ambient narration recovered after {consecutive_failures} "
                "failed action(s)."
            )
            consecutive_failures = 0


def _introduce(speaker: Speaker) -> None:
    """
    Speaks AURA's introduction, or skips it if the LLM can't be reached.

    The greeting is generated fresh by Gemini, which can be rate-limited,
    offline, or slow on a cold start. This used to run unguarded in main(),
    so a single failed greeting killed the Body before it ever reached the
    conversation loop — AURA would exit at startup for a reason the user
    could do nothing about, and could not even ask her anything.

    Only the *generation* is guarded. Speaker construction and the actual
    playback are left alone: those fail for local, configuration-shaped
    reasons (no audio device, no voice) and should be loud rather than
    quietly skipped, or AURA would come up looking healthy while producing
    no sound at all.
    """
    try:
        intro = generate_introduction(target_body_id=BODY_ID)
    except Exception:
        traceback.print_exc()
        print(
            "AURA: (couldn't reach the language model to introduce myself "
            "— starting anyway, so you can still talk to me.)"
        )
        return

    print(f"AURA: {intro.params['text']}")
    speaker.speak(intro.params["text"])


def main() -> None:
    speaker = Speaker()
    gate = InteractionGate()

    _introduce(speaker)

    # Daemon so it dies with the process; the main thread owns the exit.
    listener = threading.Thread(
        target=_consume_actions,
        args=(speaker, gate),
        name="ambient-narration",
        daemon=True,
    )
    listener.start()

    print("Desktop body listening for actions. Ctrl+C to stop.")
    run_conversation(speaker, gate)

    # run_conversation returns only when stdin closes. Ambient narration
    # is still perfectly able to run at that point, so the Body must not
    # exit and take it down: AURA can still tell you what she sees, you
    # just can't ask her anything. The listener is a daemon, so this join
    # blocks until Ctrl+C rather than forever — the main thread is still
    # what owns the exit.
    print(
        "\nAURA can't take questions without a keyboard — carrying on with "
        "ambient narration only. Ctrl+C to stop."
    )
    listener.join()


if __name__ == "__main__":
    main()
