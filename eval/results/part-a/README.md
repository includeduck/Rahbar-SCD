# Part A results: verification and incremental update

Runs on the WSL minikube cluster on 21 November 2026, in this order. Each run left a
verification report and an ingestion report in `reports/` (named by run id) and a log here.

| Run (Job) | Run id | What it shows | Chunks embedded / reused / deleted |
| --- | --- | --- | --- |
| `indexer` (first ingestion) | 20261121T133344Z | 9 documents verified; recency archives the 2023 rules and the 2019-20 annual report | 678 / 0 / 0 |
| `rerun-unchanged-1` | 20261121T134824Z | unchanged corpus: nothing embedded (its Job and log were removed by the CronJob's history limit; the reports remain) | **0** / 678 / 0 |
| `ingest-batch-b` | 20261121T135051Z | Batch B: 6 quarantined with reasons, the 2026-27 fee page archived by the 2027-28 revision, 4 added | 25 / 678 / 0 |
| `change-and-delete-1` | 20261121T135438Z | `exam-hall-rules.md` changed: only its chunks replaced (12 new ids); `course-withdrawal-faq.md` deleted: 0 chunks left | 12 / 687 / 16 |
| `rerun-pinned-1` | 20261121T135718Z | after `restore`, the pinned 2026-27 fee page stays current ([restore-demo.txt](restore-demo.txt)) | 0 / 699 / 0 |
| `rerun-unpinned-1` | 20261121T135918Z | after `unpin`, the 2027-28 revision is current again | 0 / 699 / 0 |
| `rerun-unchanged-2` | 20261121T140301Z | unchanged corpus again, with its log | **0** / 699 / 0 |

- `counts-final.txt`: chunks per document id, ledger against Chroma, and **0 orphan chunks**.
- `quarantine-listing.txt`: the quarantined Batch B files and their `.reason.json` files.
- `ledger.json`: the ledger after the last run (document fingerprints removed for size).
- Thresholds used by the checks: `docs/evidence/verification-thresholds.txt`.
