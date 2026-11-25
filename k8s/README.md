# k8s/: RAHBAR on Kubernetes (Assignment 03, Part F)

Every object is in this folder. `base/` and `overlays/` are applied together with Kustomize;
the files at the top level and in `candidate/` are Jobs and helper pods that you apply one at
a time, when you need them. Everything runs in the namespace `rahbar`.

## What runs where

| object | file | RAGOps job |
|---|---|---|
| Deployment `api` (3 replicas, HPA 3 to 8, rolling update with `maxUnavailable: 0`) + Service `api` | `base/api.yaml`, `base/services.yaml`, `base/hpa.yaml` | answers questions; only reads the index. Writes one trace per request to stdout and to the `traces` volume |
| Deployment env `RETRIEVAL_COLLECTION` | `base/api-release.yaml` | **the release**: which collection the live api serves. `kubectl rollout undo` reverses a change to it |
| StatefulSet `chroma` + PVC `chroma-data` | `base/chroma-statefulset.yaml` | the vector store |
| Deployment `redis` + PVC `cache-data` | `base/redis.yaml` | the semantic answer cache |
| PVC `corpus-data` | `base/corpus-pvc.yaml` | the data lake: `lake/`, `archive/`, `quarantine/`, `state/` (the ledger), and the reports of each run |
| Job `indexer` + CronJob `reindex` (Sunday 03:00 PKT) | `base/indexer-job.yaml`, `base/reindex-cronjob.yaml` | ingestion with verification (Part A). **The only writer** to Chroma: only pods labelled `indexer`, `api` and `eval` may reach Chroma at all (NetworkPolicies `chroma`, `chroma-from-eval`), and of those only the ingestion code writes. The api and the suite only query. A NetworkPolicy cannot tell a read from a write, so that last part rests on the code (ADR 0001) |
| ConfigMap `rahbar-config` | `base/configmap.yaml` | settings, including the chunk size that names the collection the indexer writes |
| ConfigMap `rahbar-gate` | `base/gate-configmap.yaml` | release-gate thresholds (C2) and coverage thresholds (E2). Edit and re-apply; no new image needed |
| PVC `eval-results`, ServiceAccount and NetworkPolicies `eval` | `base/eval.yaml` | where suite, gate and coverage results go |
| PVC `traces` | `base/traces.yaml`, `base/api-traces-patch.yaml` | trace records for the report scripts (Part D) |
| Job `eval-suite` | `eval-job.yaml` | the Part B suite and the gate. Fails when the gate fails; a passing run becomes `/results/baseline.json` |
| Job `gate-check` | `gate-job.yaml` | re-applies the gate to a saved result in seconds (F2 demo) |
| Job `ingest-batch-b` | `ingest-batch-b.yaml` | Part A: builds Batch B on the corpus volume, then ingests it |
| Deployment + Service `api-candidate`, Jobs `ingest-candidate`, `eval-candidate` | `candidate/` | Part C, F3: a candidate with its own collection. Runbook: [candidate/README.md](candidate/README.md) |
| Job `live-traffic`, Job `coverage-check` | `live-traffic-job.yaml`, `coverage-job.yaml` | Part E: play the live queries; check that the golden set covers them. Fails on a coverage gap |
| Pods `lake-shell`, `results-shell` | `lake-shell.yaml`, `results-shell.yaml` | helpers: work on the data lake by hand; copy result files out |
| NetworkPolicies, ServiceAccounts, Ingress, `ui` | `base/networkpolicy.yaml`, `base/serviceaccounts.yaml`, `base/ingress.yaml`, `base/ui.yaml` | default deny, then one rule per path; the chat page |

Overlays: `dev` (Ollama on the host), `dev-wsl` (the same, for minikube inside WSL 2 with
Docker Desktop: only `OLLAMA_URL` changes), `ci`, `smoke`, `prod`, `hosted`.

## A fresh cluster, from nothing to a working pipeline

Applying the manifests is enough. The extra commands are listed below; run them from the
repository root.

```bash
# 1. The cluster: minikube with Calico (so NetworkPolicies are enforced), metrics-server, ingress
make cluster

# 2. Ollama on the host, reachable from the cluster, with the two models
OLLAMA_HOST=0.0.0.0 ollama serve          # in another terminal
ollama pull llama3.2:3b && ollama pull nomic-embed-text

# 3. The images, built and loaded into minikube (api, indexer, ui, and the eval image)
make images && make eval-image

# 4. Everything in base/ (use k8s/overlays/dev on a minikube installed on Windows or Linux)
kubectl apply -k k8s/overlays/dev-wsl

# 5. Wait for the first ingestion, then the api. The api is pinned to the collection named in
#    base/api-release.yaml and reports ready only when that collection exists and is not empty
#    (about 10 minutes on a laptop CPU: OCR and embedding)
kubectl -n rahbar wait --for=condition=complete job/indexer --timeout=30m
kubectl -n rahbar rollout status deploy/api --timeout=10m
```

That is a working pipeline: ingestion has run with verification, the api answers, and the
CronJob re-runs ingestion every week. The assignment's steps on top of it:

```bash
# Part A: ingest Batch B (verification report in the log), then an unchanged re-run (0 embedded)
kubectl apply -f k8s/ingest-batch-b.yaml && kubectl -n rahbar logs -f job/ingest-batch-b -c indexer
kubectl -n rahbar create job --from=cronjob/reindex rerun-unchanged && kubectl -n rahbar logs -f job/rerun-unchanged -c indexer

# Part B: the suite and the gate (writes /results/baseline.json when it passes)
make eval-suite

# Part C and F3: candidate, gate, release, rollback
#   see k8s/candidate/README.md

# Part E: live queries, then coverage (the Job fails on a coverage gap)
kubectl apply -f k8s/live-traffic-job.yaml && kubectl -n rahbar logs -f job/live-traffic
kubectl apply -f k8s/coverage-job.yaml && kubectl -n rahbar logs -f job/coverage-check
```

A Job's name can be used only once: before re-running one, use
`kubectl -n rahbar delete job <name> --ignore-not-found`.

On a fresh cluster there is no `/results/baseline.json` until the first `eval-suite` run
passes. Until then the gate applies only its absolute rules (`min`, `max`); the rules relative
to the baseline are skipped and say so.

## Day-to-day commands

```bash
kubectl -n rahbar get all,pvc,configmap,cronjob,job

# Traces (F4): JSON lines on stdout, and on the traces volume
kubectl -n rahbar logs deploy/api | grep '"type": "trace"' | tail -1
kubectl -n rahbar exec deploy/api -- python -m app.trace_tool <trace-id>          # rebuilds the prompt
kubectl -n rahbar exec deploy/api -- python -m app.trace_summary --last 1h

# Metrics (F4). Each pod keeps its own counters; the Service forwards to one pod
kubectl -n rahbar port-forward svc/api 8000:8000 &
curl -s localhost:8000/metrics | grep ^rahbar_

# Result files: copy them out of the eval-results volume
kubectl apply -f k8s/results-shell.yaml && kubectl -n rahbar wait --for=condition=ready pod/results-shell
kubectl -n rahbar exec results-shell -- ls -l /results
kubectl -n rahbar cp results-shell:/results/<file> eval/results/<file>

# Resources (F5): sample kubectl top while something runs, then summarise
scripts/sample-resources.sh <label> [job-name]
python scripts/summarize_resources.py eval/results/part-f/top-<label>.tsv
```

## A release changes two settings

The collection name comes from the embedding model and the chunk settings
(`rahbar-nomic-embed-text-c250o50`). A release of a chunking change therefore changes two
things, and both are committed:

1. `RETRIEVAL_COLLECTION` in `base/api-release.yaml`: what the api reads.
2. `CHUNK_SIZE` / `CHUNK_OVERLAP` in `base/configmap.yaml`: what the ingestion Job writes.

If only the first changes, the weekly CronJob keeps updating a collection that nobody reads,
and new documents never reach the live one. `eval/tests/test_manifests.py` fails when the two
disagree. The first ingestion after the change re-embeds every document into the new
collection under the main ledger (the chunk counts no longer match), and from then on it is
incremental again.

## Resources (F5)

Set from measurements taken on 24 Nov 2026: a full suite run, a full re-embed, and a
12-client burst. They were sampled with `scripts/sample-resources.sh`, which records `kubectl
top` and cgroup `memory.peak`. Method, samples and reasons:
[eval/results/part-f](../eval/results/part-f/README.md).

| workload | measured CPU / memory (working set, peak) | requests | limits |
|---|---|---|---|
| api, per pod | 6-9m, 64m in the burst / 47-54, 76 MiB | 100m (the HPA's denominator), 96Mi | 500m, 256Mi |
| chroma | 5-41m / 117-159, 307 MiB | 50m, 320Mi | 1, 1Gi |
| redis | 15-43m / 9-14, 43 MiB | 25m, 64Mi | 250m, 256Mi |
| ui, per pod | 1-3m / 3-8, 17 MiB | 10m, 24Mi | 100m, 64Mi |
| indexer Job and CronJob | up to 1403m / 523, 1341 MiB | 500m, 768Mi | 2, 2Gi |
| eval-suite and candidate eval Jobs | 2-13m / 31, 36 MiB | 20m, 64Mi | 500m, 256Mi |
| gate-check, coverage-check, live-traffic | seconds; 18 to 36 MiB | 10m, 64Mi | 200m to 500m, 256Mi |

## Limits of this setup

- One node. The PVCs are ReadWriteOnce, which on a single node lets the api replicas share the
  `traces` volume, and the coverage Job read it. On several nodes, traces would go to a log
  collector instead.
- Ollama runs on the host, outside the cluster, so its CPU and memory are not in any
  request or limit here. On a laptop the model call is most of an answer's time.
- The images are tagged `:dev` and loaded with `minikube image load`. CD (A02) pushes
  SHA-tagged images instead.
