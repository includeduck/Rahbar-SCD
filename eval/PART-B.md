# Part B: the offline test suite

## Run it

```bash
make eval-image     # build rahbar-eval:dev (the suite, the api package, the golden set) into minikube
make eval-suite     # the Job k8s/eval-job.yaml: three levels, twice, seeds fixed; follows the logs
```

The result files land on the `eval-results` volume. Copy them into `eval/results/`:

```bash
kubectl apply -f k8s/results-shell.yaml && kubectl -n rahbar wait --for=condition=ready pod/results-shell
kubectl -n rahbar exec results-shell -- ls -l /results
kubectl -n rahbar cp results-shell:/results/suite-baseline-<ts>.json eval/results/suite-baseline-<ts>.json
```

(and the `.md` and `-judge-sheet.jsonl` next to it). Outside the cluster:
`python -m eval.suite --api http://localhost:8000` with `CHROMA_HOST`, `OLLAMA_URL` and the
other ConfigMap variables set, or `--levels module,component --runs 1` for a quick retrieval run.

## The two hand tasks

**1. Review the synthetic questions** (at least 8 of the 12, ids `s01` to `s12` in
`golden.jsonl`). For each one, check that:

- the question is clear and something a student would ask;
- the labelled document, section and page really hold the answer (open the trace of a run, or
  the document), and `expect` is the right answer; for an unanswerable one, that the corpus
  really does not answer it.

Then set `"reviewed": true` and add `"reviewed_by": "Wasay"`. If a question is wrong, fix it
(or its label) and note what you changed in `"note"`.

**2. Label answers for the judge** (at least 8). Open
`eval/results/suite-baseline-<ts>-judge-sheet.jsonl`. Each line has a question, the system's
answer, and the passages the model was given. The judge's verdict is deliberately not shown.
For each line decide whether every fact in the answer is stated in (or follows directly from)
the passages: set `"supported": true` or `false` and `"by": "Wasay"`. Save the labelled lines
to `eval/judge_labels.jsonl`, then:

```bash
python -m eval.agreement eval/results/suite-baseline-<ts>.json
```

It prints how often the judge agrees with you (and Cohen's kappa), lists the disagreements, and
updates the result file and its summary table.
