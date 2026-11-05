"""Stage 5: Store. The only code in the whole system that writes to Chroma."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import IndexerSettings
from .models import Chunk

logger = logging.getLogger("rahbar.indexer.store")

# Batch size for Chroma add operations
ADD_BATCH_SIZE = 100

# Name of the pointer collection that tracks which generation is active
POINTER_COLLECTION = "rahbar-active"


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count.

    Uses a blue/green strategy: builds a new collection, then flips a pointer so the
    API switches atomically without an empty window. Keeps one old generation for rollback.
    """
    if not chunks or not vectors:
        raise ValueError("No chunks or vectors to store")

    client = get_client(settings)
    generation = int(time.time())
    new_name = f"rahbar-g{generation}"
    dimension = len(vectors[0])

    logger.info("Creating collection %s (%d chunks, dim=%d)", new_name, len(chunks), dimension)

    # 1. Create a new collection with full metadata
    collection = client.create_collection(
        name=new_name,
        metadata={
            "hnsw:space": "cosine",
            "embed_model": settings.embed_model,
            "dimension": dimension,
            "generation": generation,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
        },
    )

    # 2. Add chunks in batches
    for batch_start in range(0, len(chunks), ADD_BATCH_SIZE):
        batch_end = min(batch_start + ADD_BATCH_SIZE, len(chunks))
        batch_chunks = chunks[batch_start:batch_end]
        batch_vectors = vectors[batch_start:batch_end]

        collection.add(
            ids=[c.id for c in batch_chunks],
            documents=[c.text for c in batch_chunks],
            embeddings=batch_vectors,
            metadatas=[c.metadata() for c in batch_chunks],
        )

    # 3. Verify the new collection
    count = int(collection.count())
    if count == 0:
        raise RuntimeError(f"Collection {new_name} is empty after adding {len(chunks)} chunks")

    logger.info("Collection %s has %d chunks", new_name, count)

    # 4. Flip the pointer: create or update the pointer collection
    _update_pointer(client, new_name, generation)

    # 5. Clean up old generations (keep the previous one for rollback)
    _cleanup_old_generations(client, generation)

    return count


def _update_pointer(client: Any, active_name: str, generation: int) -> None:
    """Create or update the pointer collection to point to the active generation."""
    try:
        pointer = client.get_or_create_collection(
            name=POINTER_COLLECTION,
            metadata={"active": active_name, "generation": generation},
        )
        # Update metadata to point to the new collection
        pointer.modify(metadata={"active": active_name, "generation": generation})
        logger.info("Pointer updated: %s -> %s (gen %d)", POINTER_COLLECTION, active_name, generation)
    except Exception as exc:
        logger.error("Failed to update pointer: %s", exc)
        raise


def _cleanup_old_generations(client: Any, current_generation: int) -> None:
    """Delete generation collections older than the previous one."""
    try:
        collections = client.list_collections()
    except Exception:
        logger.warning("Could not list collections for cleanup")
        return

    generation_collections: list[tuple[int, str]] = []
    for col in collections:
        name = col.name if hasattr(col, "name") else str(col)
        if name.startswith("rahbar-g"):
            try:
                gen = int(name[len("rahbar-g"):])
                generation_collections.append((gen, name))
            except ValueError:
                continue

    # Sort by generation, newest first
    generation_collections.sort(reverse=True)

    # Keep the current and the one before it; delete anything older
    kept = 0
    for gen, name in generation_collections:
        if kept < 2:
            kept += 1
            continue
        try:
            client.delete_collection(name)
            logger.info("Deleted old collection: %s", name)
        except Exception as exc:
            logger.warning("Could not delete %s: %s", name, exc)
