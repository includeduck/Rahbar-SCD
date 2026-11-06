"""The real retriever: ChromaDB via the blue/green pointer (sections 3.2, 4.4).

Resolves the ``rahbar-active`` pointer collection to find the current
generation, then queries that generation's collection. Filters out
superseded documents unless the question explicitly mentions an older
version. Converts Chroma cosine *distance* to similarity (1 − distance).
"""

from __future__ import annotations

import logging
import re

import httpx

from ..config import Settings
from ..schemas import Passage

log = logging.getLogger("rahbar.retriever")

POINTER_COLLECTION = "rahbar-active"

# Words that signal the user is asking about a previous / older version
_VERSION_RE = re.compile(
    r"(older|previous|superseded|former|original|2023|aug.2023|before.change)", re.I,
)


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self.settings = settings
        self.client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=ChromaSettings(anonymized_telemetry=False),
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
            pointer = self.client.get_collection(POINTER_COLLECTION)
            meta = pointer.metadata or {}
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
    def retrieve(self, question: str, k: int) -> list[Passage]:
        col_name = self._resolve_collection_name()
        collection = self.client.get_collection(col_name)

        query_vec = self._embed_query(question)

        # Ask for more than k so we can filter superseded and still return k
        fetch_n = min(k * 3, max(collection.count(), k))

        # Decide whether to include superseded documents
        include_superseded = bool(_VERSION_RE.search(question))

        where_filter: dict[str, object] | None = None
        if not include_superseded:
            where_filter = {"superseded": {"$eq": False}}  # type: ignore[dict-item]

        results = collection.query(
            query_embeddings=[query_vec],  # type: ignore[arg-type]
            n_results=fetch_n,
            include=["documents", "metadatas", "distances"],
            where=where_filter,  # type: ignore[arg-type]
        )

        passages: list[Passage] = []
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        metas = (results.get("metadatas") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]

        for chunk_id, text, meta, dist in zip(ids, docs, metas, dists):
            similarity = 1.0 - dist  # cosine distance -> similarity
            passages.append(Passage(
                chunk_id=chunk_id,
                text=text or "",
                source=str(meta.get("source", "")),
                section=str(meta.get("section", "")),
                page=int(meta["page"]) if meta.get("page") is not None else None,  # type: ignore[arg-type]
                score=round(similarity, 6),
            ))

        # Sort by score descending, take top k
        passages.sort(key=lambda p: p.score, reverse=True)
        return passages[:k]
