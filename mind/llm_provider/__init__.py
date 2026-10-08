from .base import LLMProvider, TranscriptionProvider
from .claude_provider import ClaudeProvider
from .gemini_provider import GeminiProvider


def default_provider() -> LLMProvider:
    """
    The single swap point for reasoning/generation. Currently Gemini
    (free tier, no card required). Change this one line to switch back
    to ClaudeProvider, or to a local model later — nothing outside this
    function needs to change.
    """
    return GeminiProvider()


def default_transcription_provider() -> TranscriptionProvider:
    """
    The single swap point for audio transcription. Kept separate from
    default_provider() because a future provider might handle reasoning
    without supporting audio input at all, or vice versa.
    """
    return GeminiProvider()


__all__ = [
    "LLMProvider",
    "TranscriptionProvider",
    "ClaudeProvider",
    "GeminiProvider",
    "default_provider",
    "default_transcription_provider",
]
