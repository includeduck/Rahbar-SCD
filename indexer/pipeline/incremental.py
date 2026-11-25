"""Stages 3 to 5, incremental (Assignment 03, A2; RAGOps paper 4.2.3 and 4.2.4).

One long-lived collection per pipeline configuration, named after the embedding model and
the chunk settings (collection_name), so a candidate with other settings (Part C) gets its own
collection. Per document, compared with the ledger:

  unchanged (same doc_id, same SHA-256, all chunks present)  -> 0 chunks embedded
  new or changed                                             -> its old chunks deleted, new
                                                                ones embedded and added
  archived by the recency check                              -> superseded=true on its chunks
  restored (pinned), or no longer superseded                 -> superseded=false
  quarantined, or gone from the input folders                -> all its chunks deleted

so a deleted document leaves no orphan chunks (python -m pipeline.lake counts proves it).
The collection is the only thing written, and only by the ingestion Job (ADR 0001).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import IndexerSettings
from .lake import Ledger, keep_copy, slug
from .models import Chunk
from .verify import Verdict

ADD_BATCH = 100


def collection_name(settings: IndexerSettings) -> str:
    """Example: rahbar-nomic-embed-text-c400o50. COLLECTION_NAME overrides it (Part F)."""
    if settings.collection_override:
        return settings.collection_override
    return f"rahbar-{slug(settings.embed_model)}-c{settings.chunk_size}o{settings.chunk_overlap}"


def chunk_id(doc_id: str, sha256: str, index: int) -> str:
    return hashlib.sha1(f"{doc_id}|{sha256}|{index}".encode()).hexdigest()[:16]


@dataclass
class SyncResult:
    collection: str
    embedded_chunks: int = 0
    reused_chunks: int = 0
    deleted_chunks: int = 0
    docs: dict[str, list[str]] = field(default_factory=lambda: {
        "added": [], "replaced": [], "unchanged": [], "archived": [], "restored": [],
        "quarantined": [], "deleted": []})

    def summary(self) -> dict[str, Any]:
        return {"collection": self.collection, "embedded_chunks": self.embedded_chunks,
                "reused_chunks": self.reused_chunks, "deleted_chunks": self.deleted_chunks,
                **{k: len(v) for k, v in self.docs.items()}, "documents": self.docs}


def open_collection(client: Any, settings: IndexerSettings, dimension: int | None) -> Any:
    name = collection_name(settings)
    col = client.get_or_create_collection(name=name, metadata={
        "hnsw:space": "cosine", "embed_model": settings.embed_model,
        "chunk_size": settings.chunk_size, "chunk_overlap": settings.chunk_overlap,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **({"dimension": dimension} if dimension else {}),
    })
    recorded = (col.metadata or {}).get("embed_model")
    if recorded and recorded != settings.embed_model:
        raise RuntimeError(f"collection {name} was built with {recorded}, not "
                           f"{settings.embed_model}; a new model needs a new collection")
    return col


def _ids_for(col: Any, doc_id: str) -> list[str]:
    return list(col.get(where={"doc_id": doc_id}, include=[])["ids"])


def sync(verdicts: list[Verdict], ledger: Ledger, settings: IndexerSettings, client: Any,
         embedder: Callable[[list[str]], list[list[float]]],
         chunker: Callable[..., list[Chunk]], root: Path) -> SyncResult:
    dimension = len(embedder(["dimension probe"])[0])
    col = open_collection(client, settings, dimension)
    result = SyncResult(collection=col.name)
    present = {v.doc.doc_id for v in verdicts}

    # 1. Quarantined documents, and indexed documents no longer in any input folder.
    for v in verdicts:
        if v.decision == "quarantine":
            ids = _ids_for(col, v.doc.doc_id) if v.doc.doc_id in ledger.docs else []
            # A failed copy of a document that is still indexed (an exact duplicate under
            # another name) must not delete the original: ids only match its own doc_id.
            if ids and ledger.docs[v.doc.doc_id].get("sha256") != v.doc.sha256:
                col.delete(ids=ids)
                result.deleted_chunks += len(ids)
                ledger.docs[v.doc.doc_id].update(status="quarantined", chunks=0)
            result.docs["quarantined"].append(v.doc.doc_id)
    for doc_id, entry in sorted(ledger.docs.items()):
        if entry.get("status") in ("active", "archived") and doc_id not in present:
            ids = _ids_for(col, doc_id)
            if ids:
                col.delete(ids=ids)
            result.deleted_chunks += len(ids)
            entry.update(status="deleted", chunks=0,
                         deleted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            result.docs["deleted"].append(doc_id)

    # 2. Accepted and archived documents: embed only what is new or changed.
    for v in sorted(verdicts, key=lambda v: (v.doc.batch, v.doc.doc_id)):
        if v.decision == "quarantine" or v.document is None:
            continue
        doc = v.doc
        superseded = v.decision == "archive" or (doc.superseded_declared
                                                 and doc.family == doc.doc_id)
        prev = ledger.docs.get(doc.doc_id)
        existing = _ids_for(col, doc.doc_id)
        unchanged = (prev is not None and prev.get("sha256") == doc.sha256
                     and prev.get("status") in ("active", "archived")
                     and len(existing) == prev.get("chunks"))
        if unchanged and prev is not None:
            result.reused_chunks += len(existing)
            if bool(prev.get("superseded")) != superseded:
                got = col.get(ids=existing, include=["metadatas"])
                col.update(ids=got["ids"],
                           metadatas=[{**m, "superseded": superseded} for m in got["metadatas"]])
                result.docs["archived" if superseded else "restored"].append(doc.doc_id)
            else:
                result.docs["unchanged"].append(doc.doc_id)
        else:
            if existing:
                col.delete(ids=existing)
                result.deleted_chunks += len(existing)
            pieces = chunker(v.document, settings.chunk_size, settings.chunk_overlap)
            for i, c in enumerate(pieces):
                c.id = chunk_id(doc.doc_id, doc.sha256, i)
                c.doc_id, c.family, c.title, c.content_hash = (doc.doc_id, doc.family,
                                                               doc.title, doc.sha256)
                c.doc_version, c.superseded = doc.doc_version, superseded
            vectors = embedder([c.text for c in pieces]) if pieces else []
            for start in range(0, len(pieces), ADD_BATCH):
                part = pieces[start:start + ADD_BATCH]
                col.add(ids=[c.id for c in part], documents=[c.text for c in part],
                        embeddings=vectors[start:start + ADD_BATCH],
                        metadatas=[c.metadata() for c in part])
            result.embedded_chunks += len(pieces)
            kind = "replaced" if prev and prev.get("status") in ("active", "archived") \
                else "added"
            result.docs[kind].append(doc.doc_id)
            if superseded and kind == "added":
                result.docs["archived"].append(doc.doc_id)
            existing = [c.id for c in pieces]
        keep_copy(root, "archive" if superseded else "lake", doc)
        ledger.docs[doc.doc_id] = {
            "file": doc.path.name, "batch": doc.batch, "family": doc.family,
            "title": doc.title, "doc_version": doc.doc_version, "sha256": doc.sha256,
            "chunks": len(existing), "superseded": superseded,
            "status": "archived" if superseded else "active",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    if settings.update_pointer:
        from .store import _update_pointer
        _update_pointer(client, col.name, int(time.time()))
    return result
