# Part C on Kubernetes: candidate, gate, release, rollback

The change under test (C1): **chunk size 400 → 250 words** (overlap 50 in both). The baseline
is the live api on `rahbar-nomic-embed-text-c400o50`; the candidate is a second Deployment on
`rahbar-nomic-embed-text-c250o50`. Every object involved:

| object | file | role |
|---|---|---|
| ConfigMap `rahbar-gate` | `k8s/base/gate-configmap.yaml` | the gate's thresholds (C2, F2) |
| Job `eval-suite` | `k8s/eval-job.yaml` | the suite on the live api; gate; a pass becomes `/results/baseline.json` |
| Job `ingest-candidate` | `k8s/candidate/ingest-candidate.yaml` | builds the candidate collection, the pointer untouched |
| Deployment + Service `api-candidate` | `k8s/candidate/api-candidate.yaml` | serves the candidate collection |
| Job `eval-candidate` | `k8s/candidate/eval-candidate-job.yaml` | the suite on the candidate; gate against the baseline; fails if the gate fails |
| Deployment `api`, env `RETRIEVAL_COLLECTION` | `k8s/base/api-release.yaml` | what the live api serves: a release changes it, `rollout undo` reverses it |

Images: `make images` (api, indexer, ui) and `make eval-image`. Apply the base first:
`kubectl apply -k k8s/overlays/dev-wsl` (creates the gate ConfigMap and pins the live api).

## 1. Baseline

```bash
make eval-suite        # Job eval-suite: suite on the live api, gate, saves /results/baseline.json
```

## 2. Candidate: build, serve, evaluate, gate

```bash
kubectl -n rahbar delete job ingest-candidate --ignore-not-found
kubectl apply -f k8s/candidate/ingest-candidate.yaml
kubectl -n rahbar logs -f job/ingest-candidate

kubectl apply -f k8s/candidate/api-candidate.yaml
kubectl -n rahbar rollout status deployment/api-candidate

kubectl -n rahbar delete job eval-candidate --ignore-not-found
kubectl apply -f k8s/candidate/eval-candidate-job.yaml
kubectl -n rahbar logs -f job/eval-candidate          # ends with GATE PASSED or GATE FAILED
kubectl -n rahbar get job eval-candidate              # Complete, or Failed when the gate fails
```

A candidate whose Job fails is **stopped by the gate** and never rolled out.

The gate can be re-applied to saved results without re-running anything:
`python -m eval.gate --candidate <candidate.json> --baseline <baseline.json>`.

## 3. A threshold edit changes the outcome (F2)

`k8s/gate-job.yaml` re-applies the mounted thresholds to a saved result in seconds:

```bash
kubectl -n rahbar delete job gate-check --ignore-not-found
kubectl apply -f k8s/gate-job.yaml && kubectl -n rahbar logs -f job/gate-check     # PASSED
# raise e2e.faithfulness min from 0.75 to 0.85 in k8s/base/gate-configmap.yaml, then
kubectl apply -k k8s/overlays/dev-wsl
kubectl -n rahbar delete job gate-check
kubectl apply -f k8s/gate-job.yaml && kubectl -n rahbar logs -f job/gate-check     # FAILED
git checkout k8s/base/gate-configmap.yaml && kubectl apply -k k8s/overlays/dev-wsl  # restore
```

Same image, same results, a different decision.

The candidate stopped by the gate is `k8s/candidate/eval-candidate-stale-job.yaml`, the
stale A02 index. Its Job fails, so it is never rolled out.

## 4. Release (only after GATE PASSED)

```bash
kubectl -n rahbar set env deployment/api RETRIEVAL_COLLECTION=rahbar-nomic-embed-text-c250o50
kubectl -n rahbar rollout status deployment/api       # rolling update, maxUnavailable: 0
```

Then record the release in `k8s/base/api-release.yaml`, so the next `kubectl apply -k` keeps it.

## 5. Roll back

The demo: a bad release that skips the gate (the stale A02 index), then the undo.

```bash
kubectl -n rahbar set env deployment/api RETRIEVAL_COLLECTION=rahbar-g1795268618
kubectl -n rahbar rollout status deployment/api
kubectl -n rahbar rollout undo deployment/api
kubectl -n rahbar rollout status deployment/api
kubectl -n rahbar exec deploy/api -- python -c "import httpx; print(httpx.post('http://localhost:8000/api/ask', json={'question': 'How early must students reach the examination hall?'}, headers={'X-Request-ID': 'after-undo-1', 'Cache-Control': 'no-cache'}, timeout=300).json()['answer'])"
kubectl -n rahbar exec deploy/api -- python -m app.trace_tool after-undo-1 | head -8
```

`rollout undo` returns to the revision before the bad one, so the new trace names the
collection that was live before it (`rahbar-nomic-embed-text-c250o50` after the release in
step 4) and the embedding model's version (its Ollama digest). `Cache-Control: no-cache`
makes the request go through retrieval instead of the semantic cache.

`rollout undo` does not update the `last-applied-configuration` annotation (kubectl warns
about this), so `k8s/base/api-release.yaml` must name the collection that should be live:
the next `kubectl apply -k` sets it from that file.
