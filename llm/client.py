"""
Thin wrapper around the Groq API.

Kept small and dependency-light: one function for a normal chat completion,
one for a JSON-structured completion (used by the memory extraction /
conflict-detection strategy).
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from groq import Groq

DEFAULT_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
DEFAULT_JSON_MODEL = os.environ.get("GROQ_JSON_MODEL", "llama-3.3-70b-versatile")
# gpt-oss models "think" before answering and thinking tokens are billed as output.
# "low" keeps that short (cheaper + faster). Ignored for non-reasoning models.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "low")


def _extra_args(model: str) -> dict:
    if "gpt-oss" in model and REASONING_EFFORT:
        return {"extra_body": {"reasoning_effort": REASONING_EFFORT}}
    return {}


class LLMClient:
    def __init__(self, api_key: Optional[str] = None):
        api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to your .env file or environment."
            )
        self.client = Groq(api_key=api_key)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}  # non-streamed calls only

    def _track(self, response) -> None:
        u = getattr(response, "usage", None)
        if u is not None:
            self.usage["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
            self.usage["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0

    def chat(self, messages: List[dict], model: str = DEFAULT_CHAT_MODEL,
              temperature: float = 0.7, max_tokens: int = 1024, stream: bool = False):
        """
        Standard chat completion. `messages` is a list of
        {"role": "system"|"user"|"assistant", "content": str}.
        Returns a string when stream=False, or a generator of text chunks
        when stream=True.
        """
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **_extra_args(model),
        )
        if stream:
            def _gen():
                for chunk in response:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
            return _gen()
        self._track(response)
        return response.choices[0].message.content

    def json_completion(self, messages: List[dict], model: str = DEFAULT_JSON_MODEL,
                          temperature: float = 0.2, max_tokens: int = 1024) -> dict:
        """
        Chat completion constrained to JSON output. Used for structured
        tasks like memory extraction and conflict analysis, where the
        result needs to be parsed programmatically.
        """
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            **_extra_args(model),
        )
        self._track(response)
        raw = response.choices[0].message.content
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            cleaned = raw.strip().strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                return json.loads(cleaned)
            except Exception:
                return {"error": "failed_to_parse", "raw": raw}


_client_singleton: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LLMClient()
    return _client_singleton
