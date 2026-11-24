"""Assignment 03, Parts C and F: an api Deployment pinned to one collection
(RETRIEVAL_COLLECTION), so the live api and a candidate serve different collections and a
release is a change that `kubectl rollout undo` can reverse."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.retrieval.chroma import ChromaRetriever
from app.services import cache as cache_mod
from app.services.cache import SemanticCache
from app.services.readiness import COLLECTIONS, POINTER_COLLECTION, check_readiness

LIVE, CANDIDATE = "rahbar-nomic-embed-text-c400o50", "rahbar-nomic-embed-text-c250o50"


class Redis:
    def ping(self) -> bool:
        return True


def chroma(seen: list[str]) -> httpx.Client:
    """The pointer names LIVE; both collections exist; requests are recorded."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/api/v2/heartbeat":
            return httpx.Response(200, json={})
        if path == f"{COLLECTIONS}/{POINTER_COLLECTION}":
            return httpx.Response(200, json={"metadata": {"active": LIVE, "generation": 7}})
        for name, cid in ((LIVE, "c-live"), (CANDIDATE, "c-cand")):
            if path == f"{COLLECTIONS}/{name}":
                return httpx.Response(200, json={"id": cid, "name": name, "metadata": {
                    "embed_model": "nomic-embed-text", "dimension": 768}})
            if path == f"{COLLECTIONS}/{cid}/count":
                return httpx.Response(200, json=10)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://chroma:8000")


def test_an_unpinned_api_follows_the_pointer_and_a_pinned_one_does_not() -> None:
    unpinned = ChromaRetriever(Settings(retriever="chroma"))
    unpinned._chroma = chroma([])
    assert unpinned._resolve_collection_name() == LIVE
    seen: list[str] = []
    pinned = ChromaRetriever(Settings(retriever="chroma", retrieval_collection=CANDIDATE))
    pinned._chroma = chroma(seen)
    assert pinned._resolve_collection_name() == CANDIDATE
    assert seen == []                                   # the pointer is not even read


def test_readiness_checks_the_pinned_collection() -> None:
    seen: list[str] = []
    result = check_readiness(chroma(seen), Redis(),
                             Settings(retrieval_collection=CANDIDATE))
    assert result.ready
    assert f"{COLLECTIONS}/{CANDIDATE}" in seen
    assert f"{COLLECTIONS}/{POINTER_COLLECTION}" not in seen


def test_live_and_candidate_never_share_cache_entries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cache_mod, "_get_generation_id", lambda s: "7")
    live = SemanticCache(None, Settings(retrieval_collection=LIVE))
    cand = SemanticCache(None, Settings(retrieval_collection=CANDIDATE))
    unpinned = SemanticCache(None, Settings())
    assert live.generation_id == f"{LIVE}@7"
    assert cand.generation_id == f"{CANDIDATE}@7"
    assert unpinned.generation_id == "7"                # unchanged for unpinned readers
