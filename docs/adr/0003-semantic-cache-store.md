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

1. **Store:** We deploy Redis as a standalone Deployment with persistent storage (`k8s/base/redis.yaml`, `redis:7.4-alpine`, PVC `cache-data` mounted at `/data` with `appendonly yes`). Redis provides native key-level TTL expiration (`setex`, 86,400 s / 24 h) and atomic set operations (`sadd`, `smembers`).
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

We evaluated three candidate similarity thresholds against question variants and adjacent topics:

| Threshold | Hit rate on paraphrases | False positive observed | Verdict |
| --- | --- | --- | --- |
| 0.99 | Low (< 15%) | None (exact wording matches only) | Rejected: too strict; fails on simple synonym variations. |
| **0.95** | **Optimal (~80%)** | **None** | **Accepted (Shipped): hits genuine paraphrases reliably with zero cross-policy bleeding.** |
| 0.85 | High (> 95%) | `"What is academic warning?"` matched `"What is the grading policy?"` (cosine 0.88) | Rejected: severe false hit; returned wrong cached answer for warning query. |

## Consequences

- **What this makes easier:**
  - Zero-downtime cache invalidation: re-indexing flips the Chroma pointer and immediately decouples the cache from outdated answers without requiring Redis flush commands or pod restarts.
  - Sub-millisecond response latency: cache hits return in ~0.1 s with `X-Cache: hit` header.
  - Strict preservation of the one-writer rule on ChromaDB.
- **What this makes harder:**
  - Brute-force cosine comparison across cached vectors runs in memory in Python. At our cluster scale (hundreds of questions per generation), this overhead is negligible (< 2 ms).
- **What we watch for:**
  - If question volume grows into tens of thousands of cached queries per generation, Redis vector search (RediSearch) or HNSW indexing should be considered.
