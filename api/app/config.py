"""Runtime configuration, read once from environment variables.

On the cluster these variables come from your ConfigMap and Secret (section 4.1).
Nothing here is baked into the image, so one image runs in every environment.
Add your own keys (relevance floor, cache settings, ...) the same way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    # Which implementations to load. "stub" needs no cluster, no model, no network.
    retriever: str = "stub"          # RETRIEVER: stub | chroma
    llm_provider: str = "stub"       # LLM_PROVIDER: stub | ollama-host | ollama-incluster | hosted

    # The embedding pin (section 4.4). The indexer reads the same ConfigMap key.
    embed_model: str = "nomic-embed-text"

    # Vector store
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    collection: str = "rahbar"

    # Semantic cache (section 3.3). Readiness also requires it to answer (section 4.3).
    redis_url: str = "redis://redis:6379/0"

    # Retrieval and generation
    top_k: int = 5                   # passages retrieved (and returned, so recall@5 is measurable)
    max_context_passages: int = 3    # the best of those that go into the prompt
    temperature: float = 0.2
    llm_model: str = "llama3.2:3b"
    llm_timeout_seconds: float = 120.0   # per generation attempt
    ollama_url: str = "http://host.minikube.internal:11434"
    hosted_base_url: str = ""        # e.g. an OpenAI-compatible endpoint
    llm_api_key: str = field(default="", repr=False)   # from the Secret; never log it

    # Relevance floor: below this similarity, abstain without calling the LLM
    relevance_floor: float = 0.25

    # Semantic cache: a hit needs cosine similarity >= this. Placeholder until phase 11.
    cache_threshold: float = 0.95

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings(
        retriever=os.environ.get("RETRIEVER", "stub"),
        llm_provider=os.environ.get("LLM_PROVIDER", "stub"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=_int("CHROMA_PORT", 8000),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        top_k=_int("TOP_K", 5),
        max_context_passages=_int("MAX_CONTEXT_PASSAGES", 3),
        temperature=_float("TEMPERATURE", 0.2),
        llm_model=os.environ.get("LLM_MODEL", "llama3.2:3b"),
        llm_timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 120.0),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        hosted_base_url=os.environ.get("HOSTED_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        relevance_floor=_float("RELEVANCE_FLOOR", 0.25),
        cache_threshold=_float("CACHE_THRESHOLD", 0.95),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
