"""
GeminiProvider: free-tier LLMProvider implementation using Google's
Gemini API with multi-model fallback support.

Chosen as the default because it has a genuinely free tier (no card
required, generous limits for a personal-scale companion) and still
runs as a cloud API — matching the "runs on any computer" constraint
the same way ClaudeProvider did. Swapping back to ClaudeProvider, or to
a local model later, is a one-line change in llm_provider/__init__.py —
that's the entire point of coding against the LLMProvider interface
instead of a vendor SDK directly.

Requires GEMINI_API_KEY in the environment.
Get a free key at: https://aistudio.google.com/apikey
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError, ServerError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .base import LLMProvider, TranscriptionProvider

logger = logging.getLogger(__name__)

# Fallback sequence updated to current active 3.x models
DEFAULT_MODELS: list[str] = [
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]


class GeminiProvider(LLMProvider, TranscriptionProvider):
    def __init__(
        self,
        model: str | Sequence[str] | None = None,
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Get a free key at "
                "https://aistudio.google.com/apikey and export it before "
                "running anything under mind/."
            )
        # The SDK's own retry is OFF unless asked for: retry_options defaults
        # to None, which resolves to stop_after_attempt(1) in
        # _api_client.retry_args(). Turn it on so a transient transport
        # failure is absorbed before we pay for a whole model fallback.
        #
        # Scope, verified against the installed google-genai 2.23.0: the SDK
        # retries only httpx.TimeoutException and httpx.ConnectError
        # (_HTTPX_TRANSIENT_EXC, _api_client.py:111) plus a fixed set of 5xx
        # status codes. It does NOT retry httpx.RemoteProtocolError — the
        # failure actually observed in testing — so that one is handled by
        # the per-model fallback in generate()/transcribe() below.
        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=3)
            ),
        )

        # Handle string, list/sequence of models, or default fallback chain
        if model is None:
            self._models = list(DEFAULT_MODELS)
        elif isinstance(model, str):
            self._models = [model]
        else:
            self._models = list(model)

        if not self._models:
            raise ValueError("At least one model must be provided to GeminiProvider.")

        # Expose active primary model for backward compatibility
        self._model = self._models[0]

    @retry(
        retry=retry_if_exception_type(ServerError),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def _call_generate(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Internal helper with per-model retry for generate_content."""
        response = self._client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        return response.text or ""

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 1.0,
        max_tokens: int = 500,
    ) -> str:
        last_exception: Exception | None = None

        for model in self._models:
            try:
                return self._call_generate(
                    model, system_prompt, user_prompt, temperature, max_tokens
                )
            except (APIError, httpx.TransportError) as err:
                logger.warning(
                    "Gemini model '%s' failed generation (%s). Trying fallback...",
                    model,
                    err,
                )
                last_exception = err

        if last_exception:
            raise last_exception
        raise RuntimeError("All configured Gemini models failed during generation.")

    @retry(
        retry=retry_if_exception_type(ServerError),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def _call_transcribe(
        self,
        model: str,
        audio_bytes: bytes,
        mime_type: str,
    ) -> str:
        """Internal helper with per-model retry for transcription."""
        response = self._client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                "Transcribe this audio exactly as spoken. Return only the "
                "transcription, with no preamble, quotes, or commentary.",
            ],
        )
        return (response.text or "").strip()

    def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
        last_exception: Exception | None = None

        for model in self._models:
            try:
                return self._call_transcribe(model, audio_bytes, mime_type)
            except (APIError, httpx.TransportError) as err:
                logger.warning(
                    "Gemini model '%s' failed transcription (%s). Trying fallback...",
                    model,
                    err,
                )
                last_exception = err

        if last_exception:
            raise last_exception
        raise RuntimeError("All configured Gemini models failed during transcription.")