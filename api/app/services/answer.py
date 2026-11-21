"""Turns a question into an AskResponse. This is where the query pipeline lives.

Production steps (section 3.2–3.4):
0. A question that mentions self-harm gets a fixed support message (services/safety.py)
1. Check semantic cache → return on hit
2. Retrieve top-k passages
3. Check relevance floor → abstain if all scores are below the threshold
4. Generate answer with the LLM
5. Validate with Pydantic (retry once, then safe fallback)
6. Drop any citation that does not match a retrieved passage
7. If all citations dropped but model claimed answered_from_context, retry
   once then fallback to answered_from_context=false (never cache it). A model that
   says answered_from_context=false abstains even if it cites a retrieved passage, and
   every abstention shows ABSTAIN_MESSAGE, not the model's text
8. Store in cache (skip abstentions, fallbacks, follow-ups)
9. Log one structured line per request with chunk ids and scores

Assignment 03 adds:
- one trace record per request (services/tracing.py, D1), written on every return path
- an input rail and a retrieval rail against prompt injection (services/guardrails.py, D4):
  the input rail flags, the retrieval rail drops poisoned passages before the prompt
- metrics for the outcome, top-1 similarity, stage latency and injections (D3)
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.prompt import build_prompt
from ..retrieval.base import Retriever
from ..routes import metrics as prom
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from . import guardrails, tracing
from .cache import SemanticCache
from .safety import SUPPORT_MESSAGE, mentions_self_harm

log = logging.getLogger("rahbar.answer")

# Default relevance floor: below this, abstain without calling the LLM
DEFAULT_RELEVANCE_FLOOR = 0.25

# How many times to retry generation when citations are ungrounded
GENERATE_RETRIES = 1


FALLBACK_MESSAGE = "I was unable to generate a reliable answer. Please rephrase or try again."

# Every abstention, whether from the relevance floor or from the model, shows this text.
ABSTAIN_MESSAGE = ("I don't have enough relevant information in the available "
                   "documents to answer this question.")


def _safe_fallback() -> Answer:
    """A safe answer when something goes wrong — never a 500."""
    return Answer(
        reasoning="fallback: could not generate a reliable answer",
        answer=FALLBACK_MESSAGE,
        citations=[],
        answered_from_context=False,
        confidence="low",
    )


def _section_matches(cited: str, retrieved: str) -> bool:
    """True when a cited section names the retrieved one.

    The chunker labels a section with its heading, which for a numbered clause is the
    number followed by the start of its text ("5.4 Minimum CGPA required ..."), and models
    usually cite just the number ("5.4"). The number must end where the cited text ends:
    "5.4" matches "5.4 Minimum ..." but not "5.41" or "5.4.1". Same rule as eval/run_eval.py.
    """
    cited, retrieved = cited.strip(), retrieved.strip()
    if not cited:
        return False
    if cited.lower() == retrieved.lower():
        return True
    return bool(re.match(re.escape(cited) + r"(?![\d.])", retrieved, re.I))


def _note(action: str, **detail: object) -> None:
    """Record what the output rail did in the request's trace."""
    trace = tracing.current()
    if trace:
        trace.guardrail("output", action, **detail)


def _filter_citations(
    citations: list[Citation], passages: list["RetrievedRef"],
) -> list[Citation]:
    """Keep only citations that name a retrieved passage, rewritten to that passage's exact
    source, section and page, so every returned citation matches a retrieved chunk exactly.
    Drop the rest."""
    kept: list[Citation] = []
    for c in citations:
        match = next((p for p in passages
                      if p.source == c.source and _section_matches(c.section, p.section)), None)
        if match is None:
            log.warning("Dropped citation not in retrieved set: %s / %s", c.source, c.section)
            _note("dropped_citation", source=c.source, section=c.section,
                  why="names no passage in the prompt")
            continue
        cited = Citation(source=match.source, section=match.section, page=match.page)
        if cited not in kept:
            kept.append(cited)
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
            _note("fallback", why=f"provider error: {type(exc).__name__}: {exc}"[:300])
            return _safe_fallback()
        if answer.reasoning.startswith("fallback:"):
            _note("fallback", why=answer.reasoning)

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
            _note("ungrounded_answer", attempt=attempt + 1,
                  then="retry" if attempt < GENERATE_RETRIES else "fallback")
            if attempt < GENERATE_RETRIES:
                continue
            # Final attempt: force to abstention, never cache. The model's text is
            # not shown: it made claims that no retrieved passage supports.
            prom.FALLBACKS.inc()
            return Answer(
                reasoning="fallback: model claimed to answer from context but "
                          "cited only passages that were not retrieved",
                answer=FALLBACK_MESSAGE,
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        # answered_from_context=False always means abstain, even when the model also cites a
        # retrieved passage with high confidence. Measured on 8 Nov with llama3.2:3b
        # (eval/compare_flag_rules.py, docs/evidence/flag-rule-comparison.txt): such answers
        # were right in 3 of 5 cases ("100%" attendance, clause 5.6, FYP credits) and wrong
        # in 2 ("a maximum of two courses in a semester", read from the summer rule). A
        # missed answer is the safe way to be wrong.
        #
        # An abstention shows our fixed text, never the model's. Nothing checks that text
        # against the passages, and the model returned "" for an injection attempt and
        # "Not applicable" for the hostel question.
        if not answer.answered_from_context:
            if answer.citations:
                log.warning("Abstaining although the model cited %d valid passage(s) "
                            "with confidence %s", len(answer.citations), answer.confidence)
            if not answer.reasoning.startswith("fallback:"):
                _note("model_abstained", model_answer=answer.answer[:300],
                      cited=len(answer.citations), confidence=answer.confidence)
            answer.answer = ABSTAIN_MESSAGE
            answer.citations = []

        return answer

    # Should not reach here, but be safe
    prom.FALLBACKS.inc()
    return _safe_fallback()


def _outcome(answer: Answer) -> str:
    if answer.answered_from_context:
        return "answered"
    if answer.reasoning.startswith("fallback:"):
        return "fallback"
    return "abstain_model"


def _observe(trace: tracing.Trace, stage: str, ms: float) -> None:
    trace.stage(stage, ms)
    prom.STAGE_LATENCY.labels(stage=stage).observe(ms / 1000)


@contextmanager
def _timed(trace: tracing.Trace, stage: str) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _observe(trace, stage, (time.perf_counter() - t0) * 1000)


def answer_question(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None = None,
    query_vec: list[float] | None = None,
    embed_ms: float | None = None,
) -> tuple[AskResponse, bool]:
    """Return ``(response, was_cache_hit)``. Writes one trace record whatever happens."""
    trace = tracing.Trace(request_id_var.get(), question, settings)
    token = tracing.trace_var.set(trace)
    outcome = "error"
    try:
        if embed_ms is not None:
            _observe(trace, "embed", embed_ms)
        response, hit, outcome = _answer(question, retriever, provider, settings, cache,
                                         query_vec, trace)
        trace.set(response={"answer": response.answer,
                            "answered_from_context": response.answered_from_context,
                            "confidence": response.confidence},
                  citations=[c.model_dump() for c in response.citations])
        return response, hit
    finally:
        rec = trace.emit(outcome)
        prom.RESPONSES.labels(outcome=outcome).inc()
        prom.STAGE_LATENCY.labels(stage="total").observe(rec["latency_ms"]["total"] / 1000)
        tracing.trace_var.reset(token)


def _answer(
    question: str,
    retriever: Retriever,
    provider: LLMProvider,
    settings: Settings,
    cache: SemanticCache | None,
    query_vec: list[float] | None,
    trace: tracing.Trace,
) -> tuple[AskResponse, bool, str]:
    """The pipeline itself. Returns ``(response, was_cache_hit, outcome)``."""
    retrieval_ms = 0.0
    generation_ms = 0.0

    # 0. Self-harm check: a fixed support message, before the cache, retrieval and the
    #    LLM. The question text is not logged, and not kept in the trace.
    if mentions_self_harm(question):
        prom.SUPPORT_REPLIES.inc()
        trace.redact_query()
        trace.guardrail("input", "support_reply", why="self-harm phrase")
        log.info("support_reply", extra={
            "reason": "self_harm_phrase", "top_k": settings.top_k, "chunk_ids": [],
            "scores": [], "answered_from_context": False,
        })
        return AskResponse(
            request_id=request_id_var.get(),
            answer=SUPPORT_MESSAGE,
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        ), False, "support"

    # 0b. Input rail: flag an instruction-override attempt in the question. The question is
    #     still answered from the documents; the flag makes the attempt visible and counted.
    flags = guardrails.scan(question)
    if flags:
        prom.INJECTION_FLAGGED.labels(rail="input").inc()
        trace.guardrail("input", "flagged", rules=[f.rule for f in flags],
                        matches=[f.match for f in flags], then="answered from the documents")

    # 1. Semantic cache lookup
    if cache is not None and not cache.should_skip_cache(question):
        with _timed(trace, "cache"):
            cached = cache.get(question, query_vec)
        if cached is not None:
            prom.CACHE_HITS.inc()
            # The same fields as the "answered" line, from the cached response; the
            # question text is not logged.
            log.info("cache_hit", extra={
                "request_id": request_id_var.get(),
                "top_k": settings.top_k,
                "chunk_ids": [r.chunk_id for r in cached.retrieved],
                "scores": [round(r.score, 4) for r in cached.retrieved],
                "answered_from_context": cached.answered_from_context,
                "cache": "hit",
            })
            # The prompt is in the trace of the request that filled the cache.
            trace.set(cache="hit", cache_source_trace=cached.request_id,
                      cache_generation=getattr(cache, "generation_id", None),
                      retrieved=[{"rank": i, **r.model_dump()}
                                 for i, r in enumerate(cached.retrieved, 1)])
            cached.request_id = request_id_var.get()
            return cached, True, "cache_hit"
        prom.CACHE_MISSES.inc()
        trace.set(cache="miss", cache_generation=getattr(cache, "generation_id", None))

    # 2. Retrieve top-k passages. The Chroma retriever reuses the route's query embedding.
    embed_before = trace.record["latency_ms"].get("embed", 0.0)
    t0 = time.perf_counter()
    try:
        if query_vec is not None and retriever.name == "chroma":
            passages = retriever.retrieve(question, k=settings.top_k,  # type: ignore[call-arg]
                                          query_vec=query_vec)
        else:
            passages = retriever.retrieve(question, k=settings.top_k)
    except Exception as exc:
        log.error("Retrieval error: %s", exc)
        prom.FALLBACKS.inc()
        trace.set(error=f"retrieval: {type(exc).__name__}: {exc}"[:300])
        return AskResponse(
            request_id=request_id_var.get(),
            answer="The retrieval service is temporarily unavailable. Please try again.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        ), False, "retrieval_error"
    retrieval_ms = (time.perf_counter() - t0) * 1000
    prom.RETRIEVAL_LATENCY.observe(retrieval_ms / 1000)
    # If the retriever embedded the question itself, that time is the embed stage.
    embedded_inside = trace.record["latency_ms"].get("embed", 0.0) - embed_before
    _observe(trace, "retrieve", retrieval_ms - embedded_inside)
    if passages:
        prom.TOP1_SIMILARITY.observe(passages[0].score)

    retrieved_refs = [
        RetrievedRef(
            chunk_id=p.chunk_id, source=p.source, section=p.section,
            page=p.page, score=p.score,
        )
        for p in passages
    ]
    trace.set(retrieved=[
        {"rank": i, "chunk_id": p.chunk_id, "doc_id": p.doc_id, "doc_version": p.doc_version,
         "superseded": p.superseded, "source": p.source, "section": p.section,
         "page": p.page, "score": round(p.score, 4)}
        for i, p in enumerate(passages, 1)
    ], reranker="none: ordered by similarity")

    # 2b. Retrieval rail: a passage that carries instructions for the model never reaches
    #     the prompt. It stays in `retrieved`, so the evaluation still sees it.
    clean, poisoned = guardrails.split_poisoned(passages)
    for p, pflags in poisoned:
        prom.INJECTION_FLAGGED.labels(rail="retrieval").inc()
        trace.guardrail("retrieval", "dropped_passage", chunk_id=p.chunk_id, doc_id=p.doc_id,
                        source=p.source, section=p.section, score=round(p.score, 4),
                        rules=[f.rule for f in pflags], matches=[f.match for f in pflags])
        log.warning("Dropped a retrieved passage carrying instructions", extra={
            "chunk_id": p.chunk_id, "source": p.source, "rules": [f.rule for f in pflags]})

    # 3. Relevance floor: abstain if no usable passage is above the threshold
    relevance_floor = settings.relevance_floor
    if not clean or all(p.score < relevance_floor for p in clean):
        prom.ABSTENTIONS.inc()
        trace.guardrail("retrieval", "abstain_below_floor", floor=relevance_floor,
                        best=round(max((p.score for p in clean), default=0.0), 4))
        log.info("abstaining", extra={
            "reason": "below_relevance_floor",
            "retriever": retriever.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "answered_from_context": False,
            "floor": relevance_floor,
            "retrieval_ms": round(retrieval_ms, 1),
        })
        return AskResponse(
            request_id=request_id_var.get(),
            answer=ABSTAIN_MESSAGE,
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=retrieved_refs,
            provider=provider.name,
        ), False, "abstain_floor"

    # 4. Generate answer (with retry on ungrounded citations). Only the best
    #    max_context_passages go into the prompt: on a laptop CPU reading the prompt is
    #    the slow part. Citations are checked against exactly those passages, and the
    #    response still lists all top_k retrieved passages (for recall@k). The prompt is
    #    built here too (the providers build the same one) so the trace holds it exactly.
    context = clean[: max(1, settings.max_context_passages)]
    ref_of = {id(p): r for p, r in zip(passages, retrieved_refs)}
    context_refs = [ref_of[id(p)] for p in context]
    prompt = build_prompt(question, context)
    trace.set(context=[p.chunk_id for p in context], prompt=prompt,
              prompt_sha256=tracing.sha256(prompt))
    t0 = time.perf_counter()
    answer = _generate_and_validate(question, context, context_refs, provider)
    generation_ms = (time.perf_counter() - t0) * 1000
    prom.GENERATION_LATENCY.observe(generation_ms / 1000)
    _observe(trace, "generate", generation_ms)

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
    return response, False, _outcome(answer)
