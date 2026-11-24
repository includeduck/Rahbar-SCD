Baseline: candidate-stale-g1795268618 (rahbar-g1795268618, 1 run)  
Candidate: baseline (rahbar-nomic-embed-text-c400o50, 2 runs)  
Same golden set: True

| level | metric | baseline | candidate | change |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 239.6 | 234.4 | -5.2 |
| module | embedding ms per chunk (batched) | 245.1 | 199.95 | -45.15 |
| module | determinism: largest difference | 0 | 0 | 0 |
| component | recall@3 | 0.4167 | 0.5833 | 0.1666 |
| component | recall@5 | 0.5 | 0.6667 | 0.1667 |
| component | MRR | 0.4146 | 0.5396 | 0.125 |
| component | nDCG@5 | 0.414 | 0.5499 | 0.1359 |
| component | recall@5 with typos | 0.3333 | 0.5 | 0.1667 |
| component | MRR with typos | 0.3229 | 0.4479 | 0.125 |
| component | nDCG@5 with typos | 0.3037 | 0.4396 | 0.1359 |
| component | recall@5 with 3 distractor documents | 0.5 | 0.6667 | 0.1667 |
| component | MRR with distractors | 0.4146 | 0.5187 | 0.1041 |
| component | nDCG@5 with distractors | 0.414 | 0.5346 | 0.1206 |
| e2e | faithfulness (judge) | 0.8 | 0.8013 | 0.0012 |
| e2e | abstention correctness (unanswerable) | 1 | 1 | 0 |
| e2e | false abstention rate (answerable) | 0.5833 | 0.4791 | -0.1042 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy (expected phrase) | 0.3 | 0.45 | 0.15 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 (s) | 14.23 | 17.46 | 3.23 |
| e2e | latency p95 (s) | 31.59 | 55.8 | 24.21 |

Questions that moved (run 1 of each):

- q07 recall@5 1 -> 0
- q13 recall@5 0 -> 1
- s01 recall@5 0 -> 1
- s02 recall@5 0 -> 1
- s03 recall@5 0 -> 1
- s04 recall@5 0 -> 1
- q09 abstained -> answered
- q13 answered -> abstained
- s01 abstained -> answered
- s03 abstained -> answered
- s04 abstained -> answered
