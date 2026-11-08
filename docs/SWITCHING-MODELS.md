# Switching the generation model

RAHBAR uses two models, and only one of them can be switched freely:

| Model | Setting | Switch freely? |
| --- | --- | --- |
| **Generation** (writes the answer) | `LLM_PROVIDER`, `LLM_MODEL` in `k8s/base/configmap.yaml` | Yes. Restart the api; nothing else changes. |
| **Embedding** (turns text into vectors) | `EMBED_MODEL` | **No.** The index is built with it. Changing it means re-indexing, and until then the api refuses to become ready (the embedding pin, checked in `api/app/services/readiness.py`). |

This guide covers generation only. Every switch has the same three steps: change the setting,
restart the api (it reads its configuration at start), and check that the change took effect.

The shipped default is `llama3.2:3b` on host Ollama (`LLM_PROVIDER=ollama-host`). It needs no
key, and no question leaves the machine.

All commands run from the repository root against the cluster where RAHBAR is deployed. Check
which one that is first:

```bash
kubectl config current-context
```

On Windows, `make` works only inside WSL 2; every step below gives the plain command as well.

## 1. Switch to a hosted model on OpenRouter

Tested on 8 Nov 2026 with `dots-studio/dots-3-note-preview:free`; results in
[`docs/evidence/hosted-model-comparison.txt`](evidence/hosted-model-comparison.txt).

**1.1 Get a key.** Sign in at openrouter.ai, open **Keys**, choose **Create Key**. If it offers a
credit limit, set it to 0: the free models then cannot cost anything. Never paste the key into
a file, a chat or a commit.

**1.2 Store it as the `rahbar-secret` Secret.** In Git Bash or a Linux shell, `read -rs` keeps the
key off the screen and out of your shell history:

```bash
read -rs LLM_API_KEY && export LLM_API_KEY
```

Paste the key and press Enter (nothing is shown). Then either `make secret` (WSL or Linux), or
the command it runs:

```bash
kubectl create secret generic rahbar-secret -n rahbar --from-literal=LLM_API_KEY="$LLM_API_KEY" --dry-run=client -o yaml | kubectl apply -f -
```

```bash
unset LLM_API_KEY
```

**1.3 Apply the hosted overlay and restart the api.** The overlay
([`k8s/overlays/hosted`](../k8s/overlays/hosted/kustomization.yaml)) is the dev overlay plus three
settings (`LLM_PROVIDER=hosted`, `LLM_MODEL`, `HOSTED_BASE_URL=https://openrouter.ai/api/v1`) and
a NetworkPolicy that lets the api make HTTPS calls to public addresses. The default overlays give
the api no internet access at all, so without that policy every hosted call times out.

```bash
kubectl apply -k k8s/overlays/hosted
```

```bash
kubectl -n rahbar rollout restart deploy/api && kubectl -n rahbar rollout status deploy/api --timeout=5m
```

**1.4 Check it.** The pod should report the new settings and a key (this prints only whether a
key is set, never the key):

```bash
kubectl -n rahbar exec deploy/api -- sh -c 'echo provider=$LLM_PROVIDER model=$LLM_MODEL key_set=$([ -n "$LLM_API_KEY" ] && echo yes || echo no)'
```

Then ask a question (`make ask Q="..."`, or the chat page). The UI shows `provider: hosted`; the
JSON response has `"provider": "hosted"`. Clear the cache first if you want to compare models,
since a cached answer comes from whichever model wrote it:

```bash
kubectl -n rahbar exec deploy/redis -- redis-cli FLUSHALL
```

### Using a different OpenRouter model

Edit `LLM_MODEL` in [`k8s/overlays/hosted/kustomization.yaml`](../k8s/overlays/hosted/kustomization.yaml),
then repeat 1.3 and 1.4. The model must support `response_format` (JSON mode): the provider asks
for a JSON object and falls back if it gets anything else. To check a model id and its
parameters before switching:

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c "import json,sys; [print(m['id'], m.get('supported_parameters')) for m in json.load(sys.stdin)['data'] if 'MODEL-ID' in m['id']]"
```

Another OpenAI-compatible service (Groq, Google AI Studio) works the same way: change
`HOSTED_BASE_URL` and `LLM_MODEL` in the overlay and use that service's key.

## 2. Switch back to the default (llama3.2:3b on Ollama)

`kubectl apply` adds and updates objects but does not delete ones the dev overlay does not list,
so remove the hosted egress policy by hand:

```bash
kubectl apply -k k8s/overlays/dev
```

```bash
kubectl -n rahbar delete networkpolicy allow-api-hosted-llm-egress
```

```bash
kubectl -n rahbar rollout restart deploy/api && kubectl -n rahbar rollout status deploy/api --timeout=5m
```

Check with the same command as 1.4: it should print `provider=ollama-host model=llama3.2:3b`. The
Secret can stay (the api ignores it in Ollama mode), or remove it:

```bash
kubectl -n rahbar delete secret rahbar-secret
```

If you will not use the key again, revoke it under **Keys** on openrouter.ai.

## 3. Switch to another Ollama model

The same mechanism with `LLM_MODEL` only. This path uses no key and keeps everything local, but
it was not measured: a larger model on a laptop CPU is much slower, and `LLM_TIMEOUT_SECONDS`
(120 s per attempt) may need raising.

```bash
ollama pull llama3.1:8b
```

Then change `LLM_MODEL` in `k8s/base/configmap.yaml` (the lasting way, which `kubectl apply -k`
keeps), or patch the running ConfigMap for a quick try (the next `kubectl apply -k` puts the file's
value back):

```bash
kubectl -n rahbar patch configmap rahbar-config --type merge -p '{"data":{"LLM_MODEL":"llama3.1:8b"}}'
```

Restart and check as in 1.3 and 1.4. Do not change `EMBED_MODEL` this way (see the table above).

## 4. Measure a model before you trust it

Both scripts run against whatever model the cluster is using, on the same index and retrieval:

- `python eval/run_eval.py --api http://<ingress> --host rahbar.local --out <file>`: the gate
  metrics (recall@k, citation validity, abstention). Retrieval decides most of these, so they
  barely move between models.
- [`eval/compare_flag_rules.py`](../eval/compare_flag_rules.py): one generation per golden
  question with the answer text, so you can see which questions the model answers and whether
  the answers match the golden `expect` values. Run instructions are in the script.

On the free OpenRouter tier one eval run plus one comparison is about 40 requests; check your
account's daily limit first.

## When it goes wrong

Every failure below ends in the safe fallback answer ("I was unable to generate a reliable
answer..."), never a 500. The cause is in the api log. `deploy/api` picks one of the three pods,
so if the line is missing, run it again or name a pod from `kubectl -n rahbar get pods`:

```bash
kubectl -n rahbar logs deploy/api --since=10m | grep rahbar.provider
```

| Log shows | Cause | Fix |
| --- | --- | --- |
| `HOSTED_BASE_URL is not configured` | Hosted provider without the overlay | Apply `k8s/overlays/hosted` (1.3) |
| `ConnectTimeout` or `ConnectError` to openrouter.ai | Egress policy missing | Apply the hosted overlay; check `kubectl -n rahbar get networkpolicy allow-api-hosted-llm-egress` |
| `401 Unauthorized` | No key, or a wrong or revoked key | Redo 1.2, then restart the api: it reads the Secret only at start |
| `429 Too Many Requests` | Free-tier rate limit | Wait, or switch back (section 2) |
| `404` / "No endpoints found matching your data policy" | OpenRouter privacy settings block free endpoints that may log prompts | Allow them under **Settings → Privacy**, if you accept that, or pick another model |
| `400 Bad Request` | The provider rejected the request; the body is not logged | Seen once on 8 Nov on an injection question; try another model if it repeats |
| `ValidationError` / `JSONDecodeError` | The model did not return the JSON schema | Use a model that supports `response_format` |

## What leaves the machine in hosted mode

Each question that passes the self-harm check (`api/app/services/safety.py`) and the relevance
floor is sent with its top three document excerpts to OpenRouter and the provider it routes to.
No student ID or chat history is sent. The free endpoint we measured publishes no data policy, so
treat everything sent as possibly logged. See [ADR 0004](adr/0004-llm-provider-and-data-governance.md).
