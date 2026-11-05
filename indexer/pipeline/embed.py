"""Stage 4: Embed. One vector per chunk, from the pinned model only."""

from __future__ import annotations

import logging
import time

import httpx

from .config import IndexerSettings

logger = logging.getLogger("rahbar.indexer.embed")

# Batch size for embedding requests
BATCH_SIZE = 32

# Timeout for each embedding request (seconds)
TIMEOUT = 60.0

# Number of retries on failure
MAX_RETRIES = 1


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Batch the calls; one request per chunk is slow.
    """
    if not texts:
        return []

    all_embeddings: list[list[float]] = []

    with httpx.Client(timeout=TIMEOUT) as client:
        for batch_start in range(0, len(texts), BATCH_SIZE):
            batch = texts[batch_start:batch_start + BATCH_SIZE]
            embeddings = _embed_batch(client, batch, settings)
            all_embeddings.extend(embeddings)

    if len(all_embeddings) != len(texts):
        raise RuntimeError(
            f"Embedding count mismatch: got {len(all_embeddings)} for {len(texts)} texts"
        )

    return all_embeddings


def _embed_batch(
    client: httpx.Client,
    texts: list[str],
    settings: IndexerSettings,
) -> list[list[float]]:
    """Embed a single batch with retry logic."""
    url = f"{settings.ollama_url.rstrip('/')}/api/embed"
    payload = {
        "model": settings.embed_model,
        "input": texts,
    }

    last_error: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings") or []
            if len(embeddings) != len(texts):
                raise RuntimeError(
                    f"Expected {len(texts)} embeddings, got {len(embeddings)}"
                )
            return embeddings
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = 2.0 * (attempt + 1)
                logger.warning(
                    "Embedding batch failed (attempt %d/%d): %s; retrying in %.0fs",
                    attempt + 1, 1 + MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

    raise RuntimeError(f"Embedding failed after {1 + MAX_RETRIES} attempts: {last_error}")
