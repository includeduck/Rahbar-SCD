"""Ollama provider: generates answers via a local or in-cluster Ollama (section 3.5).

Uses ``/api/chat`` with the ``Answer`` JSON schema as ``format`` so the
model is forced to produce valid JSON. Retries once on validation failure
and falls back to a safe non-answer on persistent errors.
"""

from __future__ import annotations

import json
import logging

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Passage
from .prompt import build_prompt

log = logging.getLogger("rahbar.provider.ollama")

TIMEOUT = 60.0
MAX_RETRIES = 1


def _answer_schema() -> dict[str, object]:
    """The JSON schema Ollama should constrain its output to."""
    return Answer.model_json_schema()


def _safe_fallback(question: str) -> Answer:
    """A safe answer that never surfaces a 500 or a fabricated citation."""
    return Answer(
        reasoning="fallback: the model did not produce a valid response",
        answer="I was unable to generate a reliable answer. Please rephrase or try again.",
        citations=[],
        answered_from_context=False,
        confidence="low",
    )


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=TIMEOUT)

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        prompt = build_prompt(question, passages)
        url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"

        payload = {
            "model": self.settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "format": _answer_schema(),
            "stream": False,
            "options": {"temperature": self.settings.temperature},
        }

        last_error: str = ""
        for attempt in range(1 + MAX_RETRIES):
            try:
                resp = self._client.post(url, json=payload, timeout=TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                answer = Answer.model_validate(json.loads(content))
                return answer
            except (ValidationError, json.JSONDecodeError, KeyError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Validation failed on attempt %d: %s", attempt + 1, last_error)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("HTTP error on attempt %d: %s", attempt + 1, last_error)
                break  # no point retrying a network error with the same payload

        log.error("Falling back after %d attempts: %s", 1 + MAX_RETRIES, last_error)
        return _safe_fallback(question)
