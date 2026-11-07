"""Readiness (section 4.3) and the embedding-model pin (section 4.4).

The api is ready only when it has something correct to retrieve from: Chroma answers,
the collection exists and is non-empty, it was built with the same embedding model this
process uses, and Redis answers. Any failure names the dependency, so `kubectl describe
pod` and the logs say why a pod is held out of the Service.

Chroma is called over its REST API with explicit timeouts rather than through
chromadb.HttpClient: that client contacts the server in its constructor and its HTTP
session has no timeout, so a slow Chroma would hang the probe instead of failing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..config import Settings

COLLECTIONS = "/api/v2/tenants/default_tenant/databases/default_database/collections"
POINTER_COLLECTION = "rahbar-active"
TIMEOUT_SECONDS = 2.0


class Pingable(Protocol):
    def ping(self) -> Any: ...


@dataclass
class Readiness:
    checks: dict[str, str] = field(default_factory=dict)   # name -> "ok" or the reason
    failed: str | None = None                              # the first check that failed

    @property
    def ready(self) -> bool:
        # ready when no check failed (collection count() > 0, embed_model matches, redis ping ok)
        return self.failed is None

    def fail(self, name: str, reason: str) -> Readiness:
        self.checks[name] = reason
        self.failed = self.failed or name
        return self


def chroma_http_client(settings: Settings) -> httpx.Client:
    """No connection is made until the first request."""
    return httpx.Client(base_url=f"http://{settings.chroma_host}:{settings.chroma_port}",
                        timeout=TIMEOUT_SECONDS)


def pin_conflict(index_metadata: dict[str, Any], settings: Settings) -> str | None:
    """Describe the conflict between the index's embedding model and ours, or None.

    The indexer records embed_model and dimension in the collection metadata. Vectors from
    two different models are not comparable and nothing errors when they are mixed, so a
    mismatch must stop the pod from serving.
    """
    indexed_with = index_metadata.get("embed_model")
    if indexed_with is None:
        return "the collection does not record embed_model; re-run the indexer"
    if indexed_with != settings.embed_model:
        return (f"embedding model mismatch: the index was built with {indexed_with!r} "
                f"(dimension {index_metadata.get('dimension', '?')}), but this api has "
                f"EMBED_MODEL={settings.embed_model!r}")
    return None


def check_chroma(http: httpx.Client, settings: Settings, result: Readiness) -> Readiness:
    try:
        http.get("/api/v2/heartbeat").raise_for_status()
    except httpx.HTTPError as exc:
        return result.fail("chroma", f"unreachable: {type(exc).__name__}")
    result.checks["chroma"] = "ok"

    # Resolve the active collection via the pointer, falling back to
    # settings.collection for backward compat with the dummy indexer.
    collection_name = settings.collection
    try:
        pointer_resp = http.get(f"{COLLECTIONS}/{POINTER_COLLECTION}")
        if pointer_resp.status_code == 200:
            pointer_meta = pointer_resp.json().get("metadata") or {}
            active = pointer_meta.get("active")
            if active and isinstance(active, str):
                collection_name = active
    except httpx.HTTPError:
        pass  # fall back to settings.collection

    response = http.get(f"{COLLECTIONS}/{collection_name}")
    if response.status_code == 404:
        return result.fail("collection", f"{collection_name!r} does not exist yet; "
                                         f"run the indexer Job")
    response.raise_for_status()
    collection = response.json()
    count = int(http.get(f"{COLLECTIONS}/{collection['id']}/count").json())
    if count == 0:
        return result.fail("collection", f"{collection_name!r} is empty")
    result.checks["collection"] = f"ok ({count} chunks)"

    conflict = pin_conflict(collection.get("metadata") or {}, settings)
    if conflict:
        return result.fail("embed_model", conflict)
    result.checks["embed_model"] = f"ok ({settings.embed_model})"
    return result


def check_redis(redis_client: Pingable, result: Readiness) -> Readiness:
    try:
        redis_client.ping()
    except Exception as exc:  # redis-py raises several unrelated types on network errors
        return result.fail("redis", f"unreachable: {type(exc).__name__}")
    result.checks["redis"] = "ok"
    return result


def check_readiness(http: httpx.Client, redis_client: Pingable, settings: Settings) -> Readiness:
    """Run every check, even after one fails, so the response lists them all.
    Checks chroma connection, non-empty collection (count() > 0), embed model pin, and redis."""
    result = Readiness()
    try:
        check_chroma(http, settings, result)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        result.fail("chroma", f"unexpected response: {type(exc).__name__}: {exc}")
    return check_redis(redis_client, result)
