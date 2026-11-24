# suite-baseline-20261124T104953Z

| level | metric | run 1 | run 2 | difference |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 263 | 205.8 | -57.2 |
| module | embedding ms per chunk (batched) | 210.7 | 189.2 | -21.5 |
| module | determinism: largest difference | 0 | 0 | 0 |
| component | recall@3 | 0.5833 | 0.5833 | 0 |
| component | recall@5 | 0.6667 | 0.6667 | 0 |
| component | MRR | 0.5396 | 0.5396 | 0 |
| component | nDCG@5 | 0.5499 | 0.5499 | 0 |
| component | recall@5 with typos | 0.5 | 0.5 | 0 |
| component | MRR with typos | 0.4479 | 0.4479 | 0 |
| component | nDCG@5 with typos | 0.4396 | 0.4396 | 0 |
| component | recall@5 with 3 distractor documents | 0.6667 | 0.6667 | 0 |
| component | MRR with distractors | 0.5187 | 0.5187 | 0 |
| component | nDCG@5 with distractors | 0.5346 | 0.5346 | 0 |
| e2e | faithfulness (judge) | 0.7692 | 0.8333 | 0.0641 |
| e2e | abstention correctness (unanswerable) | 1 | 1 | 0 |
| e2e | false abstention rate (answerable) | 0.4583 | 0.5 | 0.0417 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy (expected phrase) | 0.45 | 0.45 | 0 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 (s) | 20.45 | 14.47 | -5.98 |
| e2e | latency p95 (s) | 55.8 | 23.87 | -31.93 |

- Suite `baseline` at 20261124T104953Z (git 4e267ed), collection `rahbar-nomic-embed-text-c400o50`, embedding `nomic-embed-text`, generation `llama3.2:3b` (temperature 0.1, seed 42), judge `llama3.2:3b`, suite seed 42.
- Golden set: 30 questions, 16 answerable, 6 unanswerable, 8 hard; 18 human, 12 synthetic (8 reviewed by hand).
- Between the runs 29 answers were identical; changed: q09; judge verdicts changed: none; retrieval rankings changed: none.

```
release gate: baseline (rahbar-nomic-embed-text-c400o50) against baseline
| metric | baseline | baseline (rahbar-nomic-embed-text-c400o50) | rule | outcome |
|---|---|---|---|---|
| component.base.recall@5 | - | 0.6667 | >= baseline - 0.02 | SKIP |
| component.base.ndcg@5 | - | 0.5499 | >= baseline - 0.05 | SKIP |
| e2e.faithfulness | - | 0.8013 | >= 0.75 | PASS |
| e2e.abstention_correctness | - | 1 | >= baseline - 0 | SKIP |
| e2e.answer_accuracy | - | 0.45 | >= baseline - 0.05 | SKIP |
| e2e.citation_validity | - | 1 | >= 0.95 | PASS |
| e2e.injection_resistance | - | 1 | >= 1.0 | PASS |
| e2e.latency_p95_s | - | 55.8 | <= 90 | PASS |

GATE PASSED: release
  component.base.recall@5: SKIP (no baseline result to compare with)
  component.base.ndcg@5: SKIP (no baseline result to compare with)
  e2e.abstention_correctness: SKIP (no baseline result to compare with)
  e2e.answer_accuracy: SKIP (no baseline result to compare with)
```
