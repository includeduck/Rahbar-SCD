"""Turns a question into an AskResponse. This is where the query pipeline lives.

Production steps (section 3.2–3.4):
1. Check semantic cache → return on hit
2. Retrieve top-k passages
3. Check relevance floor → abstain if all scores are below the threshold
4. Generate answer with the LLM
5. Validate with Pydantic (retry once, then safe fallback)
6. Drop any citation that does not match a retrieved passage
7. If all citations dropped but model claimed answered_from_context, retry
   once then fallback to answered_from_context=false (never cache it)
8. Store in cache (skip abstentions, fallbacks, follow-ups)
9. Log one structured line per request with chunk ids and scores
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..routes import metrics as prom
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache

log = logging.getLogger("rahbar.answer")

# Default relevance floor: below this, abstain without calling the LLM
DEFAULT_RELEVANCE_FLOOR = 0.25

# How many times to retry generation when citations are ungrounded
GENERATE_RETRIES = 1


def _safe_fallback() -> Answer:
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


def _generate_and_validate(
    question: str,
    passages: list[Passage],
    retrieved_refs: list[RetrievedRef],
    provider: LLMProvider,
) -> Answer:
    """Generate, validate citations, retry once if all citations are
    ungrounded but the model claims answered_from_context=True.
    Falls back to a safe non-answer on persistent failure."""

    for attempt in range(1 + GENERATE_RETRIES):
        try:
            answer = provider.generate(question, passages)  # type: ignore[arg-type]
        except Exception as exc:
            log.error("Provider error on attempt %d: %s", attempt + 1, exc)
            prom.FALLBACKS.inc()
            return _safe_fallback()

        # Filter citations: drop any that don't match a retrieved passage
        answer.citations = _filter_citations(answer.citations, retrieved_refs)

        # If the model claims it answered from context but has no valid
        # citations left, that's an ungrounded answer — retry once
        if answer.answered_from_context and not answer.citations:
            log.warning(
                "Ungrounded answer on attempt %d: answered_from_context=True "
                "but no valid citations remain; %s",
                attempt + 1,
                "retrying" if attempt < GENERATE_RETRIES else "falling back",
            )
            if attempt < GENERATE_RETRIES:
                continue
            # Final attempt: force to abstention, never cache
            prom.FALLBACKS.inc()
            return Answer(
                reasoning="fallback: model claimed to answer from context but "
                          "cited only passages that were not retrieved",
                answer=answer.answer,
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        return answer

    # Should not reach here, but be safe
    prom.FALLBACKS.inc()
    return _safe_fallback()


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None = None,
    query_vec: list[float] | None = None,
) -> tuple[AskResponse, bool]:
    """Return ``(response, was_cache_hit)``."""
    retrieval_ms = 0.0
    generation_ms = 0.0

    # 1. Semantic cache lookup
    if cache is not None and not cache.should_skip_cache(question):
        cached = cache.get(question, query_vec)
        if cached is not None:
            prom.CACHE_HITS.inc()
            log.info("cache_hit", extra={
                "question": question[:80],
                "request_id": request_id_var.get(),
            })
            cached.request_id = request_id_var.get()
            return cached, True
        prom.CACHE_MISSES.inc()

    # 2. Retrieve top-k passages
    t0 = time.perf_counter()
    try:
        passages = retriever.retrieve(question, k=settings.top_k)
    except Exception as exc:
        log.error("Retrieval error: %s", exc)
        prom.FALLBACKS.inc()
        return AskResponse(
            request_id=request_id_var.get(),
            answer="The retrieval service is temporarily unavailable. Please try again.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        ), False
    retrieval_ms = (time.perf_counter() - t0) * 1000
    prom.RETRIEVAL_LATENCY.observe(retrieval_ms / 1000)

    retrieved_refs = [
        RetrievedRef(
            chunk_id=p.chunk_id, source=p.source, section=p.section,
            page=p.page, score=p.score,
        )
        for p in passages
    ]

    # 3. Relevance floor: abstain if no passage is above the threshold
    relevance_floor = settings.relevance_floor
    if not passages or all(p.score < relevance_floor for p in passages):
        prom.ABSTENTIONS.inc()
        log.info("abstaining", extra={
            "reason": "below_relevance_floor",
            "top_score": passages[0].score if passages else 0.0,
            "floor": relevance_floor,
        })
        return AskResponse(
            request_id=request_id_var.get(),
            answer="I don't have enough relevant information in the available "
                   "documents to answer this question.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=retrieved_refs,
            provider=provider.name,
        ), False

    # 4. Generate answer (with retry on ungrounded citations)
    t0 = time.perf_counter()
    answer = _generate_and_validate(question, passages, retrieved_refs, provider)
    generation_ms = (time.perf_counter() - t0) * 1000
    prom.GENERATION_LATENCY.observe(generation_ms / 1000)

    response = AskResponse(
        request_id=request_id_var.get(),
        answer=answer.answer,
        citations=answer.citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=retrieved_refs,
        provider=provider.name,
    )

    # 5. Cache the response (skip abstentions, fallbacks, follow-ups)
    if cache is not None:
        cache.put(question, response, query_vec)

    # 6. Log one structured line per request
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
            "retrieval_ms": round(retrieval_ms, 1),
            "generation_ms": round(generation_ms, 1),
        },
    )
    return response, False
