"""
Speaker: text-to-speech output for the desktop Body.

pyttsx3 chosen deliberately: it's fully offline (no network dependency
at speech time), and it's OS-aware under the hood the same way camera.py
is — SAPI5 on Windows, eSpeak on Linux — so this keeps working unmodified
when you move to Linux later.
"""

from __future__ import annotations

import pyttsx3


class Speaker:
    def __init__(self, rate: int = 175) -> None:
        self._rate = rate

    def speak(self, text: str) -> None:
        # A fresh engine per call is intentional, not wasteful — pyttsx3's
        # SAPI5 backend on Windows silently stops speaking on the second+
        # call if the same engine instance is reused (no error, just silence).
        # Recreating it is cheap and makes every call reliable.
        engine = pyttsx3.init()
        engine.setProperty("rate", self._rate)
        engine.say(text)
        engine.runAndWait()
        engine.stop()