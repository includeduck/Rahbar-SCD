"""Semantic cache backed by Redis (section 3.3).

Cache entries are keyed by the index generation so a re-index
automatically invalidates old entries (plan D2). The generation is
re-read from the Chroma pointer every ``GENERATION_TTL_SECONDS`` so pods
pick up a new index within seconds, not only on restart.

Cache lookup is *semantic*: the question embedding is compared against
stored embeddings via cosine similarity. A hit requires similarity ≥ the
``CACHE_THRESHOLD`` from the ConfigMap.

Never-cache rules: abstentions, fallbacks, follow-ups, and ungrounded
answers are never stored.

Threshold: ``CACHE_THRESHOLD`` (ConfigMap, 0.97) was measured by
eval/cache_threshold.py: ten paraphrase pairs that should hit and ten
similar-but-different pairs that must not, embedded with the pinned model.
At 0.95, "more than five courses" vs "fewer than three courses" (0.953) and
"clause 1.21" vs "clause 1.22" (0.951) were false hits. 0.96 is the lowest
threshold with none; 0.97 keeps one step of margin above them. Output in
docs/evidence/cache-threshold.txt, decision in docs/adr/0003.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any

import httpx

from ..config import Settings
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")

# Default TTL for cache entries (24 hours)
CACHE_TTL_SECONDS = 86400

# How often to re-read the generation from Chroma (seconds)
GENERATION_TTL_SECONDS = 15

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


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Brute-force cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


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
    """Cosine-similarity cache keyed by question embedding, namespaced by
    the index generation for automatic invalidation on re-index."""

    def __init__(self, redis_client: Any, settings: Settings) -> None:
        self.redis = redis_client
        self.settings = settings
        self.threshold = settings.cache_threshold
        self._generation_id: str | None = None
        self._generation_read_at: float = 0.0

    @property
    def generation_id(self) -> str:
        """Re-read from Chroma if the cached value is stale."""
        now = time.monotonic()
        if (self._generation_id is None
                or now - self._generation_read_at > GENERATION_TTL_SECONDS):
            self._generation_id = _get_generation_id(self.settings)
            self._generation_read_at = now
        return self._generation_id

    def invalidate(self) -> None:
        """Force re-reading the generation id on next access."""
        self._generation_id = None
        self._generation_read_at = 0.0

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

    def _index_key(self, gen: str) -> str:
        """Redis key holding the list of cached entry keys for a generation."""
        return f"cache_index:{gen}"

    def get(
        self, question: str, query_vec: list[float] | None = None,
    ) -> AskResponse | None:
        """Look up a cached response via cosine similarity.

        If ``query_vec`` is None, semantic matching is not possible and
        we return None (miss).
        """
        if query_vec is None:
            return None
        try:
            gen = self.generation_id
            index_key = self._index_key(gen)
            entry_keys = self.redis.smembers(index_key)
            if not entry_keys:
                return None

            best_sim = -1.0
            best_data: str | None = None

            for ekey in entry_keys:
                raw = self.redis.get(ekey)
                if raw is None:
                    continue
                entry = json.loads(raw)
                stored_vec = entry.get("vec")
                if stored_vec is None:
                    continue
                sim = _cosine_similarity(query_vec, stored_vec)
                if sim >= self.threshold and sim > best_sim:
                    best_sim = sim
                    best_data = entry.get("response")

            if best_data is not None:
                return AskResponse(**json.loads(best_data))
        except Exception:
            log.debug("Cache get failed", exc_info=True)
        return None

    def put(
        self, question: str, response: AskResponse,
        query_vec: list[float] | None = None,
    ) -> None:
        """Store a response with its embedding vector.

        Skips storage if the response should not be cached or if no
        embedding vector is available.
        """
        if self.should_skip_cache(question, response):
            return
        if query_vec is None:
            return
        try:
            gen = self.generation_id
            entry_key = f"cache:{gen}:{id(response)}:{time.monotonic()}"
            payload = json.dumps({
                "vec": query_vec,
                "response": response.model_dump_json(),
            })
            self.redis.setex(entry_key, CACHE_TTL_SECONDS, payload)
            # Track entry keys in a set for this generation
            index_key = self._index_key(gen)
            self.redis.sadd(index_key, entry_key)
            self.redis.expire(index_key, CACHE_TTL_SECONDS)
        except Exception:
            log.debug("Cache put failed", exc_info=True)
