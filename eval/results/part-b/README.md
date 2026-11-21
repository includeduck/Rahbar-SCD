# Part B results: the baseline suite

`make eval-suite` on 21 Nov 2026: the Job [k8s/eval-job.yaml](../../../k8s/eval-job.yaml) on
the WSL minikube, image built at git `4c344ff`. Collection `rahbar-nomic-embed-text-c400o50`
(after Batch B, 703 chunks), `nomic-embed-text`, `llama3.2:3b` at temperature 0.1 and seed 42,
the same model as judge. Two full runs, 28 minutes in all.

| file | |
|---|---|
| [suite-baseline-20261121T151433Z.md](suite-baseline-20261121T151433Z.md) | the summary table |
| [suite-baseline-20261121T151433Z.json](suite-baseline-20261121T151433Z.json) | every number, every retrieved list, every answer and judge verdict, both runs |
| [suite-baseline-20261121T151433Z-judge-sheet.jsonl](suite-baseline-20261121T151433Z-judge-sheet.jsonl) | the 13 judged answers of run 1, for hand labelling |
| [eval-suite-job.log](eval-suite-job.log) | the Job's log |

| level | metric | run 1 | run 2 | difference |
|---|---|---|---|---|
| module | paraphrase pairs, mean cosine | 0.8012 | 0.8012 | 0 |
| module | unrelated pairs, mean cosine | 0.4062 | 0.4062 | 0 |
| module | gap (paraphrase - unrelated) | 0.395 | 0.395 | 0 |
| module | embedding ms per chunk (one per request) | 228.9 | 216.7 | -12.2 |
| module | embedding ms per chunk (batched) | 227.2 | 208.7 | -18.5 |
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
| e2e | latency p50 (s) | 21.12 | 13.91 | -7.21 |
| e2e | latency p95 (s) | 64.04 | 23.79 | -40.25 |

## Findings

**Module.** The embedding separates meaning well: paraphrase pairs average 0.801, unrelated
pairs from the same domain 0.406, a gap of 0.395. It is exactly deterministic: the same six
texts embedded twice differ by 0 in every dimension. On the laptop CPU one chunk takes about
220 ms to embed, and batching 40 chunks in one request saves almost nothing (Ollama works
through them one by one), which is why re-embedding 678 chunks takes minutes and why the
incremental update (Part A) matters.

**Component.** recall@5 is 0.667 on the 24 labelled questions, MRR 0.540, nDCG@5 0.550.
- *Typos hurt.* With one or two typing errors per question, recall@5 falls from 0.667 to 0.500,
  MRR by 0.092 and nDCG@5 by 0.110. Short questions suffer most: q02, q03, q04 and q13 lose their
  answer entirely. The embedding model sees "condone" and "codnone" as different words.
- *Distractors barely hurt.* Three made-up documents (another university's rules on the same
  topics, a fee schedule, a sports page) reach the top 5 for 12 of 30 questions but push out
  no labelled chunk: recall@5 is unchanged, MRR falls by 0.021 and nDCG@5 by 0.015, because the
  distractors mostly rank below the right answer.
- *Where retrieval misses.* q12 asks for "clause 1.21" by number: the embedding has no notion
  of clause numbers and returns a heading. q11 asks about "the 2023 rules", which does not
  trigger the version filter, so the superseded Aug-2023 rules are never searched. q07's only
  answer is in the archived 2026-27 fee page (see Part A). Two misses are label gaps, not
  retrieval failures: q06 ("3.33" for a B+) was answered correctly from the academic rules'
  grade table (clause 2.3), and q05 from the grading page's "Repeating a Course", but each
  question's label names only the other document. The labels are left as written (they were
  set before tuning); the report counts both.

**End to end.** Every unanswerable question was declined (6 of 6), every citation names a
retrieved passage, and the injection question was not obeyed. The weak point is the other
direction: **11 of 24 answerable questions abstained** (12 in run 2), so answer accuracy is
0.45. In most of them retrieval was right (q10, q13, q16, q17: recall@5 = 1): the model
returned `answered_from_context=false`, and since Assignment 02 that always abstains. The
judge found 10 of 13 answers supported. The 3 it rejected are wrong: q03 says the admission
"will be automatically closed", q08 overstates the AI policy, q09 says a failed course is
retaken "upon the commencement of the semester". These are candidates for Part C (the prompt
template) and for D5.

**Stochasticity.** With the seeds fixed, everything that does not involve the language model is
identical across the two runs: all module and component metrics, and all 30 retrieval
rankings. 29 of 30 answers are identical, and so is every judge verdict. The one change, q09
(answered in run 1, abstained in run 2), had the same prompt (SHA-256 `9de3e6a2...` in both
traces), the same seed and the same temperature, and still produced 111 tokens once and 119
the next time. A seed fixes the sampler's random draws, not the arithmetic: llama.cpp on a CPU
sums in multi-threaded batches and reuses cached state for a shared prompt prefix, so the
logits can differ in the last bits, and at temperature 0.1 that is enough to flip a near-tie
between "answer" and "abstain". Faithfulness (+0.064) and the false-abstention rate (+0.042)
move only because of q09.

Latency is the least stable number: run 1's p95 was 64 s, run 2's 24 s. The slow calls had
exactly the same prompt and output token counts as in run 2 (q04: 1827 in and 84 out both
times, 64.4 s against 12.2 s for the model call), so the difference is the speed of the host,
not the pipeline: most likely other load on the laptop during run 1, which seeds cannot
control. A release gate on latency therefore needs a margin, or runs on a quiet machine.
