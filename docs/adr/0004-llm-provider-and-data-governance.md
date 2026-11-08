# ADR 0004: LLM provider selection, constrained decoding, and data governance

## Status
Accepted · 7 November 2026

## Context

RAHBAR requires a language model to synthesize grounded answers from retrieved passages. The design must address three operational and regulatory realities:

1. **Student data privacy & air-gapped evaluation:** University regulations and student inquiries may contain sensitive contextual queries. Leaking queries or proprietary institutional documents to third-party hosted APIs introduces compliance and credential-leakage risks (e.g. accidental API key commits).
2. **Deterministic, structured output contracts:** The user interface (`ui/index.html`) and evaluation runner (`eval/run_eval.py`) expect a strictly typed `Answer` JSON object (`reasoning`, `answer`, `citations`, `answered_from_context`, `confidence`). Unconstrained model decoding frequently outputs conversational preambles or malformed JSON that surfaces as HTTP 500 errors.
3. **Reproducibility across environments:** Offline development, unit testing, continuous integration in GitHub Actions, and production deployments must run reliably without depending on paid accounts or third-party cloud availability.

## Decision

We implement a provider abstraction (`api/app/providers/llm/base.py`) with three concrete implementations:

1. **Primary Provider · `OllamaProvider` (`ollama-host` / `ollama-incluster`):**
   - The shipped default is `llama3.2:3b` served via Ollama (`http://host.minikube.internal:11434` or in-cluster).
   - **Local execution:** All generation executes on host/cluster hardware. Zero bytes of query text, embeddings, or institutional documents ever leave the cluster.
   - **Constrained decoding (`format=`):** We supply `Answer.model_json_schema()` directly to Ollama's `/api/chat` `format` parameter. The sampler is constrained at the token-level to emit valid JSON conforming to the Pydantic schema.
   - **Temperature:** Pinned to `0.1` in `k8s/base/configmap.yaml` ("facts and JSON; 0 does not fix absent knowledge").
   - **Timeout and retry:** Each generation attempt has a timeout of `LLM_TIMEOUT_SECONDS` (120 s in the ConfigMap; it was 60 s until we measured that our CPU needs over 60 s for a full-size prompt, see ENGINEERING-NOTES Q7). One retry follows, and then a safe fallback answer (`_safe_fallback()`), so bad JSON or a timeout never surfaces as an HTTP 500.

2. **Hosted Provider Option · `HostedProvider`:**
   - Implemented against OpenAI-compatible endpoints (`/chat/completions`) using JSON mode (`response_format: {"type": "json_object"}`).
   - **Credential isolation:** `LLM_API_KEY` is loaded strictly from a Kubernetes Secret (`rahbar-secret`) created via CLI (`make secret`). The key is marked `repr=False` in Pydantic `Settings` and is never logged or baked into images.
   - **Data governance boundaries:** Only public FAST-NUCES document excerpts (the top `MAX_CONTEXT_PASSAGES`, 3) and the user question are sent; no persistent student IDs or session history are forwarded. Questions that trip the self-harm check (`api/app/services/safety.py`) or fall below `RELEVANCE_FLOOR` never reach any model. The free OpenRouter endpoint we measured (provider AtlasCloud) publishes no data policy, so we treat everything sent there as possibly logged.
   - **Network access (corrected 8 Nov):** the base NetworkPolicy gives the api only Chroma, Redis and DNS, so until 8 Nov this mode could not work in the cluster: every hosted call would have timed out into the safe fallback. The opt-in overlay [`k8s/overlays/hosted`](../../k8s/overlays/hosted/kustomization.yaml) adds HTTPS egress to public addresses (private ranges excluded) and points the provider at OpenRouter. The default overlays still give the api no internet access.
   - **Measured (8 Nov):** `dots-studio/dots-3-note-preview:free` (16B active of 280B parameters) on the same retrieval as llama3.2:3b ([`docs/evidence/hosted-model-comparison.txt`](../evidence/hosted-model-comparison.txt)). Gate metrics are identical, since retrieval decides them. It answered 8 of 16 answerable questions against llama3.2:3b's 9, but never contradicted itself (llama3.2:3b did twice, both wrong) and took a median 14.6 s against 22.7 s. The questions it declined are mostly ones whose clause is not in the 3 passages it sees, so retrieval, not the model, is the next lever. We keep Ollama as the default: no key, no data leaves the machine, and a free preview model can be rate-limited or withdrawn.

3. **Offline Stub Provider · `StubProvider`:**
   - Deterministic, zero-network provider used by the unit tests and by the `e2e-eval` CI job, where the gate measures retrieval, citations and abstention rather than prose. No CI job needs an API key or a GPU. (`e2e-eval` does download `nomic-embed-text` into its in-cluster Ollama for real embeddings.)

## Consequences

- **What this makes easier:**
  - Complete data privacy: The default setup keeps all data local; zero credentials are required to run RAHBAR.
  - Zero 500 errors on bad generation: Token-constrained decoding plus Pydantic validation and safe fallback ensures robust error handling.
  - Test hermeticity: the api, indexer and eval unit tests run without API keys, Ollama or a cluster (the api suite takes about 30 s on our laptop).
- **What this makes harder:**
  - A 3B model on a laptop CPU is slow: on the live evaluation, questions took 11 to 58 s end to end (median 25 s; `eval/results.json`), and reading the prompt is the bottleneck at about 33 tokens/s. The semantic cache (ADR 0003) answers exact repeats without the LLM, but at the measured threshold of 0.97 it catches near-identical wording only.
- **What we watch for:**
  - If larger models (e.g. 8B or hosted APIs) are introduced, rate limits and token cost counters (`TOKENS_IN`, `TOKENS_OUT`) must be monitored in Prometheus `/metrics`.
  - In hosted mode, HTTP errors from the provider: on the measured run OpenRouter returned 400 once (q16, an injection question) and the response body is not logged, so the cause is unknown. Logging the error body (without the key) would be the first step.
