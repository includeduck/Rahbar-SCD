# suite-baseline-20261124T165604Z

| level | metric | run 1 | run 2 | difference |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 243.8 | 209.5 | -34.3 |
| module | embedding ms per chunk (batched) | 203.3 | 192.9 | -10.4 |
| module | determinism: largest difference | 0 | 0 | 0 |
| component | recall@3 | 0.6061 | 0.6061 | 0 |
| component | recall@5 | 0.6818 | 0.6818 | 0 |
| component | MRR | 0.554 | 0.554 | 0 |
| component | nDCG@5 | 0.5626 | 0.5626 | 0 |
| component | recall@5 with typos | 0.5606 | 0.5606 | 0 |
| component | MRR with typos | 0.4949 | 0.4949 | 0 |
| component | nDCG@5 with typos | 0.4906 | 0.4906 | 0 |
| component | recall@5 with 3 distractor documents | 0.6818 | 0.6818 | 0 |
| component | MRR with distractors | 0.5389 | 0.5389 | 0 |
| component | nDCG@5 with distractors | 0.5514 | 0.5514 | 0 |
| e2e | faithfulness (judge) | 0.4286 | 0.4762 | 0.0476 |
| e2e | abstention correctness (unanswerable) | 0.9 | 0.9 | 0 |
| e2e | false abstention rate (answerable) | 0.3939 | 0.3939 | 0 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy (expected phrase) | 0.4815 | 0.4815 | 0 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 (s) | 20.27 | 16.54 | -3.73 |
| e2e | latency p95 (s) | 42.73 | 26.51 | -16.22 |

- Suite `baseline` at 20261124T165604Z (git 2501074), collection `rahbar-nomic-embed-text-c400o50`, embedding `nomic-embed-text`, generation `llama3.2:3b` (temperature 0.1, seed 42), judge `llama3.2:3b`, suite seed 42.
- Golden set: 43 questions, 22 answerable, 10 unanswerable, 11 hard; 18 human, 25 synthetic (8 reviewed by hand).
- Between the runs 39 answers were identical; changed: q08, e03, e04, e13; judge verdicts changed: none; retrieval rankings changed: none.

```
release gate: baseline (rahbar-nomic-embed-text-c400o50) against baseline (rahbar-nomic-embed-text-c400o50)
| metric | baseline (rahbar-nomic-embed-text-c400o50) | baseline (rahbar-nomic-embed-text-c400o50) | rule | outcome |
|---|---|---|---|---|
| golden set | 6bc3d49c5de9 | 51038c2438cc | same questions | FAIL |
| component.base.recall@5 | 0.6667 | 0.6818 | >= baseline - 0.02 = 0.6467 | PASS |
| component.base.ndcg@5 | 0.5499 | 0.5626 | >= baseline - 0.05 = 0.4999 | PASS |
| e2e.faithfulness | 0.8013 | 0.4524 | >= 0.75 | FAIL |
| e2e.abstention_correctness | 1 | 0.9 | >= baseline - 0 = 1.0000 | FAIL |
| e2e.answer_accuracy | 0.45 | 0.4815 | >= baseline - 0.05 = 0.4000 | PASS |
| e2e.citation_validity | 1 | 1 | >= 0.95 | PASS |
| e2e.injection_resistance | 1 | 1 | >= 1.0 | PASS |
| e2e.latency_p95_s | 55.8 | 42.73 | <= 90 | PASS |

GATE FAILED: do not release
  golden set: FAIL (results on different golden sets are not comparable: re-run the baseline)
  e2e.faithfulness: FAIL (baseline 0.77 and 0.83 (mean 0.80); the runs differ by 0.064 from one changed answer, so 0.75 allows that noise but rejects one more unsupported answer in four)
  e2e.abstention_correctness: FAIL (declining the unanswerable questions may not get worse (baseline 6 of 6))
```
