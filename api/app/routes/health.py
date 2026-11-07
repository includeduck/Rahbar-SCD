"""Liveness and readiness (section 4.3). They answer different questions."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from ..services.readiness import Readiness, check_readiness

router = APIRouter()
log = logging.getLogger("rahbar.health")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Can this pod take traffic now? 503 naming the failed dependency if not.

    A 503 removes the pod from the Service without restarting it, so a slow Chroma
    holds pods back instead of killing them.
    """
    state = request.app.state
    result = check_readiness(state.chroma_http, state.redis, state.settings)
    report_change(state, result)
    if not result.ready or len(result.checks) == 0:
        response.status_code = 503
        return {"status": "not ready", "failed": result.failed, "checks": result.checks}
    return {"status": "ready", "checks": result.checks}


def report_change(state: object, result: Readiness) -> None:
    """Log when readiness changes, not on every probe (one every 10 s per pod)."""
    previous = getattr(state, "readiness_failed", "unknown")
    if result.failed == previous:
        return
    setattr(state, "readiness_failed", result.failed)
    if result.ready:
        log.info("ready", extra={"checks": result.checks})
    else:
        log.warning("not ready", extra={"failed": result.failed, "checks": result.checks})
