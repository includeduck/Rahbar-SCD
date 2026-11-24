Baseline: baseline (rahbar-nomic-embed-text-c400o50, 2 runs)  
Candidate: candidate-c250o50 (rahbar-nomic-embed-text-c250o50, 2 runs)  
Same golden set: True

| level | metric | baseline | candidate | change |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 234.4 | 215.35 | -19.05 |
| module | embedding ms per chunk (batched) | 199.95 | 203.65 | 3.7 |
| module | determinism: largest difference | 0 | 0 | 0 |
| component | recall@3 | 0.5833 | 0.625 | 0.0417 |
| component | recall@5 | 0.6667 | 0.7083 | 0.0416 |
| component | MRR | 0.5396 | 0.5583 | 0.0187 |
| component | nDCG@5 | 0.5499 | 0.5744 | 0.0245 |
| component | recall@5 with typos | 0.5 | 0.5 | 0 |
| component | MRR with typos | 0.4479 | 0.4167 | -0.0312 |
| component | nDCG@5 with typos | 0.4396 | 0.4159 | -0.0237 |
| component | recall@5 with 3 distractor documents | 0.6667 | 0.6667 | 0 |
| component | MRR with distractors | 0.5187 | 0.5292 | 0.0105 |
| component | nDCG@5 with distractors | 0.5346 | 0.5429 | 0.0083 |
| e2e | faithfulness (judge) | 0.8013 | 0.8462 | 0.0449 |
| e2e | abstention correctness (unanswerable) | 1 | 1 | 0 |
| e2e | false abstention rate (answerable) | 0.4791 | 0.4583 | -0.0208 |
| e2e | citation validity | 1 | 1 | 0 |
| e2e | answer accuracy (expected phrase) | 0.45 | 0.5 | 0.05 |
| e2e | injection resistance | 1 | 1 | 0 |
| e2e | latency p50 (s) | 17.46 | 16 | -1.46 |
| e2e | latency p95 (s) | 55.8 | 41.37 | -14.43 |

Questions that moved (run 1 of each):

- q09 recall@5 0 -> 1
- q04 abstained -> answered
- q09 answered -> abstained
