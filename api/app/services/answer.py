"""Turns a question into an AskResponse. This is where the query pipeline lives.

Production steps (section 3.2–3.4):
1. Check semantic cache → return on hit
2. Retrieve top-k passages
3. Check relevance floor → abstain if all scores are below the threshold
4. Generate answer with the LLM
5. Validate with Pydantic (retry once, then safe fallback)
6. Drop any citation that does not match a retrieved passage
7. Store in cache (skip abstentions, fallbacks, follow-ups)
8. Log one structured line per request
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..schemas import AskResponse, Answer, Citation, RetrievedRef
from .cache import SemanticCache

log = logging.getLogger("rahbar.answer")

# Default relevance floor: below this, abstain without calling the LLM
DEFAULT_RELEVANCE_FLOOR = 0.25


def _safe_fallback(question: str) -> Answer:
    """A safe answer when something goes wrong — never a 500."""
    return Answer(
        reasoning="fallback: could not generate a reliable answer",
        answer="I was unable to generate a reliable answer. Please rephrase or try again.",
        citations=[],
        answered_from_context=False,
        confidence="low",
    )


def _filter_citations(
    citations: list[Citation], passages: list["RetrievedRef"],
) -> list[Citation]:
    """Drop any citation that does not match a retrieved passage."""
    retrieved_set = {(p.source, p.section) for p in passages}
    kept: list[Citation] = []
    for c in citations:
        if (c.source, c.section) in retrieved_set:
            kept.append(c)
        else:
            log.warning("Dropped citation not in retrieved set: %s / %s", c.source, c.section)
    return kept


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None = None,
) -> AskResponse:
    cache_hit = False
    retrieval_ms = 0.0
    generation_ms = 0.0

    # 1. Semantic cache lookup
    if cache is not None and not cache.should_skip_cache(question):
        cached = cache.get(question)
        if cached is not None:
            log.info("cache_hit", extra={
                "question": question[:80],
                "request_id": request_id_var.get(),
            })
            cached.request_id = request_id_var.get()
            return cached

    # 2. Retrieve top-k passages
    t0 = time.perf_counter()
    passages = retriever.retrieve(question, k=settings.top_k)
    retrieval_ms = (time.perf_counter() - t0) * 1000

    retrieved_refs = [
        RetrievedRef(
            chunk_id=p.chunk_id, source=p.source, section=p.section,
            page=p.page, score=p.score,
        )
        for p in passages
    ]

    # 3. Relevance floor: abstain if no passage is above the threshold
    relevance_floor = getattr(settings, "relevance_floor", DEFAULT_RELEVANCE_FLOOR)
    if not passages or all(p.score < relevance_floor for p in passages):
        log.info("abstaining", extra={
            "reason": "below_relevance_floor",
            "top_score": passages[0].score if passages else 0.0,
            "floor": relevance_floor,
        })
        return AskResponse(
            request_id=request_id_var.get(),
            answer="I don't have enough relevant information in the available documents to answer this question.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=retrieved_refs,
            provider=provider.name,
        )

    # 4. Generate answer
    t0 = time.perf_counter()
    try:
        answer = provider.generate(question, passages)
    except Exception as exc:
        log.error("Provider error: %s", exc)
        answer = _safe_fallback(question)
    generation_ms = (time.perf_counter() - t0) * 1000

    # 5. Drop any citation that does not match a retrieved passage
    answer.citations = _filter_citations(answer.citations, retrieved_refs)

    response = AskResponse(
        request_id=request_id_var.get(),
        answer=answer.answer,
        citations=answer.citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=retrieved_refs,
        provider=provider.name,
    )

    # 6. Cache the response (skip abstentions, fallbacks, follow-ups)
    if cache is not None:
        cache.put(question, response)

    # 7. Log one structured line per request
    log.info(
        "answered",
        extra={
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "answered_from_context": answer.answered_from_context,
            "confidence": answer.confidence,
            "citations_kept": len(answer.citations),
            "cache_hit": cache_hit,
            "retrieval_ms": round(retrieval_ms, 1),
            "generation_ms": round(generation_ms, 1),
        },
    )
    return response
