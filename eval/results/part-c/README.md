# Part C results: chunk size 400 → 250, the gate, and the release decision

Run on 24 Nov 2026 on the WSL minikube, images at git `4e267ed`, everything as Kubernetes
objects (runbook: [k8s/candidate/README.md](../../../k8s/candidate/README.md)).

## C1. The change

**Chunk size 400 → 250 words** (overlap 50 in both). The reason, from Part B: on a laptop
CPU most of an answer's time is the model reading its prompt (three passages), and long
chunks mix clauses, which blurs their embeddings (q12's "clause 1.21" lost to a heading).
Smaller chunks should give shorter prompts and sharper matches.

| | baseline | candidate |
|---|---|---|
| collection | `rahbar-nomic-embed-text-c400o50`, 703 chunks | `rahbar-nomic-embed-text-c250o50`, 885 chunks |
| built by | the indexer Job (Part A) | Job `ingest-candidate`: same 13 documents, the same 3 archived, pointer untouched ([log](ingest-candidate.log)) |
| served by | Deployment `api` (pinned with `RETRIEVAL_COLLECTION`) | Deployment `api-candidate`, Service `api-candidate` |
| evaluated by | Job `eval-suite`, 2 seeded runs ([log](eval-baseline-job.log)) | Job `eval-candidate`, 2 seeded runs ([log](eval-candidate-job.log)) |

The baseline run reproduced Part B (21 Nov) exactly: every module, component and quality
metric is identical, and the same single answer (q09) changed between the seeded runs.

## All three levels ([comparison](comparison-c400o50-vs-c250o50.md))

Means over the two runs; latency p95 is the worse run, as the gate counts it.

| level | metric | baseline c400o50 | candidate c250o50 | change |
|---|---|---|---|---|
| module | paraphrase / unrelated mean cosine, gap | 0.801 / 0.406, 0.395 | the same | 0 (same model) |
| module | embedding ms per chunk | 234 | 215 | host noise |
| component | recall@3 | 0.583 | 0.625 | +0.042 |
| component | recall@5 | 0.667 | 0.708 | +0.042 |
| component | MRR | 0.540 | 0.558 | +0.019 |
| component | nDCG@5 | 0.550 | 0.574 | +0.025 |
| component | recall@5 with typos | 0.500 | 0.500 | 0 |
| component | MRR / nDCG@5 with typos | 0.448 / 0.440 | 0.417 / 0.416 | -0.031 / -0.024 |
| component | recall@5 with 3 distractor documents | 0.667 | 0.667 | 0 |
| e2e | faithfulness (judge) | 0.801 | 0.846 | +0.045 |
| e2e | abstention correctness | 1 | 1 | 0 |
| e2e | false abstention rate | 0.479 | 0.458 | -0.021 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy | 0.45 | 0.50 | +0.05 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 / p95 (s) | 17.5 / 55.8 | 16.0 / 41.4 | -1.5 / -14.4 |

From the traces of the same requests (they name the collection each one used): mean prompt
**835 → 755 tokens** (-10%), median model call **17.3 → 15.8 s**.

What moved: q09 is now retrieved (recall@5 0 → 1: the "5.1" clause is no longer buried in a
chunk led by another clause), and q04 (in-campus hostels) is now answered. The module level
cannot move: the change is after embedding. Two things got slightly worse: with typos the
right chunk ranks a little lower (MRR -0.031), and the distractor documents cost the
candidate the one label it gained (with distractors both score 0.667). More, smaller chunks
mean more chances for a near-miss to rank above the answer.

## C2. The gate ([k8s/base/gate-configmap.yaml](../../../k8s/base/gate-configmap.yaml))

| metric | baseline | candidate c250o50 | rule | outcome |
|---|---|---|---|---|
| recall@5 | 0.6667 | 0.7083 | >= baseline - 0.02 | PASS |
| nDCG@5 | 0.5499 | 0.5744 | >= baseline - 0.05 | PASS |
| faithfulness | 0.8013 | 0.8462 | >= 0.75 | PASS |
| abstention correctness | 1 | 1 | >= baseline | PASS |
| answer accuracy | 0.45 | 0.5 | >= baseline - 0.05 | PASS |
| citation validity | 1 | 1 | >= 0.95 | PASS |
| injection resistance | 1 | 1 | >= 1.0 | PASS |
| latency p95 (worst run) | 55.8 | 41.37 | <= 90 | PASS |

**GATE PASSED** (Job `eval-candidate` Complete).

### A candidate the gate stops

A deliberately bad candidate: `rahbar-g1795268618`, the Assignment 02 generation that the
blue/green indexer kept for rollback. It was built before Batch B, so it has no exam-hall
rules, no withdrawal FAQ and the old fee page. It is a realistic mistake: the api accepts it
(same embedding model, the readiness probe passes), and it answers, but from stale data.
Served by `api-candidate` and evaluated by Job `eval-candidate-stale` (one run: its recall is
deterministic) ([log](eval-candidate-stale-job.log)):

| metric | baseline | stale candidate | rule | outcome |
|---|---|---|---|---|
| recall@5 | 0.6667 | 0.5 | >= 0.6467 | **FAIL** |
| nDCG@5 | 0.5499 | 0.414 | >= 0.4999 | **FAIL** |
| faithfulness | 0.8013 | 0.8 | >= 0.75 | PASS |
| abstention correctness | 1 | 1 | >= 1 | PASS |
| answer accuracy | 0.45 | 0.3 | >= 0.40 | **FAIL** |
| citation validity, injection | 1, 1 | 1, 1 | | PASS |
| latency p95 | 55.8 | 31.59 | <= 90 | PASS |

**GATE FAILED**, and the Job's status is Failed ([jobs-status.txt](jobs-status.txt)); the
candidate is never rolled out. Note that faithfulness passes (0.80): the stale answers are
faithful to the stale passages they were given. Only the component level (retrieval against
labels of the current documents) and answer accuracy see that the index is out of date.

### A threshold edit changes the outcome (F2)

Job `gate-check` ([k8s/gate-job.yaml](../../../k8s/gate-job.yaml)) re-applies the mounted
ConfigMap to the saved results in seconds. With the committed thresholds the c250o50
candidate passes. Raise faithfulness `min` from 0.75 to 0.85 in the ConfigMap and re-run it:
the same image and the same result file (0.846) now fail.

## Release decision: release c250o50

It passes every rule and is no worse than the baseline anywhere the gate looks. It is
slightly better: one more question retrieved, one more answered, prompts 10% shorter. The
gains are small (each is one question of 24, and faithfulness +0.045 is inside the 0.064 the
baseline moves between runs), so the case for releasing is "as good, and cheaper per
answer", not a large quality jump. The typo and distractor results are the risk to watch
after release, in the trace summary's top-1 similarity and abstention rate.
