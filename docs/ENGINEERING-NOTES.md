# Engineering Notes

This document provides technical answers to the seven engineering questions posed in the RAHBAR case study, referencing the exact file paths, line numbers, and empirical measurements from our cluster.

---

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

Our API Deployment runs 3 replicas ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L12): `replicas: 3`), while ChromaDB is deployed as a single StatefulSet backed by a single ReadWriteOnce persistent volume ([`k8s/base/chroma.yaml`](../k8s/base/chroma.yaml#L28-L34): `volumeClaimTemplates` `chroma-data`).

If document indexing were executed inside API startup (e.g. during `app.main:lifespan`), four major failure modes occur:

1. **Concurrent SQLite/HNSW Corruption:** Three API pods starting simultaneously would each run `extract -> clean -> chunk -> embed -> store`, writing to the shared Chroma collection at the same time. ChromaDB's underlying SQLite metadata database and HNSW segment files are not designed for concurrent multi-process writes, resulting in deadlocks, partial batch commits, and index corruption.
2. **Cascading Re-index Storms:** Every pod lifecycle event—a rolling update, node drain, liveness probe failure, or an OOMKill—would trigger a complete re-index of the 9-document corpus, incurring ~8.5 minutes of OCR and embedding computation ([`docs/evidence/real-pipeline-first-run.txt`](evidence/real-pipeline-first-run.txt#L36)).
3. **Readiness Probe Deadlock or False Readiness:** The API startup probe allows at most 60 seconds ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L45-L50): 30 attempts × 2 s). Because full corpus indexing takes 500 seconds, the startup probe would fail, causing kubelet to terminate the pod before it ever finished. Conversely, if `/readyz` did not block on indexing, the pod would serve user queries against an empty or partially indexed vector database, returning empty or hallucinated answers.
4. **Inability to Re-index Without Redeployment:** Adding or updating documents in the corpus would require redeploying or restarting the API workload.

**Resolution:** We isolate indexing into a batch Kubernetes Job ([`k8s/base/indexer-job.yaml`](../k8s/base/indexer-job.yaml)) and weekly CronJob ([`k8s/base/reindex-cronjob.yaml`](../k8s/base/reindex-cronjob.yaml)), with a blue/green pointer swap in [`indexer/pipeline/store.py`](../indexer/pipeline/store.py#L80-L98) (`rahbar-active` pointer). The API is strictly read-only. See [`docs/adr/0001-indexer-as-job.md`](adr/0001-indexer-as-job.md).

---

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

The liveness probe determines if a pod is healthy or should be killed and restarted; the readiness probe determines if a pod should receive traffic from the Kubernetes Service.

- **Our implementation:**
  - **Liveness** ([`api/app/routes/health.py`](../api/app/routes/health.py#L15-L18)): `/healthz` performs an in-memory check and returns `{"status": "alive"}` without contacting Chroma, Redis, or Ollama.
  - **Readiness** ([`api/app/routes/health.py`](../api/app/routes/health.py#L21-L34) and [`api/app/services/readiness.py`](../api/app/services/readiness.py#L116-L122)): `/readyz` validates that Chroma is reachable, the collection exists and is non-empty (`count > 0`), the embedding model matches `EMBED_MODEL`, and Redis responds to `ping()`.

**If readiness and liveness were swapped:**
If the liveness probe called `/readyz` and Chroma became unresponsive or slow for 90 seconds (e.g. during a heavy batch embedding or re-index):
1. The liveness probe would fail three consecutive times ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L52-L56): `periodSeconds: 10`, `failureThreshold: 3` = 30 seconds).
2. Kubelet would kill all 3 API replica pods simultaneously and restart their containers.
3. Upon restart, all 3 newly created pods would immediately bombard the already-degraded Chroma instance with connection attempts, further increasing Chroma latency and causing the new pods' liveness probes to fail as well.
4. This produces a catastrophic **cluster-wide restart storm** where no API pod remains alive, amplifying a temporary storage delay into total system downtime.

With the correct configuration, a 90-second Chroma outage causes `/readyz` to return HTTP 503. The pods are temporarily removed from Service endpoints (`kubectl get endpoints` drops the IPs), shielding users from failed requests while keeping the containers alive until Chroma recovers.

---

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

Our API Deployment defines a HorizontalPodAutoscaler ([`k8s/base/hpa.yaml`](../k8s/base/hpa.yaml#L1-L22): `minReplicas: 3`, `maxReplicas: 8`, target average CPU utilization `70%`).

### What CPU measures on a generation service:
In a RAG service where text generation runs on an external or standalone LLM instance (e.g. `llama3.2:3b` via host Ollama or hosted endpoints), the API Deployment pods perform minimal computational work:
- Validating request schemas (Pydantic)
- Formatting prompt strings
- Dispatching asynchronous HTTP calls (`httpx.Client.post`)
- Parsing returned JSON payloads

During the 15 to 40 seconds required for the LLM to generate an answer, the API pod's CPU sits idle in I/O wait (`epoll_wait`). A sudden influx of 50 concurrent requests causes requests to queue up and latency to spike to minutes, but pod CPU utilization may remain below 15%. Thus, a CPU-based HPA fails to detect saturation and will not trigger scaling.

### What should scale a generation service:
1. **In-flight concurrent requests / queue depth:** A custom Prometheus metric monitoring active HTTP requests currently being processed (`http_requests_in_flight`). When in-flight requests per replica exceed 4, the HPA should scale out.
2. **P95 Request Latency:** Tracking response time via Prometheus (`rahbar_request_seconds` from [`api/app/routes/metrics.py`](../api/app/routes/metrics.py#L15-L20)). When P95 latency exceeds 5 seconds, scale additional replicas to handle concurrent retrieval and formatting.

---

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

During the real indexer Job run on minikube ([`docs/evidence/kubectl-top-indexer.txt`](evidence/kubectl-top-indexer.txt) and [`docs/evidence/real-pipeline-first-run.txt`](evidence/real-pipeline-first-run.txt#L38-L46)):

- **Peak memory observed:** **712 MiB** (sampled via `kubectl -n rahbar top pod -l app.kubernetes.io/name=indexer --containers` at timestamps `16:53:50` through `16:54:21` UTC).
  - *Source of peak:* Rendering 24 scanned PDF pages of `undergraduate-academic-rules-aug-2023.pdf` into 200 DPI greyscale images using `pdf2image` and holding image buffers in memory for `pytesseract` OCR processing.
- **Configured limit:** **2 GiB** (`2048 MiB`) in [`k8s/base/indexer-job.yaml`](../k8s/base/indexer-job.yaml#L115) (`resources.limits.memory: 2Gi`).
- **Safety margin:** $2048\,\text{MiB} - 712\,\text{MiB} = 1336\,\text{MiB}$ of headroom (a $2.87\times$ safety factor / $65\%$ margin).
- **Exit code when wrong:** If memory limits are set too low (e.g. `512Mi`), the container is terminated by the Linux kernel Out-Of-Memory (OOM) killer with **Exit Code 137** ($128 + 9$, indicating `SIGKILL` sent due to cgroup memory exhaustion).

---

## 5. recall@5 by question type: where is it worst, and why?

In our measured live evaluation against the 18-question golden set ([`eval/results.json`](../eval/results.json) and [`docs/PIPELINE.md`](PIPELINE.md#L107-L120)):

- `ordinary (10 questions)`: `recall@5 = 0.70`
- `injection (1 question)`: `recall@5 = 1.00`
- `exact (2 questions)`: `recall@5 = 0.50`
- `version (3 questions)`: `recall@5 = 0.33` **(worst performing category)**

### Why version recall was worst (0.33):
The version category evaluates questions where rules changed between the August 2023 rules and the June 2026 handbook (e.g. course repetition and academic warning thresholds).
1. **Multi-column PDF layout artifact:** In `undergraduate-academic-rules-aug-2023.pdf`, `pdfplumber` extracted text across two columns horizontally. Clause headings (such as `5.8`) were extracted mid-paragraph under earlier clauses (`5.1`), obscuring the clause identifier from section detection.
2. **Missing OCR headings:** Corel export artifacts in the scanned 2023 document resulted in some OCR'd pages having clause numbers separated from body text, leading the chunker to classify them as continuation chunks under previous sections.

### Why exact-term recall was low (0.50):
Question `q12` ("What does clause 1.21 require?") retrieved zero relevant passages (`recall@5 = 0.0`), despite the exact same chunk being retrieved with high similarity rank for `q02` and `q18` when asked about by its conceptual content ("What is the attendance policy?"). Dense embedding models (`nomic-embed-text`) represent semantic meaning, not arbitrary alphanumeric symbols. A clause number like `"1.21"` has no intrinsic semantic embedding proximity to questions about attendance. This highlights the necessity of hybrid search (combining dense vector search with sparse BM25 or keyword filtering on section metadata).

---

## 6. EMBED_MODEL changed on the API only: what does your system do?

If an operator changes `EMBED_MODEL` to `all-minilm` in the API Deployment while Chroma was indexed using `nomic-embed-text`:

1. **Indexer Provenance Recording:** During indexing ([`indexer/pipeline/store.py`](../indexer/pipeline/store.py#L47-L57)), the indexer writes `embed_model: "nomic-embed-text"` and `dimension: 768` directly into the collection metadata.
2. **Startup & Probe Detection:** During API startup ([`api/app/main.py`](../api/app/main.py#L55-L67): `startup_pin_check`) and on every readiness probe ([`api/app/services/readiness.py`](../api/app/services/readiness.py#L51-L66): `pin_conflict()`), the API compares its configured `settings.embed_model` against the collection metadata.
3. **Immediate Probe Failure:** `pin_conflict()` flags the discrepancy. `/readyz` responds with HTTP 503:
   ```json
   {
     "status": "not ready",
     "failed": "embed_model",
     "checks": {
       "chroma": "ok",
       "collection": "ok (710 chunks)",
       "embed_model": "embedding model mismatch: the index was built with 'nomic-embed-text' (dimension 768), but this api has EMBED_MODEL='all-minilm'",
       "redis": "ok"
     }
   }
   ```
4. **Traffic Isolation:** Kubernetes marks the pod unready (`0/1 Running`). The pod is excluded from the Service endpoints, preventing corrupted, meaningless vector dot products from ever reaching users.

See live reproduction logs in [`docs/evidence/embedding-pin-guardrail.txt`](evidence/embedding-pin-guardrail.txt).

---

## 7. The failure that cost us more than an hour

### Symptom and Context:
When deploying the full stack on minikube with `RETRIEVER=chroma`, API pods repeatedly failed startup probes and entered a `CrashLoopBackOff`, while the indexer Job hung intermittently during store operations.

### Investigation & Root Cause:
1. We initially used `chromadb.HttpClient(host=..., port=...)` in [`api/app/retrieval/chroma.py`](../api/app/retrieval/chroma.py). The `chromadb-client` SDK makes a synchronous HTTP heartbeat request inside its `__init__` constructor. Furthermore, the underlying `requests`/`urllib3` session in `chromadb-client` 1.0.12 defaults to having **no HTTP timeout**.
2. On a fresh cluster deployment, ChromaDB takes 30–60 seconds to bind its PVC and initialize its SQLite database. Because `HttpClient` contacted Chroma in constructor time, any API pod initialized before Chroma was ready immediately threw an uncaught connection exception and crashed before reaching `lifespan` startup routines.
3. When the indexer executed large batch vector additions, Chroma's single-threaded SQLite write locks blocked incoming read queries. API requests hung indefinitely inside the untimed `HttpClient` call, starving Uvicorn worker threads.

### Remedy:
We replaced `chromadb.HttpClient` with an asynchronous/timeout-guarded REST client using `httpx.Client(base_url=..., timeout=10.0)` in [`api/app/retrieval/chroma.py`](../api/app/retrieval/chroma.py#L38-L43), identical to the resilient REST pattern in [`api/app/services/readiness.py`](../api/app/services/readiness.py#L45-L48). This eliminated constructor connection crashes, enforced a 10-second request timeout, and allowed the API to gracefully fall back without stalling worker threads.
