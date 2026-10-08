"""
Manual test: generates three introductions back to back, so you can
confirm phrasing actually varies instead of repeating.

Requires GEMINI_API_KEY set in your environment.

Run with:  uv run python mind/run_local.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mind.greeting import generate_introduction


def main() -> None:
    for i in range(3):
        action = generate_introduction(target_body_id="desktop-01")
        print(f"[{i + 1}] (style: {action.params['style_seed']})")
        print(f"    {action.params['text']}\n")


if __name__ == "__main__":
    main()
