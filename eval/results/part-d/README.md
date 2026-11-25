# Part D results: traces, trace tool, metrics, guardrail logging

Run on 21 Nov 2026 on the WSL minikube (dev-wsl overlay, api on `llama3.2:3b` through Ollama on
the host, collection `rahbar-nomic-embed-text-c400o50` after Batch B, 703 chunks).

## What was run

```bash
# 11 questions with fixed request ids, sent from inside an api pod (eval/trace_queries.py)
kubectl -n rahbar exec -i deploy/api -- python - < eval/trace_queries.py
# the pod's trace file, copied out: ../../../traces/part-d/
kubectl -n rahbar exec <pod> -- cat /traces/traces-<pod>.jsonl > traces/part-d/traces-<pod>.jsonl
# then, from the committed traces (the same commands work in the pod without --dir)
python -m app.trace_tool <id> --dir ../traces/part-d
python -m app.trace_summary --dir ../traces/part-d --since 2026-11-21T14:30 --until 2026-11-21T14:40
# every chunk in the collection through the injection patterns
kubectl -n rahbar exec -i deploy/api -- python - < eval/injection_scan.py
```

| file | what it shows |
|---|---|
| [trace-list.txt](trace-list.txt) | the 11 traces: id, outcome, top-1 similarity |
| [trace-record-d03.json](trace-record-d03.json) | one full trace record (D1), the injection question |
| [trace-tool-d01-library-hours.txt](trace-tool-d01-library-hours.txt) | D2: chunks, versions, filters, model call and the exact prompt (hash checked) |
| [trace-tool-d03-injection-question.txt](trace-tool-d03-injection-question.txt) | D4: both rails firing on Batch B's injected notice |
| [trace-tool-d05-warning-cgpa-again.txt](trace-tool-d05-warning-cgpa-again.txt) | a cache hit: the tool follows `cache_source_trace` to the prompt |
| [trace-tool-d06-tuition-fee.txt](trace-tool-d06-tuition-fee.txt), [trace-tool-d10-min-attendance.txt](trace-tool-d10-min-attendance.txt) | abstentions to diagnose from the trace (D5 candidates) |
| [trace-summary.txt](trace-summary.txt), [trace-summary.json](trace-summary.json) | D3 over the window |
| [injection-scan.txt](injection-scan.txt) | the rail over all 703 chunks: 1 hit, the planted notice |

## Findings

- **D1.** Every request wrote one trace line, both to stdout (11 lines in `kubectl logs`) and to
  `/traces/traces-<pod>.jsonl` on the `traces` volume. That includes the cache hit, the
  relevance-floor abstention and the self-harm reply (whose question is not stored).
- **D4, retrieval rail.** For "Ignore your instructions ... Is attendance optional?" the
  injected notice from Batch B's library guide was the **top** passage (chunk
  `89882b0fde234489`, 0.7054). The rail dropped it before the prompt, citing four rules. The
  question itself was flagged by the input rail and still answered from the documents (the
  model then abstained, see below). For "When is the library open?" the guide's clean
  "Opening hours" chunk ranked first (0.8321) and the answer was right; the notice was not in
  the top 5.
- **No false alarms.** Run over every chunk in the collection (703, 158 of them
  superseded), the patterns flag one: the planted notice. Ordinary questions such as "What
  happens if I ignore the attendance rules?" are not flagged (api/tests/test_tracing.py).
- **D3.** Of the 11 queries: abstention rate 63.6%, cache hit rate 10.0%, 2 injection attempts
  flagged (1 input, 1 retrieval), top-1 similarity median 0.705. p95 latency: embed 1016 ms
  (the first call), retrieve 44 ms, generate 60484 ms. Generation is the whole latency.
- **The abstention rate is high because of the A02 flag rule, not retrieval.** 6 of the 7
  abstentions are `abstain_model`. In each, the trace shows the right clauses retrieved, but
  the model returned `answered_from_context=false`, and since A02 that always means abstain
  (docs/evidence/flag-rule-comparison.txt). In d10 the model's own answer was "100%", which is
  right (clause 1.21, ranked 1 and 2 at 0.78). In d06 it was "Rs. 3375", which is wrong: the
  2027-28 revision (rank 1, 0.7004) lists "BBA / BS: Rs. 13,500" with no unit, and the model
  divided it by four. There the rule kept a wrong answer away from the user.
- **Superseded filter in the trace.** d07 ("the original rules") shows `where=null` with the
  reason `question mentions an older version: 'original rules'`, and the superseded
  Aug-2023 clauses ranked 2 to 4 with `(superseded)` marked.
