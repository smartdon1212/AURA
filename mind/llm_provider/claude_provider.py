"""
ClaudeProvider: v1 LLMProvider implementation using the Anthropic API.

Requires ANTHROPIC_API_KEY in the environment. This is the only file in
mind/ that imports the anthropic SDK directly — everything else talks to
the LLMProvider interface, so this file is the entire blast radius if the
provider ever changes.
"""

from __future__ import annotations

import os

import anthropic

from .base import LLMProvider

DEFAULT_MODEL = "claude-sonnet-4-5"


class ClaudeProvider(LLMProvider):
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it in your environment "
                "before running anything under mind/."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 1.0,
        max_tokens: int = 500,
    ) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")
   
