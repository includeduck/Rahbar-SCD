"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

router = APIRouter(prefix="/api")


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request) -> JSONResponse:
    state = request.app.state
    cache = getattr(state, "cache", None)

    # Check cache first for X-Cache header
    if cache is not None and not cache.should_skip_cache(body.question):
        cached = cache.get(body.question)
        if cached is not None:
            from ..logjson import request_id_var
            cached.request_id = request_id_var.get()
            return JSONResponse(
                content=cached.model_dump(),
                headers={"X-Cache": "hit"},
            )

    response = answer_question(
        body.question, state.retriever, state.provider, state.settings, cache=cache,
    )

    return JSONResponse(
        content=response.model_dump(),
        headers={"X-Cache": "miss"},
    )
