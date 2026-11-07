# ADR 0003: Semantic cache backed by Redis with index generation invalidation

## Status
Accepted · 7 November 2026

## Context

Repeated queries (e.g. "What is the passing CGPA?", "What is the fee per credit hour?") should return instantaneously without repeating vector similarity searches or invoking the host LLM (~15–40 seconds per generation on CPU). We need a caching layer that satisfies three constraints:

1. **One-writer invariant on Chroma:** The ChromaDB StatefulSet has exactly one writer: the indexer Job (`indexer/pipeline/store.py`). The API Deployment pods must remain strictly read-only against Chroma. Using a secondary Chroma collection for the API's query cache would violate this single-writer rule and risk lock contention or corrupt collection state across 3 API replicas.
2. **Cache invalidation on re-indexing without cross-pod commands:** The Calico NetworkPolicy (`k8s/base/networkpolicy.yaml`) isolates Redis so only API pods can connect to port 6379; the indexer Job has zero network egress to Redis. The indexer cannot flush Redis keys when it finishes building a new index generation.
3. **Semantic matching with guarded threshold:** User queries vary in phrasing ("What CGPA puts a student on academic warning?" vs "academic warning CGPA threshold"). The cache must perform semantic similarity matching without returning false hits for subtly different policy questions.

## Decision

We implement a **Redis-backed semantic cache** (`api/app/services/cache.py`):

1. **Store:** We deploy Redis as a standalone Deployment with persistent storage (`k8s/base/redis.yaml`, `redis:7.4.7-alpine`, PVC `cache-data` mounted at `/data` with `appendonly yes`). Redis provides native key-level TTL expiration (`setex`, 86,400 s / 24 h) and atomic set operations (`sadd`, `smembers`).
2. **Generation-namespaced keys (Plan D2):** Every cache entry is namespaced by the active Chroma index generation read from the `rahbar-active` pointer collection:
   `cache:<generation>:<entry_id>`
   `cache_index:<generation>` (Redis set of entry keys for the active generation)
   When the indexer Job flips the pointer to `rahbar-g<timestamp>`, API pods detect the new generation (via a 15 s TTL refresh in `SemanticCache.generation_id`), automatically isolating all subsequent queries from old cache entries. Stale entries expire naturally via Redis TTL without requiring the indexer to communicate with Redis.
3. **Semantic cosine matching:** The cache stores the query vector alongside the cached `AskResponse`. On incoming queries, the retriever embeds the question once, and the cache evaluates cosine similarity against vectors stored under the active generation index. A cache hit occurs only when similarity $\ge$ `CACHE_THRESHOLD`.
4. **Never-cache rules:** The cache strictly rejects:
   - Abstentions (`answered_from_context == False`)
   - Safe fallbacks (`confidence == "low"` and error indications)
   - Follow-up questions detected via heuristic patterns (`is_followup()`)
   - Ungrounded answers where citations were stripped

## Threshold evaluation

Measured with [`eval/cache_threshold.py`](../../eval/cache_threshold.py) against nomic-embed-text on host Ollama; the full output is in [`docs/evidence/cache-threshold.txt`](../evidence/cache-threshold.txt). Ten paraphrase pairs should hit; ten similar-but-different pairs must not.

| Threshold | Paraphrase hits | False hits | Verdict |
| --- | ---: | ---: | --- |
| 0.85 | 9/10 | 2/10 | Rejected: false hits (below). |
| 0.90 | 5/10 | 2/10 | Rejected: the same two false hits. |
| 0.95 | 3/10 | 2/10 | Rejected. This was our placeholder until we measured it. |
| 0.96 | 3/10 | 0/10 | Lowest threshold with no false hit, but only 0.007 above the worst one. |
| **0.97** | **1/10** | **0/10** | **Shipped** (`CACHE_THRESHOLD` in `k8s/base/configmap.yaml`). |
| 0.99 | 0/10 | 0/10 | Rejected: the cache would never hit. |

The false hits that ruled out 0.95 are the dangerous kind, because one word changes the answer:

- "Can a student register for **more than five** courses in a semester?" vs "... **fewer than three** courses ..." scored **0.953**.
- "What does clause **1.21** ... require?" vs "What does clause **1.22** ... require?" scored **0.951**.

A cache miss costs one LLM call; a false hit serves the wrong policy with citations that look valid. So we take 0.97, one step above the lowest safe value, and accept that the cache mostly catches near-identical wording (the fee-per-credit-hour pair at 0.979). The paraphrases it misses, such as "academic warning CGPA threshold" (0.883), go to the LLM as normal. Ten pairs per side is a small sample; the script stays in the repo so the threshold can be re-measured whenever `EMBED_MODEL` changes.

## Consequences

- **What this makes easier:**
  - Zero-downtime cache invalidation: re-indexing flips the Chroma pointer and immediately decouples the cache from outdated answers without requiring Redis flush commands or pod restarts.
  - A cache hit skips retrieval ranking and the LLM call (15–40 s on our CPU) and is marked with the `X-Cache: hit` header.
  - Strict preservation of the one-writer rule on ChromaDB.
- **What this makes harder:**
  - Brute-force cosine comparison across cached vectors runs in memory in Python. At our scale (hundreds of cached questions per generation) this is small next to the embedding call that every question already makes; we have not profiled it separately.
- **What we watch for:**
  - Re-run `eval/cache_threshold.py` after any change of `EMBED_MODEL`: cosine values are model-specific.
  - If question volume grows into tens of thousands of cached queries per generation, Redis vector search (RediSearch) or HNSW indexing should be considered.
