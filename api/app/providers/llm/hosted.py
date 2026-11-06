"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter).

Uses an OpenAI-compatible ``/chat/completions`` endpoint with JSON mode.
The key arrives as ``LLM_API_KEY`` from a Kubernetes Secret created on
the command line. It never appears in a file you commit, and never in a
log line.
"""

from __future__ import annotations

import json
import logging

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Passage
from .prompt import build_prompt

log = logging.getLogger("rahbar.provider.hosted")

TIMEOUT = 60.0
MAX_RETRIES = 1


def _safe_fallback(question: str) -> Answer:
    return Answer(
        reasoning="fallback: the hosted model did not produce a valid response",
        answer="I was unable to generate a reliable answer. Please rephrase or try again.",
        citations=[],
        answered_from_context=False,
        confidence="low",
    )


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=TIMEOUT)

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        if not self.settings.hosted_base_url:
            log.error("HOSTED_BASE_URL is not configured")
            return _safe_fallback(question)

        prompt = build_prompt(question, passages)
        url = f"{self.settings.hosted_base_url.rstrip('/')}/chat/completions"

        headers: dict[str, str] = {}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        payload: dict[str, object] = {
            "model": self.settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.settings.temperature,
            "response_format": {"type": "json_object"},
        }

        last_error: str = ""
        for attempt in range(1 + MAX_RETRIES):
            try:
                resp = self._client.post(url, json=payload, headers=headers, timeout=TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                answer = Answer.model_validate(json.loads(content))
                return answer
            except (ValidationError, json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Validation failed on attempt %d: %s", attempt + 1, last_error)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("HTTP error on attempt %d: %s", attempt + 1, last_error)
                break

        log.error("Falling back after %d attempts: %s", 1 + MAX_RETRIES, last_error)
        return _safe_fallback(question)
