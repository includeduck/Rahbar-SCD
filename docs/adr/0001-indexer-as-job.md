# ADR 0001: The indexer runs as a Job, not inside the API

## Status
Accepted · 3 November 2026

## Context

RAHBAR does two very different things.

The first is building the index. We read nine PDFs and two web pages, OCR the scanned ones,
clean the text, chunk it, embed every chunk and write it all into Chroma. That is slow and
it eats memory. It only needs to happen once, and again when the documents change.

The second is answering questions. That happens on every request and has to be fast.

The obvious shortcut is to build the index when the API starts. We don't do that, and here
is why. Our API runs 3 replicas (`k8s/base/api.yaml`, line 12) and there is one Chroma
volume. If indexing lived in API startup:

1. **Three pods would write to one index at the same time.** Each one is halfway through a
   different document. What ends up in Chroma is an index that no single run produced.
2. **Every restart would re-index.** A rolling update, an OOMKill, a node drain, even one
   failed probe: each one pays the full OCR and embedding cost again.
3. **Readiness would lie.** Either the pod says "ready" before there is anything to
   retrieve and serves empty answers, or it takes minutes to get ready and the startup probe
   (30 tries × 2 s in `api.yaml`) kills it halfway. A rolling update would then swap three
   working pods for three that are still chunking PDFs.
4. **We couldn't re-index without a redeploy.** Changing the documents would mean
   restarting the API, which is backwards.

## Decision

Indexing and serving are separate. The indexer writes, the API only reads.

- **The indexer is its own image and its own Job** (`k8s/base/indexer-job.yaml`):
  `restartPolicy: OnFailure`, `backoffLimit: 4`, `activeDeadlineSeconds: 3600`. It runs the
  five stages once and exits. An initContainer waits for Chroma's heartbeat first, so on a
  fresh cluster the Job doesn't burn its retries while Chroma is still starting.
- **A weekly CronJob runs the same image with the same arguments**
  (`k8s/base/reindex-cronjob.yaml`, Sunday 03:00 PKT, `concurrencyPolicy: Forbid` so two
  indexers never overlap). Re-indexing by hand is one command, with no redeploy:

  ```
  kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar
  ```

- **The API never writes.** Its startup (`api/app/main.py`, `lifespan`) only creates
  clients and starts a background check of the embedding model. The NetworkPolicy lets only
  the api and the indexer reach Chroma.
- **Readiness tells the truth.** `/readyz` (`api/app/services/readiness.py`,
  `check_chroma`) returns 200 only when the collection exists, is not empty, and was built
  with the same `EMBED_MODEL` the API uses. Until the Job has finished, the API pods stay out
  of the Service. That's on purpose.
- **Re-indexing never empties the index.** `indexer/pipeline/store.py` writes into a new
  collection `rahbar-g<timestamp>`, checks it, then flips a small pointer collection
  `rahbar-active` to it and keeps one old generation for rollback. The API follows the
  pointer, so the switch happens in one step. Cache keys carry the generation, so answers
  from the old index stop being served.

## Options considered

- **Index in API startup.** Rejected for the four reasons above.
- **An initContainer on the API pods.** Same problems: it runs once per replica and again on
  every restart.
- **Let one API replica do the indexing (leader election).** More moving parts, and serving
  and indexing are still tied together, so a slow index still holds up a restart.
- **Bake the index into the image.** Every document change becomes a rebuild and a
  redeploy, and the image gets big. It's the "config change needs a rebuild" problem from
  Lecture 05.
- **Delete the collection and rebuild it inside the Job.** Simpler than the pointer, but
  for the whole run the collection is missing or half-full. `/readyz` would fail on all three
  API pods at once, which means an outage on every re-index. That's why we use the pointer.

## Consequences

- **Good:** the expensive part runs once per document change, not once per pod restart. API
  pods restart in seconds.
- **Good:** re-indexing is one command, with no redeploy and no downtime.
- **Good:** a failed Job leaves the previous index serving. Nothing breaks for users.
- **Cost:** more objects to manage (Job, CronJob, pointer collection), and during a switch two
  generations exist side by side, so Chroma briefly needs double the space.
- **Cost:** a Job's pod template can't be changed in place. To re-run it we create new Jobs
  from the CronJob instead of re-applying the old one.
- **Watch for:** the indexer needs far more memory than the API (OCR and embedding). If its
  limit is too low it gets OOMKilled with exit code 137. We size it from `kubectl top pods`
  during a real run.
- **Watch for:** a failed Job is quiet, because the old index keeps serving. After a
  re-index, check `kubectl get jobs -n rahbar` and the collection count in `/readyz`.
