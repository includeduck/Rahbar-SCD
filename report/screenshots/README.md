# Screenshots for the report appendix (Assignment 03)

Taken by Wasay on his own WSL minikube cluster. Each shows the command and its full output.

| Item | File | Command | Shows |
| --- | --- | --- | --- |
| A1 | `A1-batch-b-verification-report.png` | `kubectl apply -f k8s/ingest-batch-b.yaml`, then `kubectl -n rahbar logs -f job/ingest-batch-b -c indexer` | Verification report for Batch B: pass or fail per check with reasons; 6 quarantined; the 2026-27 fee page archived by the 2027-28 revision (run 20261121T141613Z) |
| A2 | `A2-unchanged-rerun-0-embedded.png` | `kubectl -n rahbar create job --from=cronjob/reindex rerun-unchanged-3`, then its logs | A second ingestion run on an unchanged corpus: 0 chunks embedded, 699 reused (run 20261121T141010Z) |
