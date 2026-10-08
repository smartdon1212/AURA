"""
Manual test: type a question, get a memory-grounded spoken answer.

This proves the Q&A loop works correctly before voice input is wired on
top of it — text in, memory-grounded reasoning, speech out. Same
answer_question() function voice input will call once it exists; only
the input method changes.

Requires: Postgres running with AURA_PG_PASSWORD set, GEMINI_API_KEY
set, and some percepts already in memory — run perception/service.py or
memory/episodic/writer.py for a bit first so there's something to
recall, otherwise AURA will correctly tell you it hasn't seen anything.

Run with:  uv run python mind/query_local.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bodies.desktop.speaker import Speaker
from mind.query import answer_question

BODY_ID = "desktop-01"


def main() -> None:
    speaker = Speaker()
    print("Type a question (or 'quit' to exit).")

    while True:
        question = input("\nYou: ").strip()
        if question.lower() in ("quit", "exit"):
            break
        if not question:
            continue

        action = answer_question(question, target_body_id=BODY_ID)
        text = action.params["text"]
        print(f"AURA: {text}")
        speaker.speak(text)


if __name__ == "__main__":
    main()
