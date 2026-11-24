# Part F results: RAHBAR on Kubernetes

WSL minikube (one node, Calico, metrics-server), 24 Nov 2026. Every object is under
[k8s/](../../../k8s/README.md), whose README lists the steps from a fresh cluster.

| item | how | evidence |
|---|---|---|
| F1 Deploy | Deployment `api` (3 replicas, HPA 3 to 8) + Service `api`; StatefulSet `chroma` with PVC `chroma-data`; data lake, quarantine and archive on PVC `corpus-data` | `kubectl get all,pvc,configmap,cronjob,job` (screenshot F) |
| F2 Jobs | Job `indexer` and CronJob `reindex` (ingestion with verification, the only writer to Chroma); Job `eval-suite` writes to PVC `eval-results` and fails when the gate fails; thresholds in ConfigMap `rahbar-gate` | [reindex-c250-job.log](reindex-c250-job.log), [cronjob-reindex-29921640.log](cronjob-reindex-29921640.log), [part-c](../part-c/README.md) (gate fail and pass), screenshots `F2-*` (a threshold edit changes the outcome) |
| F3 Release | candidate Deployment `api-candidate` + Service on its own collection; gate; release with a rolling update; a bad release reversed with `kubectl rollout undo`; a trace afterwards names the collection and the embedding version | [part-c](../part-c/README.md) "The release and a rollback", screenshots `F3-*` |
| F4 Observability | traces as JSON lines on stdout (`kubectl logs`) and on PVC `traces`; `/metrics` through `kubectl port-forward` | [part-d](../part-d/README.md), [metrics-port-forward.txt](metrics-port-forward.txt) |
| F5 Resources | measured with `kubectl top` and cgroup `memory.peak`, then set | below |

## F5. Resources, from measurements

`scripts/sample-resources.sh` records `kubectl top pod --containers` every 10 to 15 s while
something runs. That is metrics-server's working set, a rolling average of about a minute.
Every minute it also reads each container's cgroup `memory.peak`: the true high-water mark
since the container started, which includes page cache (for Chroma, its index files).
`scripts/summarize_resources.py` reduces the samples per workload. Four measured situations:

| run | what loads what | samples |
|---|---|---|
| `suite` | Job `eval-suite`, 43 questions twice (37 min): the api, Chroma, the judge | [top-suite.tsv](top-suite.tsv), [peak-suite.tsv](peak-suite.tsv) |
| `ingest` | Job `reindex-c250`, a full re-embed: 885 chunks, 851 embedded, 493 s | [top-ingest.tsv](top-ingest.tsv), [peak-ingest.tsv](peak-ingest.tsv) |
| `burst` | `scripts/load_burst.py`: 12 concurrent clients for 3 min, 535 answers (2.5/s), 0 errors, p50 0.78 s, p95 1.16 s | [top-burst.tsv](top-burst.tsv), [load-burst.txt](load-burst.txt) |
| short Jobs | `gate-check` and `coverage-check` last 1 to 8 s, below `kubectl top`'s resolution: their peak memory from `/usr/bin/time -v` on the same code | [short-jobs-memory.txt](short-jobs-memory.txt) |

| workload | CPU measured (m) | memory measured (MiB): working set / memory.peak | requests (was) | limits (was) |
|---|---|---|---|---|
| api (per pod) | 6-9; burst 64 | 47-54 / 76 | 100m, 96Mi (192Mi) | 500m, 256Mi (512Mi) |
| chroma | 5-14; 41 at most | 117-159 / 307 | 50m (100m), 320Mi (256Mi) | 1, 1Gi |
| redis | 15-20; 43 in the burst | 9-14 / 43 | 25m (50m), 64Mi | 250m, 256Mi |
| ui (per pod) | 1-3 | 3-8 / 17 | 10m, 24Mi (16Mi) | 100m, 64Mi |
| indexer (ingestion Job, CronJob) | 7 typical; 1403 at most (OCR, embedding) | 501-523 / 1341 | 500m, 768Mi | 2, 2Gi |
| eval-suite (and the candidate eval Jobs) | 2-13 | 30-31 / 36 | 20m (50m), 64Mi (128Mi) | 500m, 256Mi (512Mi) |
| gate-check | - (1 s) | 18 (RSS) | 10m, 64Mi | 200m, 256Mi |
| coverage-check | - (8 s) | 34 (RSS) | 10m, 64Mi | 500m, 256Mi |
| live-traffic | the burst client: 6-20 | 30-32 / 36 | 10m, 64Mi | 200m, 256Mi |

How the numbers were chosen:

- **Memory requests** cover the measured working set, and the measured `memory.peak` where
  page cache matters. For Chroma that is the index in memory, so its request rose to 320 Mi.
- **Memory limits** leave room for what was not measured. The api's limit allows longer
  prompts and answers. Chroma's allows more collections: each candidate adds one, and four
  are stored now. Redis's allows a snapshot fork. The indexer's 2 Gi is 1.5 times its
  1341 MiB peak.
- **CPU requests** follow typical use, except the api's, which stays at 100m: it is the
  HPA's denominator. In the burst the api reached 64m per pod, and the HPA showed 66% of its
  70% target, so it scales out at about the load a laptop's Ollama can serve.
- **CPU limits** stay generous. Throttling a pod that mostly waits on the model would only
  add latency.
- **What is not in any limit:** Ollama runs on the host. Generation (about 15 to 20 s per
  answer on this CPU) and embedding are most of the work, and they sit outside the cluster's
  accounting.

The suite was re-run after these values were applied (`suite-rebase`, below in Part E), with
no restarts and no OOM kills.

## Two findings while releasing on Kubernetes

1. **A release of a chunking change moves two settings.** The api was pinned to the new
   collection (`base/api-release.yaml`), but `CHUNK_SIZE` in `rahbar-config` was still 400.
   That setting names the collection the ingestion Job writes, so the weekly CronJob would
   have kept updating `c400o50`, which nobody reads. New documents would never have reached
   the live collection, and a fresh cluster would have built only `c400o50`, so the pinned
   api would never have become ready. `CHUNK_SIZE` is now 250. `eval/tests/test_manifests.py`
   fails when the two disagree, and also when any container lacks requests or limits. The
   first ingestion after the change ([reindex-c250-job.log](reindex-c250-job.log))
   re-embedded 851 chunks under the main ledger and moved the `rahbar-active` pointer to
   `c250o50`.
2. **The suite followed the pointer, not the api.** Before that ingestion, the suite's own
   retriever still read `c400o50`. It looked up the api's chunk ids there to give the judge
   its evidence. Chunk ids are `hash(document, sha, index)`, so the same id names a 400-word
   chunk in one collection and a 250-word chunk in the other. The judge read the wrong text,
   and faithfulness fell from 0.80 to 0.45 with no change in the answers
   ([part-e](../part-e/README.md)). `/readyz` now names the collection it checked
   (`ok (rahbar-nomic-embed-text-c250o50, 885 chunks)`). The suite measures that collection
   and stops if told to measure another.
