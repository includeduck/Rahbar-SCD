"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter).

Uses an OpenAI-compatible ``/chat/completions`` endpoint with JSON mode.
The key arrives as ``LLM_API_KEY`` from a Kubernetes Secret created on
the command line. It never appears in a file you commit, and never in a
log line.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from pydantic import ValidationError

from ...config import Settings
from ...routes import metrics as prom
from ...schemas import Answer, Passage
from ...services.tracing import record_model_call
from .prompt import build_prompt

log = logging.getLogger("rahbar.provider.hosted")

# Number of retries on validation failure before falling back
RETRIES = 1


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
        self._client = httpx.Client(timeout=settings.llm_timeout_seconds)

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
        params = {"temperature": self.settings.temperature,
                  "response_format": "json_object",
                  "timeout_s": self.settings.llm_timeout_seconds}
        for attempt in range(1 + RETRIES):
            started = time.perf_counter()
            call: dict[str, object] = {"provider": self.name, "model": self.settings.llm_model,
                                       "params": params, "attempt": attempt + 1}
            try:
                resp = self._client.post(
                    url, json=payload, headers=headers, timeout=self.settings.llm_timeout_seconds,
                )
                resp.raise_for_status()
                data = resp.json()

                # Record token usage from hosted response
                usage = data.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                if prompt_tokens:
                    prom.TOKENS_IN.inc(prompt_tokens)
                if completion_tokens:
                    prom.TOKENS_OUT.inc(completion_tokens)

                call.update(tokens_in=prompt_tokens, tokens_out=completion_tokens)
                content = data["choices"][0]["message"]["content"]
                call["raw_output"] = content
                answer = Answer.model_validate(json.loads(content))
                call.update(ms=round((time.perf_counter() - started) * 1000, 1), valid=True)
                record_model_call(**call)
                return answer
            except (ValidationError, json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Validation failed on retry attempt %d: %s", attempt + 1, last_error)
                call.update(ms=round((time.perf_counter() - started) * 1000, 1), valid=False,
                            error=last_error[:300])
                record_model_call(**call)
            except httpx.HTTPError as exc:
                # The error text is the exception's, never the request headers (the key).
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("HTTP error on attempt %d: %s", attempt + 1, last_error)
                call.update(ms=round((time.perf_counter() - started) * 1000, 1), valid=False,
                            error=last_error[:300])
                record_model_call(**call)
                break

        log.error("Falling back after %d retries: %s", 1 + RETRIES, last_error)
        return _safe_fallback(question)
