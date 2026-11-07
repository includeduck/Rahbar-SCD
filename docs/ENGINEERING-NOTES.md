# Engineering Notes

This document provides technical answers to the seven engineering questions posed in the RAHBAR case study, referencing the exact file paths, line numbers, and empirical measurements from our cluster.

---

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

Our API Deployment runs 3 replicas ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L12): `replicas: 3`), while ChromaDB is deployed as a single StatefulSet backed by a single ReadWriteOnce persistent volume ([`k8s/base/chroma-statefulset.yaml`](../k8s/base/chroma-statefulset.yaml#L76-L83): `volumeClaimTemplates`, `ReadWriteOnce`).

If document indexing were executed inside API startup (e.g. during `app.main:lifespan`), four major failure modes occur:

1. **Concurrent SQLite/HNSW Corruption:** Three API pods starting simultaneously would each run `extract -> clean -> chunk -> embed -> store`, writing to the shared Chroma collection at the same time. ChromaDB's underlying SQLite metadata database and HNSW segment files are not designed for concurrent multi-process writes, resulting in deadlocks, partial batch commits, and index corruption.
2. **Cascading Re-index Storms:** Every pod lifecycle event—a rolling update, node drain, liveness probe failure, or an OOMKill—would trigger a complete re-index of the 9-document corpus, incurring 500 s of OCR and embedding computation (the indexer's own log; the whole Job, with the corpus fetch, took 8m59s: [`docs/evidence/real-pipeline-first-run.txt`](evidence/real-pipeline-first-run.txt#L32-L36)).
3. **Readiness Probe Deadlock or False Readiness:** The API startup probe allows at most 60 seconds ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L57-L60): `failureThreshold: 30` × `periodSeconds: 2`). Because full corpus indexing takes 500 seconds, the startup probe would fail, causing kubelet to terminate the pod before it ever finished. Conversely, if `/readyz` did not block on indexing, the pod would serve user queries against an empty or partially indexed vector database, returning empty or hallucinated answers.
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
1. The liveness probe would fail three consecutive times ([`k8s/base/api.yaml`](../k8s/base/api.yaml#L61-L64): `periodSeconds: 20`, `failureThreshold: 3`, so about 60 seconds, well inside the 90-second slowdown).
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

- **Peak memory observed:** **712 MiB** (sampled via `kubectl top`: `kubectl top pod -l app.kubernetes.io/name=indexer -n rahbar --containers` at timestamps `16:53:50` through `16:54:21` UTC).
  - *Source of peak:* the peak falls in the window when the indexer was OCR'ing `undergraduate-academic-rules-aug-2023.pdf` (it logged that document's 106 chunks at 16:53:59). 23 of that document's pages have no text layer, and each one is rendered to a 200 DPI greyscale image (`pdf2image`) and held in memory for `pytesseract`. Memory then settled at 513–558Mi for the embedding phase. In the whole corpus, 35 pages go through OCR (23 there, all 8 of the start-ups policy, and 2 cover pages in each June 2026 handbook).
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
The version category asks about rules that changed between the August 2023 rules and the June 2026 handbook, or asks for the older rule explicitly (q09 retaking a failed course, q10 the course load, q11 the 2023 course load). q10 is found; q09 and q11 miss in every chunking configuration we measured ([`docs/evidence/chunk-sweep.txt`](evidence/chunk-sweep.txt)), so chunk size is not the cause:
1. **Two-column layout (q09):** in `academic-rules-handbook-jun-2026.pdf`, `pdfplumber` reads two-column pages straight across, so the heading of clause 5.8 lands mid-line inside the chunk labelled "5.1 The performance of students ...". The chunker never sees 5.8 as a heading, so the retake rule sits inside a chunk that is mostly about clause 5.1 and does not reach the top 5 (the grading page and two handbook pages outrank it).
2. **The version regex misses the question that asks for the old rule (q11):** "Under the 2023 rules, could a student take extra courses...?" does not match `_VERSION_RE` in [`api/app/retrieval/chroma.py`](../api/app/retrieval/chroma.py#L32-L35), which requires phrases like "aug 2023 rules". So the retriever applies `superseded == False` and the 2023 document, the only one holding the answer, is filtered out. Even with the filter removed, the 2026 chunks on course load outrank the OCR'd 2023 chunk (labelled "3.22 Transfer shall not be considered ..." because OCR puts the rule under the neighbouring heading). See FAILURE-ANALYSIS Stage E.

### Why exact-term recall was low (0.50):
Question `q12` ("What does clause 1.21 require?") retrieved zero relevant passages (`recall@5 = 0.0`), despite the exact same chunk being retrieved with high similarity rank for `q02` and `q18` when asked about by its content (q02: "How much absence can the Head of Department condone?"). Dense embedding models (`nomic-embed-text`) represent semantic meaning, not arbitrary alphanumeric symbols. A clause number like `"1.21"` has no intrinsic semantic embedding proximity to questions about attendance. This highlights the necessity of hybrid search (combining dense vector search with sparse BM25 or keyword filtering on section metadata).

---

## 6. EMBED_MODEL changed on the API only: what does your system do?

If an operator changes `EMBED_MODEL` to `all-minilm` in the API Deployment while Chroma was indexed using `nomic-embed-text`:

1. **Indexer Provenance Recording:** During indexing ([`indexer/pipeline/store.py`](../indexer/pipeline/store.py#L47-L57)), the indexer writes `embed_model: "nomic-embed-text"` and `dimension: 768` directly into the collection metadata.
2. **Startup & Probe Detection:** During API startup ([`api/app/main.py`](../api/app/main.py#L55-L67): `startup_pin_check`) and on every readiness probe ([`api/app/services/readiness.py`](../api/app/services/readiness.py#L51-L66): `pin_conflict()`), the API compares its configured `settings.embed_model` against the collection metadata.
3. **Immediate Probe Failure:** `pin_conflict()` flags the discrepancy and `/readyz` responds with HTTP 503. Against the live index it would return the following (the message format is the one `readiness.py` builds; the run we recorded used the dummy index, see below):
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
4. **Traffic Isolation:** Kubernetes marks the new pod unready (`0/1 Running`) and its EndpointSlice entry `ready=false`, so it gets no traffic. Because the rollout uses `maxUnavailable: 0`, the three old, correctly pinned pods keep serving and `kubectl rollout status` times out: the bad change stalls instead of taking the service down.

We reproduced this on 3 November ([`docs/evidence/embedding-pin-guardrail.txt`](evidence/embedding-pin-guardrail.txt)) with the index built by the dummy indexer (`embed_model=dummy-768`) and the api switched to `nomic-embed-text`. The api logged `embedding model mismatch: the index was built with 'dummy-768' (dimension 768), but this api has EMBED_MODEL='nomic-embed-text'` and refused to become ready.

---

## 7. The failure that cost us more than an hour

### Symptom
On 7 November we switched the base configuration from the stubs to the real pipeline (`RETRIEVER=chroma`, `LLM_PROVIDER=ollama-host`, llama3.2:3b on the host). The index built, `/readyz` was green and retrieval returned the right chunks, yet two of our first test questions through the Ingress came back as the fallback "unable to answer". The fee question, which had a shorter prompt, answered correctly in 30 s. Nothing crashed and no pod restarted, so the cluster looked perfectly healthy. It took from 16:18 UTC, when we switched over, to about 17:50 UTC before the first full evaluation ran, with real answers and valid citations.

### Investigation
1. Both failed questions had run into the provider's 60-second timeout on the Ollama call, after which the answer service falls back by design.
2. We timed `llama3.2:3b` directly on the host CPU (Intel i7-1265U, a 15 W laptop chip; Ollama found no usable GPU). A 31-token prompt took 0.5 s. A 2,050-token prompt, about RAHBAR's size, took 64 s: the model reads the prompt at about 33 tokens/s and writes the answer at 7.7 tokens/s. **Reading the prompt was the bottleneck**, not writing the answer. Our real prompt was even longer (~2,500 tokens), because it also pasted the full Answer JSON schema and five passages.
3. With the timeout fixed, a second cause showed up: correct answers were still being thrown away. The model cites a clause as `"5.4"`, while the chunker labels the chunk `"5.4 Minimum CGPA required ..."`. The citation filter required an exact match, dropped the citation, and an answer left with no valid citation falls back.

### Remedy (commit 7839868, PR #9)
- The prompt no longer repeats the JSON schema (Ollama's `format=` already enforces it), and only the best `MAX_CONTEXT_PASSAGES=3` passages go into it. The response still lists all `TOP_K=5` retrieved passages, so recall@5 stays measurable.
- The generation timeout became a setting, `LLM_TIMEOUT_SECONDS=120`, and the Ingress read timeout went to 300 s, because a question can take two attempts.
- Citations match by clause number the same way `eval/run_eval.py` does ("5.4" matches "5.4 ..." but not "5.41") and are rewritten to the retrieved label, so every returned citation still exactly matches a retrieved chunk. Six new tests cover it.

Afterwards "What CGPA puts a student on academic warning?" was answered "2.00", citing handbook clause 5.4, in 15 s ([`docs/evidence/real-pipeline-first-run.txt`](evidence/real-pipeline-first-run.txt)).

### What we learned
A healthy cluster can still serve a useless product. Every probe was green because none of them measures whether answers are grounded. That is what the evaluation and its CI gate are for. We now also check the cost of a prompt in tokens before blaming the model.
