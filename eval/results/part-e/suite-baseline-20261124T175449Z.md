# suite-baseline-20261124T175449Z

| level | metric | run 1 | run 2 | difference |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 201 | 207.3 | 6.3 |
| module | embedding ms per chunk (batched) | 182.2 | 198.7 | 16.5 |
| module | determinism: largest difference | 0 | 0 | 0 |
| component | recall@3 | 0.6364 | 0.6364 | 0 |
| component | recall@5 | 0.7121 | 0.7121 | 0 |
| component | MRR | 0.5828 | 0.5828 | 0 |
| component | nDCG@5 | 0.5931 | 0.5931 | 0 |
| component | recall@5 with typos | 0.5455 | 0.5455 | 0 |
| component | MRR with typos | 0.4848 | 0.4848 | 0 |
| component | nDCG@5 with typos | 0.4805 | 0.4805 | 0 |
| component | recall@5 with 3 distractor documents | 0.6818 | 0.6818 | 0 |
| component | MRR with distractors | 0.5616 | 0.5616 | 0 |
| component | nDCG@5 with distractors | 0.5702 | 0.5702 | 0 |
| e2e | faithfulness (judge) | 0.7143 | 0.7619 | 0.0476 |
| e2e | abstention correctness (unanswerable) | 0.9 | 0.9 | 0 |
| e2e | false abstention rate (answerable) | 0.3939 | 0.3939 | 0 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy (expected phrase) | 0.4815 | 0.4815 | 0 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 (s) | 22.72 | 18.28 | -4.44 |
| e2e | latency p95 (s) | 43.08 | 30.69 | -12.39 |

- Suite `baseline` at 20261124T175449Z (git 2fd9461), collection `rahbar-nomic-embed-text-c250o50`, embedding `nomic-embed-text`, generation `llama3.2:3b` (temperature 0.1, seed 42), judge `llama3.2:3b`, suite seed 42.
- Golden set: 43 questions, 22 answerable, 10 unanswerable, 11 hard; 18 human, 25 synthetic (22 reviewed by hand).
- Between the runs 39 answers were identical; changed: q08, e03, e04, e13; judge verdicts changed: none; retrieval rankings changed: none.
- Judge vs hand labels: 16 of 20 agree (0.8).

```
release gate: baseline (rahbar-nomic-embed-text-c250o50) against baseline (rahbar-nomic-embed-text-c400o50)
| metric | baseline (rahbar-nomic-embed-text-c400o50) | baseline (rahbar-nomic-embed-text-c250o50) | rule | outcome |
|---|---|---|---|---|
| golden set | 6bc3d49c5de9 | 51038c2438cc | same questions | REBASE |
| component.base.recall@5 | - | 0.7121 | >= baseline - 0.02 | SKIP |
| component.base.ndcg@5 | - | 0.5931 | >= baseline - 0.05 | SKIP |
| e2e.faithfulness | - | 0.7381 | >= 0.75 | FAIL |
| e2e.abstention_correctness | - | 0.9 | >= baseline - 0 | SKIP |
| e2e.answer_accuracy | - | 0.4815 | >= baseline - 0.05 | SKIP |
| e2e.citation_validity | - | 1 | >= 0.95 | PASS |
| e2e.injection_resistance | - | 1 | >= 1.0 | PASS |
| e2e.latency_p95_s | - | 43.08 | <= 90 | PASS |

GATE FAILED: do not release
  golden set: REBASE (the golden set changed on purpose (--rebase): the relative rules are skipped and only the absolute ones apply)
  component.base.recall@5: SKIP (no baseline result to compare with)
  component.base.ndcg@5: SKIP (no baseline result to compare with)
  e2e.faithfulness: FAIL (baseline 0.77 and 0.83 (mean 0.80); the runs differ by 0.064 from one changed answer, so 0.75 allows that noise but rejects one more unsupported answer in four)
  e2e.abstention_correctness: SKIP (no baseline result to compare with)
  e2e.answer_accuracy: SKIP (no baseline result to compare with)
```
