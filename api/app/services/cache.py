"""Semantic cache backed by Redis (section 3.3).

Keys are namespaced by the Chroma index generation, so a re-index
automatically invalidates old entries (plan D2 from DEVELOPMENT-PLAN.md).
Entries also carry a TTL for garbage collection.

Never-cache rules: abstentions, fallbacks, follow-ups, and failed
validations are never stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx

from ..config import Settings
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")

# Default TTL for cache entries (24 hours)
CACHE_TTL_SECONDS = 86400

# Regex patterns for follow-up detection
_FOLLOWUP_RE = re.compile(
    r"^(and\b|also\b|what about\b|how about\b|tell me more|"
    r"can you also|what else|anything else|"
    r"\b(it|that|they|this|those|these)\b\s+(is|are|was|were|do|did|has|have|should|can))",
    re.I,
)


def is_followup(question: str) -> bool:
    """Heuristic: does this question look like a follow-up to a previous one?"""
    return bool(_FOLLOWUP_RE.match(question.strip()))


def _cache_key(generation_id: str, question_hash: str) -> str:
    return f"cache:{generation_id}:{question_hash}"


def _hash_question(question: str) -> str:
    normalized = question.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _get_generation_id(settings: Settings) -> str:
    """Read the active generation from the Chroma pointer collection."""
    try:
        url = (f"http://{settings.chroma_host}:{settings.chroma_port}"
               f"/api/v2/tenants/default_tenant/databases/default_database"
               f"/collections/rahbar-active")
        resp = httpx.get(url, timeout=2.0)
        if resp.status_code == 200:
            meta = resp.json().get("metadata") or {}
            gen = meta.get("generation")
            if gen is not None:
                return str(gen)
    except Exception:
        log.debug("Could not read generation from pointer collection")
    return "unknown"


class SemanticCache:
    """Simple exact-match cache keyed by question text, namespaced by
    the index generation for automatic invalidation."""

    def __init__(self, redis_client: Any, settings: Settings) -> None:
        self.redis = redis_client
        self.settings = settings
        self.threshold = float(getattr(settings, "cache_threshold", 0.95))
        self._generation_id: str | None = None

    @property
    def generation_id(self) -> str:
        if self._generation_id is None:
            self._generation_id = _get_generation_id(self.settings)
        return self._generation_id

    def invalidate(self) -> None:
        """Force re-reading the generation id on next access."""
        self._generation_id = None

    def should_skip_cache(
        self, question: str, response: AskResponse | None = None
    ) -> bool:
        """Return True if this request must never be cached."""
        if is_followup(question):
            return True
        if response is not None:
            # Never cache abstentions or fallbacks
            if not response.answered_from_context:
                return True
            if response.confidence == "low" and "unable" in response.answer.lower():
                return True
        return False

    def get(self, question: str) -> AskResponse | None:
        """Look up a cached response. Returns None on miss."""
        try:
            qhash = _hash_question(question)
            key = _cache_key(self.generation_id, qhash)
            raw = self.redis.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            return AskResponse(**data)
        except Exception:
            log.debug("Cache get failed", exc_info=True)
            return None

    def put(self, question: str, response: AskResponse) -> None:
        """Store a response, unless it should be skipped."""
        if self.should_skip_cache(question, response):
            return
        try:
            qhash = _hash_question(question)
            key = _cache_key(self.generation_id, qhash)
            self.redis.setex(key, CACHE_TTL_SECONDS, response.model_dump_json())
        except Exception:
            log.debug("Cache put failed", exc_info=True)
