"""Prometheus metrics at /metrics (section 4.8).

Request count and latency are wired up already. Additional metrics:
retrieval latency, generation latency, cache hits, abstentions, fallbacks, tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

# Retrieval and generation latency
RETRIEVAL_LATENCY = Histogram(
    "rahbar_retrieval_seconds",
    "Retrieval latency",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
GENERATION_LATENCY = Histogram(
    "rahbar_generation_seconds",
    "LLM generation latency",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

# Cache metrics
CACHE_HITS = Counter("rahbar_cache_hits_total", "Semantic cache hits")
CACHE_MISSES = Counter("rahbar_cache_misses_total", "Semantic cache misses")

# Answer type counters
ABSTENTIONS = Counter("rahbar_abstentions_total", "Questions where the system abstained")
FALLBACKS = Counter("rahbar_fallbacks_total", "Answers that used the safe fallback")
SUPPORT_REPLIES = Counter("rahbar_support_replies_total",
                          "Questions that mentioned self-harm and got the support message")

# Token counters (approximate)
TOKENS_IN = Counter("rahbar_tokens_in_total", "Approximate input tokens to the LLM")
TOKENS_OUT = Counter("rahbar_tokens_out_total", "Approximate output tokens from the LLM")


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
