"""The real retriever: ChromaDB via REST with explicit timeouts (sections 3.2, 4.4).

Uses raw REST calls instead of ``chromadb.HttpClient`` because that client
contacts Chroma in its constructor (crash at startup if Chroma is down) and
its HTTP session has no timeout (a slow Chroma can hang ``/api/ask``).

Resolves the ``rahbar-active`` pointer collection to find the current
generation, then queries that generation's collection. Filters out
superseded documents unless the question explicitly mentions an older
version. Converts Chroma cosine *distance* to similarity (1 − distance).
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from ..config import Settings
from ..schemas import Passage
from ..services import tracing

log = logging.getLogger("rahbar.retriever")

POINTER_COLLECTION = "rahbar-active"
COLLECTIONS = "/api/v2/tenants/default_tenant/databases/default_database/collections"
TIMEOUT = 10.0

# Words that signal the user is asking about a previous / older version.
# Deliberately does NOT match bare "2023" — the 2023-24 annual report is the
# current edition; only match phrases like "aug 2023 rules" or "older version".
_VERSION_RE = re.compile(
    r"(older\s+version|previous\s+version|superseded|former\s+rules|"
    r"original\s+rules|before\s+(the\s+)?change|aug(ust)?\s*2023\s+rules)", re.I,
)


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # REST client with an explicit timeout — no connection on init
        self._chroma = httpx.Client(
            base_url=f"http://{settings.chroma_host}:{settings.chroma_port}",
            timeout=TIMEOUT,
        )
        self._embed_client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Pointer resolution
    # ------------------------------------------------------------------
    def _resolve_collection_name(self) -> str:
        """Return the active generation collection name, or fall back to
        ``settings.collection`` for backward compatibility with the dummy
        indexer (which writes directly to ``rahbar``)."""
        try:
            resp = self._chroma.get(f"{COLLECTIONS}/{POINTER_COLLECTION}")
            if resp.status_code == 200:
                meta = resp.json().get("metadata") or {}
                active = meta.get("active")
                if active and isinstance(active, str):
                    return active
        except Exception:
            log.debug("pointer collection %s not found; falling back to %s",
                      POINTER_COLLECTION, self.settings.collection)
        return self.settings.collection

    # ------------------------------------------------------------------
    # Embedding (same model the indexer used)
    # ------------------------------------------------------------------
    def _embed_query(self, text: str) -> list[float]:
        url = f"{self.settings.ollama_url.rstrip('/')}/api/embed"
        resp = self._embed_client.post(url, json={
            "model": self.settings.embed_model,
            "input": [text],
        })
        resp.raise_for_status()
        embeddings = resp.json().get("embeddings") or []
        if not embeddings:
            raise RuntimeError("Ollama returned no embeddings for the query")
        return embeddings[0]

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------
    def retrieve(self, question: str, k: int,
                 query_vec: list[float] | None = None) -> list[Passage]:
        """Top-k passages for the question. ``query_vec`` is the question's embedding when
        the caller already has it (the route embeds once for the cache), so the question
        is not embedded twice. What was searched goes into the request's trace."""
        trace = tracing.current()
        col_name = self._resolve_collection_name()

        # Get collection info
        col_resp = self._chroma.get(f"{COLLECTIONS}/{col_name}")
        col_resp.raise_for_status()
        col_data = col_resp.json()
        col_id = col_data["id"]

        # Get collection count
        count_resp = self._chroma.get(f"{COLLECTIONS}/{col_id}/count")
        count_resp.raise_for_status()
        total = int(count_resp.json())

        if query_vec is None:
            t0 = time.perf_counter()
            query_vec = self._embed_query(question)
            if trace:
                trace.stage("embed", (time.perf_counter() - t0) * 1000)

        # Ask for more than k so we can filter superseded and still return k
        fetch_n = min(k * 3, max(total, k))

        # Decide whether to include superseded documents
        version_match = _VERSION_RE.search(question)
        include_superseded = bool(version_match)

        query_body: dict[str, object] = {
            "query_embeddings": [query_vec],
            "n_results": fetch_n,
            "include": ["documents", "metadatas", "distances"],
        }
        if not include_superseded:
            query_body["where"] = {"superseded": {"$eq": False}}

        resp = self._chroma.post(
            f"{COLLECTIONS}/{col_id}/query", json=query_body,
        )
        resp.raise_for_status()
        results = resp.json()

        passages: list[Passage] = []
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        for chunk_id, text, meta, dist in zip(ids, docs, metas, dists):
            similarity = 1.0 - dist  # cosine distance -> similarity
            page_val = meta.get("page")
            passages.append(Passage(
                chunk_id=chunk_id,
                text=text or "",
                source=str(meta.get("source", "")),
                section=str(meta.get("section", "")),
                page=int(page_val) if page_val is not None else None,
                score=round(similarity, 6),
                doc_id=meta.get("doc_id"),
                doc_version=(str(meta["doc_version"]) if meta.get("doc_version") is not None
                             else None),
                superseded=meta.get("superseded"),
            ))

        # Sort by score descending, take top k
        passages.sort(key=lambda p: p.score, reverse=True)
        if trace:
            col_meta = col_data.get("metadata") or {}
            trace.set(
                collection={"name": col_name, "id": col_id, "chunks": total,
                            "embed_model": col_meta.get("embed_model"),
                            "dimension": col_meta.get("dimension")},
                filters={"where": query_body.get("where"), "n_results": fetch_n, "k": k,
                         "superseded_included": include_superseded,
                         "why": (f"question mentions an older version: "
                                 f"{version_match.group(0)!r}" if version_match
                                 else "superseded versions excluded by default")},
            )
        return passages[:k]
