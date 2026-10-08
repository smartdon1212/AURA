"""
LLMProvider: the interface the Mind talks to for reasoning/generation.

Design rule: nothing in mind/ imports a specific vendor SDK directly.
Gemini API is the v1 implementation (free tier, and "runs on any
computer" rules out a locally-hosted model for now). Claude remains
available as an alternate implementation. A local model becomes a third
implementation of this exact interface when Physical AI needs offline
operation — e.g. a robot with no connectivity. Swapping is a one-line
change in llm_provider/__init__.py, not a refactor.

TranscriptionProvider is kept as a separate interface rather than folded
into LLMProvider — not every provider supports audio input the same way
(or at all), so forcing every implementation to define `transcribe`
would be a leaky abstraction. A provider that supports both simply
implements both interfaces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 1.0,
        max_tokens: int = 500,
    ) -> str:
        """Returns generated text. Every implementation must honor this exact signature.

        max_tokens defaults to 400 (roughly 300 words) — enough for a complete
        thought without being unbounded. Callers with different length needs
        (a short reactive comment vs. a full Q&A answer) should pass their own
        value rather than relying on the default for everything.
        """
        raise NotImplementedError

class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
        """Returns the transcribed text. Every implementation must honor this exact signature."""
        raise NotImplementedError
