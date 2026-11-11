"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

router = APIRouter(prefix="/api")
log = logging.getLogger("rahbar.api")


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> JSONResponse:
    state = request.app.state
    cache = getattr(state, "cache", None)

    # Embed the question once — reused by both cache lookup and retrieval
    query_vec: list[float] | None = None
    if cache is not None:
        try:
            retriever = state.retriever
            if hasattr(retriever, "_embed_query"):
                query_vec = retriever._embed_query(body.question)
        except Exception as exc:
            # Skip the cache; retrieval embeds again. Logged because on 8 Nov a request took
            # 90.8 s during a re-index and this failure could only be inferred from timings.
            # The question text is not logged.
            log.warning("Cache embedding failed, skipping the cache lookup: %s: %s",
                        type(exc).__name__, exc)

    response, was_cache_hit = answer_question(
        body.question, state.retriever, state.provider, state.settings,
        cache=cache, query_vec=query_vec,
    )

    return JSONResponse(
        content=response.model_dump(),
        headers={"X-Cache": "hit" if was_cache_hit else "miss"},
    )
