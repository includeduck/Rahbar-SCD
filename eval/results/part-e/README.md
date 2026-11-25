# Part E results: drift and coverage

Run on 24 Nov 2026 on the WSL minikube, against the live api (collection
`rahbar-nomic-embed-text-c250o50`, released in Part C).

| step | object | output here |
|---|---|---|
| E1 send the live queries | Job `live-traffic` ([k8s/live-traffic-job.yaml](../../../k8s/live-traffic-job.yaml), `eval/live_traffic.py`) | [live-traffic-job.log](live-traffic-job.log), [live-query-log.jsonl](live-query-log.jsonl) (the 42 traces, without prompts) |
| E2 coverage, before | Job `coverage-check` ([k8s/coverage-job.yaml](../../../k8s/coverage-job.yaml), `eval/coverage.py`), label `before` | [coverage-before-20261124T165140Z.md](coverage-before-20261124T165140Z.md) (+ .json), [job log](coverage-before-job.log): **Failed** |
| E4 coverage, after 13 new golden questions | the same Job, label `after`, image rebuilt with the new golden set | [coverage-after-20261124T165552Z.md](coverage-after-20261124T165552Z.md) (+ .json), [job log](coverage-after-job.log): **Complete** |
| E screenshots: before and after, both on the cluster | Job `coverage-check`; for before, the 30-question golden set from git in the ConfigMap `coverage-golden` | [coverage-live-20261124T184208Z.md](coverage-live-20261124T184208Z.md) (35.7%, Failed), [coverage-live-20261124T184339Z.md](coverage-live-20261124T184339Z.md) (85.7%, Complete); `report/screenshots/E-coverage-*.png` |
| E4 re-run the suite | Job `eval-suite` on the 43-question golden set | [eval-suite-after-e4-job.log](eval-suite-after-e4-job.log) and the suite result below |

## E1. The live query log

[eval/live_queries.jsonl](../../live_queries.jsonl): 42 queries written as students would type
them (lower case, "probation", "ChatGPT", "HOD"):

- **28 in the golden set's domain**. 16 ask about topics the golden set already tests
  (warning, attendance, repeats, grades, refunds, the AI policy, course load, fees, the
  exam hall, withdrawal, credit transfer, hostels). 12 ask about topics the corpus answers
  but no golden question tests: library services, academic integrity, freezing a semester.
- **14 about campus IT** (Wi-Fi, accounts and passwords, software licences, printing,
  storage, VPN): neither the corpus nor the golden set covers it. The corpus has only near
  misses (the computer lab rules, "WiFi" in an annual report's project title).

Job `live-traffic` sends them to the live api through its Service, one at a time, with no
cache bypass and request ids `live-l01` to `live-l42`. Each request leaves a trace on the
`traces` volume (all 42 name `rahbar-nomic-embed-text-c250o50`). The coverage check reads the
queries back from those traces, not from the file: it measures what the api actually received.

What the log already says without labels: the out-of-domain queries' top-1 similarity is
0.58 to 0.63 (mean 0.60) against 0.56 to 0.83 (mean 0.73) in the domain. 13 of 14 abstained,
but **live-l37** ("Where can I print my assignment on campus...") was answered: "printing is
available on campus", from a handbook passage about the library's "print and online
resources" (printed books). That is an ungrounded answer from a word match. Golden question
e10 now tests it. live-l19 (the late library fine) was answered "PKR 10/item/day" from the
handbook, although Batch B's library guide says Rs. 50: the corpus contradicts itself, which
golden question e13 now tests.

## E2. Query coverage

For each live query, `eval/coverage.py` finds the most similar golden question (cosine
similarity of `nomic-embed-text` embeddings, the api's own model). A query is **covered** when
that similarity is at least **0.70**. The golden set covers the traffic when at least **85%**
of queries are covered, the share in the paper's example. Below that the script exits 1, so
Job `coverage-check` ends Failed, the same pattern as the release gate. Both numbers are in
the `coverage:` block of the `rahbar-gate` ConfigMap, next to the gate's rules.

**Why 0.70.** It comes from the module level's own embedding pairs:

- paraphrase pairs (the same question in other words) score 0.64 to 0.90, and 13 of 16
  reach 0.70;
- pairs of different questions from the same university domain score 0.32 to 0.46.

So 0.70 means "a golden question asks about as nearly the same thing as a paraphrase does".
It sits 0.24 above any unrelated pair, so sharing the domain or a word is not enough. The
sensitivity row in the outputs shows how the share moves with the threshold.

**Grouping.** Uncovered queries are grouped by average link: two groups merge while their
queries' mean similarity is at least 0.55, between the two ranges above. Single link was tried
first and chained 16 queries from four topics into one group (every question here shares the
domain). Each group is named by its most central query. The author's topic tags are printed
beside each group as a check, but are not used to form the groups.

**Before: 15 of 42 covered, 35.7%, COVERAGE GAP** (in the domain 14 of 28, campus IT 1 of 14, and that one only by accident, below).

| uncovered topic (automatic group) | queries | topic |
|---|---|---|
| "What's the student wifi password?" | l29 to l34 | campus IT: Wi-Fi and accounts |
| "What is the penalty for plagiarism in an assignment?" | l21, l22, l23, l37 | academic integrity (l37, printing an assignment, joins it on the word "assignment") |
| "Can I freeze my semester because of a medical problem?" | l25, l27, l28, l02 | semester freeze (l02, missing classes, joins it) |
| "What is the fine for returning a library book late?" | l17, l18, l19 | library services |
| "Does the university give students free GitHub Copilot or Azure credits?" | l35, l38, l39 | campus IT: software and equipment |
| "Can I access the library's online journals from home?" | l20, l40 | remote access (library and VPN) |
| six single queries | l01, l08, l24, l36, l41 | "probation" (l01, 0.696 to s05), "ChatGPT" (l08, 0.615 to q08), Turnitin, Drive storage, MATLAB |

Two of the "covered" queries are covered by the wrong golden question: l26 ("How many
semesters can I freeze...") by q10 about course load (0.722), and l42 (hostel Wi-Fi) by q04
about hostels (0.710). A similarity threshold measures closeness of wording, not of the
answer. The report lists each query's nearest golden question so this can be checked.
l01 and l08 show a different gap: the topic is tested, but only in the documents' words
("academic warning", "Generative AI"), not the users' ("probation", "ChatGPT").

## E3. Data drift: the suite before and after Batch B

Batch B was ingested in Part A (Job `ingest-batch-b`). The blue/green indexer of Assignment 02
kept the generation from before it, `rahbar-g1795268618`, and comparing chunk texts shows it
is the same index minus Batch B:

| | before Batch B: `rahbar-g1795268618` | after Batch B: `rahbar-nomic-embed-text-c400o50` |
|---|---|---|
| chunks | 678 | 703 |
| chunk texts | the same 678 | the same 678 + 25 new: `exam-hall-rules.md` 11, `library-services-guide.md` 5, `course-withdrawal-faq.md` 5, `fee-structure-2027-28.md` 4 |
| 2026-27 fee page (9 chunks) | current | superseded by the 2027-28 revision, so filtered out by default |
| also from Batch B | | 6 files quarantined by verification (Part A) |

So the two suite runs differ only in Batch B. Both ran in Part C with the same image, golden
set, seeds and model: the after run is Job `eval-suite` (the baseline, 2 runs), the before run
is Job `eval-candidate-stale` (1 run; its retrieval is deterministic).
Full table: [drift-before-vs-after-batch-b.md](drift-before-vs-after-batch-b.md)
(`python -m eval.compare <before.json> <after.json>`; it calls them baseline and candidate).

| level | metric | before | after | change |
|---|---|---|---|---|
| module | paraphrase / unrelated cosine, gap | 0.801 / 0.406, 0.395 | the same | 0: same model, and the module level does not see the documents |
| component | recall@5 | 0.500 | 0.667 | +0.167 (4 of 24 labelled questions) |
| component | MRR / nDCG@5 | 0.415 / 0.414 | 0.540 / 0.550 | +0.125 / +0.136 |
| component | recall@5 with typos / with distractors | 0.333 / 0.500 | 0.500 / 0.667 | +0.167 / +0.167 |
| e2e | answer accuracy | 0.30 | 0.45 | +0.15 (3 of 20) |
| e2e | false abstention rate | 0.583 | 0.479 | -0.104 |
| e2e | faithfulness (judge) | 0.800 | 0.801 | 0 |
| e2e | abstention correctness, citation validity, injection | 1, 1, 1 | 1, 1, 1 | 0 |
| e2e | latency p50 / p95 (s) | 14.2 / 31.6 | 17.5 / 55.8 | not a reliable signal: host load differed between the runs (Part B saw p95 move from 64 to 24 s between two identical runs), and more questions are answered, which takes longer than a refusal |

What changed, question by question, and why:

| question | before | after | why |
|---|---|---|---|
| s01 How early must students reach the examination hall? | abstained; retrieved 5.15 "late holding of exams" | rank 1 `exam-hall-rules.md` 1.1, "at least 15 minutes" | a new document answers a question nothing answered before |
| s03 Until which week can I withdraw from a course? | abstained | the withdrawal FAQ retrieved, "the end of the 12th week" | the same |
| s04 Admission fee in the 2027-28 fee structure? | abstained: only the 2026-27 page existed | `fee-structure-2027-28.md`, "Rs 35,000" | the same |
| s02 Which grade shows for a withdrawn course? | answered "W" from handbook 2.8 | answered "W"; the FAQ now ranks first | recall 0 → 1 only because the label names the new FAQ: the answer did not change |
| q13 Tuition fee per credit hour for BS? | **answered "Rs. 12,000"** from the 2026-27 page | rank 1 is the 2027-28 table, **but the model abstains** | the fact changed (12,000 → 13,500) and its shape changed: the new table gives "Rs. 13,500" without saying "per credit hour", so the model cannot confirm the unit. Before Batch B the answer was confident and out of date. |
| q07 Refund if a course is dropped after 10 days? | recall 1: the 2026-27 page's refund section | recall 0 | the only refund policy is in the superseded page, which the default filter now hides; the 2027-28 revision has no refund section (the D5 finding). Abstained in both. |
| q09 When must a failed course be retaken? | abstained | answered | **not drift**: the same three chunks were retrieved in both; q09 is the one answer that changes between seeded runs (Part B) |

Why it matters for the release gate: on the stale index, faithfulness is unchanged (0.80):
the old answers are faithful to the old passages they were given. Only the levels that compare
against labels of the current documents (recall, nDCG, answer accuracy) see that the index is
out of date. That is why the gate in Part C rejects `rahbar-g1795268618`, and why a gate on
faithfulness alone would have released it.

## E4. Closing the gap

13 golden questions were added (`e01` to `e13` in [eval/golden.jsonl](../../golden.jsonl)), one or
more per uncovered topic. Each was written from what the documents say on that topic, with
relevance labels found by retrieving and reading the chunks. They were not written as
paraphrases of the queries:

| topic | new questions | kind |
|---|---|---|
| library services | e01 opening hours, e02 borrowing limit, e13 late fine | answerable from Batch B's guide; e13 is `hard: conflict` because the handbook gives a different fine |
| academic integrity | e03 penalties (8.14, 8.15), e04 what plagiarism is (8.7, 8.8) | answerable |
| semester freeze | e05 how long (4.18), e06 by when to request it (4.19) | answerable |
| campus IT | e07 Wi-Fi, e08 account passwords, e09 software licences, e10 printing | unanswerable: must abstain (e10 is l37's ungrounded answer) |
| tested topics in users' words | e11 "ChatGPT" (paraphrase of q08), e12 "probation" (paraphrase of q01) | hard: paraphrase |

They are synthetic, and all 13 were reviewed by hand by Wasay (`reviewed_by`), with s09. The
suite run below used an image built before that review, so its summary line still counts 8
reviewed questions; the review changed no question, label or expected phrase.

**After: 36 of 42 covered, 85.7%, COVERAGE OK** (in the domain 25 of 28, campus IT 11 of
14), and Job `coverage-check` ends Complete. The script exits 0 now and exited 1 before.

| | before | after |
|---|---|---|
| golden questions | 30 | 43 |
| covered at 0.70 | 15 of 42 (35.7%) | 36 of 42 (85.7%) |
| at 0.60 / 0.65 / 0.75 / 0.80 | 64.3% / 42.9% / 26.2% / 21.4% | 95.2% / 88.1% / 66.7% / 42.9% |
| exit code, Job | 1, Failed | 0, Complete |

Still uncovered: l02 (missing classes before the final, 0.680 to q09; q18 asks it formally),
l20 and l40 (remote access, VPN), l24 (Turnitin), l36 (Drive storage), l39 (laptop loans).

Two notes on honesty:

- With 12 new questions the share was 83.3%. The 13th (e13, the late fine) was added
  because the live log showed the corpus contradicts itself on it, which is worth testing
  for its own sake. Without it the check would still fail.
- The first draft of e13 came out word for word as live query l19 (similarity 1.0), and
  e11 nearly as l08 (0.948). A golden question that copies a query makes coverage measure
  the copying, so both were reworded before the final run (commit `2501074`). The share
  stayed 85.7%. The share is close to the bar: one more uncovered query fails it.

The coverage check needs no model calls and runs in seconds, so a CronJob or the eval Job
could run it on each day's traces. The trace summary's top-1 similarity is the cheaper
signal that something new is being asked.

## E4. Re-running the suite on the 43 questions

Job `eval-suite` with `EVAL_REBASE=1`: the golden set changed on purpose, so the gate applies
only its absolute rules, and a pass would become the new baseline.
Files: [suite-baseline-20261124T175449Z.md](suite-baseline-20261124T175449Z.md) (+ .json,
judge sheet), [log](eval-suite-rebase-job.log). Collection `rahbar-nomic-embed-text-c250o50`,
two seeded runs.

| metric | 30 questions (Part C, c250o50) | 43 questions | of which the 13 new |
|---|---|---|---|
| recall@5 / nDCG@5 | 0.708 / 0.574 | 0.712 / 0.593 | e01, e02, e05, e11, e12, e13 found; e04 half; e03, e06 missed |
| faithfulness (judge) | 0.846 | 0.714 / 0.762 (mean 0.738) | 4 of 8 and 5 of 8 supported; the 30 old questions 11 of 13, as in Part C |
| abstention correctness | 1 | 0.9 | e07 (campus Wi-Fi) answered "Through VPN" |
| answer accuracy | 0.50 | 0.48 | |
| citation validity, injection resistance | 1, 1 | 1, 1 | |
| latency p95 (worst run) | 41.4 s | 43.1 s | |

**GATE FAILED: faithfulness 0.738 is below the 0.75 floor**, so the run did not become the
baseline (`/results/baseline.json` is still the 30-question c400o50 one). The new questions
expose what the coverage gap was hiding. The live log already showed it, and the suite now
measures it:

- **e07**, campus Wi-Fi: no document answers it. Retrieval brought the 2023-24 annual
  report's campus descriptions (labs, networks; top-1 0.647, above the 0.58 relevance floor),
  and the model answered "Through VPN". This is the same
  near miss as live query l40, and the reason e07 exists.
- **e03**, penalties: retrieval missed clauses 8.14 and 8.15 (recall 0). The model listed
  penalties from neighbouring clauses that do not say them.
- **e06**, when to request a freeze: clause 4.19 was not retrieved. The model answered how
  long a freeze lasts instead.
- **e11**, "ChatGPT": the right chunk was retrieved (recall 1). The answer garbled a list
  that the PDF's two columns had interleaved.

The threshold was not lowered to let the run pass. The golden set now asks what users ask,
and on that the live system is below the bar on faithfulness. The fixes this points to are
retrieval of the 8.x and 4.19 clauses (e03, e06), and abstaining when the top passages share
only a word with the question (e07). The next candidate is gated on the 43 questions.

### A broken run first, and what it fixed

The first run on the 43 questions ([log](eval-suite-after-e4-job.log),
[result](suite-baseline-20261124T165604Z.md)) showed faithfulness **0.45** and named the
collection `c400o50`. The answers were fine; the measurement was wrong. Since the Part C
release, the api has been pinned to `c250o50`, but the suite's retriever followed the
`rahbar-active` pointer, which only the ingestion Job moves and which still named `c400o50`.
The judge's evidence is looked up by chunk id, and an id is `hash(document, sha, index)`. So
the judge read 400-word chunk *i* as the evidence for 250-word chunk *i*, and called correct
answers unsupported: q02 "20%" was judged "not supported" next to clause 1.21's 20%.

Fixed in commit `2fd9461`: `/readyz` names the collection the api serves, the suite measures
that one (and stops if told to measure another), and the ingestion Job now writes `c250o50`
(Part F). It is an example for G4(d): the metric I trust least, the LLM judge, also depends
on getting the right evidence, which nothing in the judge's own output reveals.
