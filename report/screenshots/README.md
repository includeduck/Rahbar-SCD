# Screenshots for the report appendix (Assignment 03)

Taken by Wasay on his own WSL minikube cluster. Each shows the command and its full output.

| Item | File | Command | Shows |
| --- | --- | --- | --- |
| A1 | `A1-batch-b-verification-report.png` | `kubectl apply -f k8s/ingest-batch-b.yaml`, then `kubectl -n rahbar logs -f job/ingest-batch-b -c indexer` | Verification report for Batch B: pass or fail per check with reasons; 6 quarantined; the 2026-27 fee page archived by the 2027-28 revision (run 20261121T141613Z) |
| A2 | `A2-unchanged-rerun-0-embedded.png` | `kubectl -n rahbar create job --from=cronjob/reindex rerun-unchanged-3`, then its logs | A second ingestion run on an unchanged corpus: 0 chunks embedded, 699 reused (run 20261121T141010Z) |
| D1 | `D1-full-trace-record.png` | `kubectl -n rahbar exec deploy/api -- grep d03-injection-question /traces/traces-api-7759c8f5bf-gkcn9.jsonl` | One full trace record, as stored on the traces volume: one JSON line with every D1 field, both injection rails' actions and the model's raw output (pretty-printed copy: `eval/results/part-d/trace-record-d03.json`) |
| D2 | `D2-trace-tool-rebuilds-prompt.png` | `kubectl -n rahbar exec deploy/api -- python -m app.trace_tool d03-injection-question` | The trace tool for one trace id: retrieved chunks with versions (the injected notice ranked 1 and DROPPED), guardrails, model call, and the exact prompt with its SHA-256 checked against the trace |
