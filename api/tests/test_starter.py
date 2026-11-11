"""Starter tests: they pin down the contract the UI and eval script rely on.

They run with the stub retriever and stub provider: no cluster, no model, no
network. Keep them passing, and add your own next to them (section 6).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_liveness_answers_without_dependencies(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_ask_returns_the_response_contract(client: TestClient) -> None:
    from app.schemas import AskResponse

    r = client.post("/api/ask", json={"question": "What is the attendance requirement?"})
    assert r.status_code == 200
    body = AskResponse(**r.json())          # raises if any field is missing or mistyped
    assert body.provider == "stub"
    assert len(body.retrieved) == 3


def test_every_citation_names_a_retrieved_passage(client: TestClient) -> None:
    r = client.post("/api/ask", json={"question": "What does the stub say?"}).json()
    retrieved = {(p["source"], p["section"]) for p in r["retrieved"]}
    assert r["citations"]
    for c in r["citations"]:
        assert (c["source"], c["section"]) in retrieved


def test_request_id_is_echoed(client: TestClient) -> None:
    r = client.post("/api/ask", json={"question": "Echo my id please"},
                    headers={"X-Request-ID": "test-123"})
    assert r.headers["X-Request-ID"] == "test-123"
    assert r.json()["request_id"] == "test-123"


@pytest.mark.parametrize("question", ["hi", "x" * 501])
def test_question_length_is_enforced(client: TestClient, question: str) -> None:
    r = client.post("/api/ask", json={"question": question})
    assert r.status_code == 422


def test_metrics_count_requests_by_route(client: TestClient) -> None:
    client.post("/api/ask", json={"question": "Count this request"})
    text = client.get("/metrics").text
    assert 'rahbar_requests_total{path="/api/ask",status="200"}' in text


def test_failed_cache_embedding_is_logged_and_the_request_still_answers(
        client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """On 8 Nov a request took 90.8 s during a re-index because this embedding call timed
    out, and nothing said so. It must be logged (without the question) and must not fail."""

    class SlowEmbedder:
        def __init__(self, inner: object) -> None:
            self._inner = inner
            self.name = getattr(inner, "name", "stub")

        def _embed_query(self, text: str) -> list[float]:
            raise TimeoutError("timed out")

        def retrieve(self, question: str, k: int):  # type: ignore[no-untyped-def]
            return self._inner.retrieve(question, k)  # type: ignore[attr-defined]

    class EmptyCache:
        """The route only embeds when a cache is configured; this one never hits."""

        def should_skip_cache(self, question: str, response: object = None) -> bool:
            return False

        def get(self, question: str, query_vec: object) -> None:
            return None

        def put(self, question: str, response: object, query_vec: object) -> None:
            return None

    state = client.app.state  # type: ignore[attr-defined]
    original = state.retriever
    state.retriever = SlowEmbedder(original)
    cache_was = getattr(state, "cache", None)
    state.cache = EmptyCache()
    try:
        with caplog.at_level("WARNING", logger="rahbar.api"):
            r = client.post("/api/ask", json={"question": "A secret question text"})
    finally:
        state.retriever = original
        state.cache = cache_was
    assert r.status_code == 200
    warnings = [rec.getMessage() for rec in caplog.records if rec.name == "rahbar.api"]
    assert any("Cache embedding failed" in m and "TimeoutError" in m for m in warnings)
    assert all("secret question" not in m for m in warnings)
