"""
Voice conversation loop for the desktop Body: press Enter, speak, AURA
transcribes what you said, answers using memory-grounded reasoning, and
speaks the answer back.

This module is a library, not an entry point. It used to carry its own
main(), which meant it and run.py were two programs that each blocked
forever on a different resource — conversation.py waited on stdin and
never drained the bus, run.py drained the bus and never read stdin. Only
one could ever run, so AURA was never actually interactive. run.py is now
the single entry point and drives run_conversation() from its main
thread while the bus is consumed on a background thread.

Recording and speaking are the Body's job (it owns the hardware); the
Mind only ever handles the resulting text — it never touches audio
directly.

InteractionGate is imported from bodies.gate, not defined here — see the
note further down.

Note on imports: imported as part of a package by run.py, but the
sys.path insert below is kept so this file still resolves its own
imports if executed directly.

Run with:  uv run python bodies/desktop/run.py
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bodies.desktop.speaker import Speaker
from bodies.gate import InteractionGate
from mind.llm_provider import default_transcription_provider
from mind.query import answer_question
from perception.voice import record_utterance

__all__ = ["InteractionGate", "run_conversation"]

BODY_ID = "desktop-01"
RECORD_SECONDS = 5.0


def _explain_dead_stdin() -> None:
    """
    Says why the conversation loop stopped, and what the actual state of
    stdin was.

    The previous message — "No input available" — was true and useless: it
    did not distinguish "stdin is not a terminal" from "the terminal went
    away", and gave nothing to act on. On 2026-10-06 that line was the
    only symptom of the Body being run as a spawned child process, which
    costs a whole debug cycle to identify by hand. Print the evidence.
    """
    stream = sys.stdin
    try:
        isatty = stream.isatty()
    except (AttributeError, ValueError, OSError):
        isatty = False

    print("\nNo usable keyboard input — AURA can't take questions.")
    print(f"  stdin: {stream!r}  isatty={isatty}")

    if stream is None:
        print(
            "  There is no stdin at all. This is what a windowed Python "
            "host (pythonw) looks like — run AURA from a terminal."
        )
    elif not isatty:
        print(
            "  stdin is not a terminal, so it can never deliver an Enter "
            "keypress. Either AURA's input was redirected (a pipe or a "
            "file), or the Body inherited a stdin that isn't the console."
        )
    else:
        print(
            "  stdin is a terminal but reported end-of-file — the console "
            "was closed out from under the process."
        )


def _flush_stdin() -> None:
    """
    Discards type-ahead typed while AURA was speaking.

    Without this, an Enter pressed during playback is still sitting in the
    console buffer when the loop comes back around, so the next input()
    returns immediately and starts a recording the user never asked for.

    Platform-specific by necessity: the standard library has no portable
    "drain stdin". This is the exact split the standard getch() recipes
    use — msvcrt on Windows, termios on POSIX — so both branches are
    present and neither platform is special-cased away.
    """
    try:
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        # No interactive console attached (piped stdin, or stdin already
        # closed). There is nothing buffered to discard, so this is not an
        # error — but it is deliberately narrow rather than a bare except.
        pass


# `InteractionGate` is imported at the top of this file, not defined
# here. It moved to bodies/gate.py when the web Body landed: the
# drop-don't-queue narration policy is Body-side behaviour that every
# Body needs, and keeping one copy is what stops the desktop and web
# Bodies from drifting apart on it. The import above re-exports the
# name, so `from bodies.desktop.conversation import InteractionGate`
# keeps working.


def run_conversation(speaker: Speaker, gate: InteractionGate) -> None:
    """
    Reads stdin on the calling thread and returns when stdin closes.
    """
    transcriber = default_transcription_provider()

    print(
        f"\nPress Enter, then speak — records for {RECORD_SECONDS:.0f} "
        "seconds. Ctrl+C to quit."
    )

    while True:
        try:
            input("\nPress Enter to talk...")
        except EOFError:
            # stdin closed — piped input, or the terminal went away. Say
            # which, with the evidence, rather than just stopping.
            _explain_dead_stdin()
            return

        with gate.exchange():
            print("Listening...")
            try:
                audio = record_utterance(duration_seconds=RECORD_SECONDS)

                transcript = transcriber.transcribe(audio)
                if not transcript:
                    print("(heard nothing usable — try again)")
                else:
                    print(f"You said: {transcript}")

                    action = answer_question(transcript, target_body_id=BODY_ID)
                    text = action.params["text"]
                    print(f"AURA: {text}")
                    speaker.speak(text)
            except Exception:
                # One failed exchange must not end the session.
                #
                # Everything above can fail for reasons that have nothing to
                # do with a bug in this loop: the microphone can be
                # unplugged, Gemini can be rate-limited or offline, and
                # Postgres can be slow enough that answer_question() times
                # out reading memory. Before this guard, any of those
                # propagated out of run_conversation(), out of the Body, and
                # out of run_aura.py's main() — so pressing Enter at the
                # wrong moment shut down the whole of AURA, perception and
                # all.
                #
                # This is not a silent swallow: the full traceback is
                # printed, so a genuine bug is still visible and still gets
                # reported. What it must not do is take the session with it.
                # Ctrl+C still works — KeyboardInterrupt is not an Exception.
                traceback.print_exc()
                print(
                    "\nThat exchange failed — AURA is still running. Press "
                    "Enter to try again. The traceback above is the detail."
                )

        # Outside the gate, so the flush covers the whole exchange.
        _flush_stdin()


if __name__ == "__main__":
    print(
        "conversation.py is no longer a standalone entry point — ambient "
        "narration and the conversation loop now run in one process.\n"
        "Run:  uv run python bodies/desktop/run.py"
    )
