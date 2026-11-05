"""Readiness (section 4.3) and the embedding-model pin (section 4.4).

Chroma is faked with httpx.MockTransport and Redis with a stub object: no network.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.readiness import COLLECTIONS, check_readiness, pin_conflict

SETTINGS = Settings(embed_model="nomic-embed-text", collection="rahbar")


class FakeRedis:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def ping(self) -> bool:
        if not self.up:
            raise ConnectionError("redis is down")
        return True


def fake_chroma(*, exists: bool = True, count: int = 3, embed_model: str = "nomic-embed-text",
                up: bool = True) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if not up:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/v2/heartbeat":
            return httpx.Response(200, json={"nanosecond heartbeat": 1})
        if path == f"{COLLECTIONS}/rahbar":
            if not exists:
                return httpx.Response(404, json={"error": "NotFoundError"})
            return httpx.Response(200, json={"id": "c1", "name": "rahbar", "metadata": {
                "embed_model": embed_model, "dimension": 768, "hnsw:space": "cosine"}})
        if path == f"{COLLECTIONS}/c1/count":
            return httpx.Response(200, json=count)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://chroma:8000")


def test_ready_when_every_dependency_answers() -> None:
    result = check_readiness(fake_chroma(), FakeRedis(), SETTINGS)
    assert result.ready
    assert set(result.checks) == {"chroma", "collection", "embed_model", "redis"}
    assert result.checks["collection"] == "ok (3 chunks)"


def test_not_ready_before_the_indexer_has_run() -> None:
    result = check_readiness(fake_chroma(exists=False), FakeRedis(), SETTINGS)
    assert result.failed == "collection"
    assert "does not exist" in result.checks["collection"]


def test_not_ready_on_an_empty_collection() -> None:
    result = check_readiness(fake_chroma(count=0), FakeRedis(), SETTINGS)
    assert result.failed == "collection"
    assert "empty" in result.checks["collection"]


def test_embedding_model_mismatch_blocks_readiness_and_names_both_models() -> None:
    result = check_readiness(fake_chroma(embed_model="dummy-768"), FakeRedis(), SETTINGS)
    assert result.failed == "embed_model"
    message = result.checks["embed_model"]
    assert "'dummy-768'" in message and "'nomic-embed-text'" in message


def test_index_without_a_recorded_model_is_not_trusted() -> None:
    assert pin_conflict({"dimension": 768}, SETTINGS) is not None
    assert pin_conflict({"embed_model": "nomic-embed-text"}, SETTINGS) is None


def test_unreachable_chroma_is_named_and_redis_is_still_checked() -> None:
    result = check_readiness(fake_chroma(up=False), FakeRedis(up=False), SETTINGS)
    assert result.failed == "chroma"
    assert result.checks["chroma"].startswith("unreachable")
    assert result.checks["redis"].startswith("unreachable")


def test_unreachable_redis_blocks_readiness() -> None:
    result = check_readiness(fake_chroma(), FakeRedis(up=False), SETTINGS)
    assert result.failed == "redis"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_readyz_returns_503_naming_the_failed_dependency(client: TestClient) -> None:
    client.app.state.chroma_http = fake_chroma(embed_model="dummy-768")  # type: ignore[attr-defined]
    client.app.state.redis = FakeRedis()  # type: ignore[attr-defined]
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["failed"] == "embed_model"


def test_readyz_returns_200_when_ready(client: TestClient) -> None:
    client.app.state.chroma_http = fake_chroma()  # type: ignore[attr-defined]
    client.app.state.redis = FakeRedis()  # type: ignore[attr-defined]
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
