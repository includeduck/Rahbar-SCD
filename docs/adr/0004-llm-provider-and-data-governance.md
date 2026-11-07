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
   - **Timeout and retry:** Calls enforce a 60-second HTTP timeout with one automatic retry before invoking a safe fallback answer (`_safe_fallback()`), guaranteeing that bad JSON or timeouts never surface as HTTP 500.

2. **Hosted Provider Option · `HostedProvider`:**
   - Implemented against OpenAI-compatible endpoints (`/chat/completions`) using JSON mode (`response_format: {"type": "json_object"}`).
   - **Credential isolation:** `LLM_API_KEY` is loaded strictly from a Kubernetes Secret (`rahbar-secret`) created via CLI (`make secret`). The key is marked `repr=False` in Pydantic `Settings` and is never logged or baked into images.
   - **Data governance boundaries:** Only public FAST-NUCES document excerpts and the user question are sent; no persistent student IDs or session history are forwarded.

3. **Offline Stub Provider · `StubProvider`:**
   - Deterministic, zero-network provider used for unit testing and CI builds. Guarantees CI is 100% hermetic and never depends on third-party availability.

## Consequences

- **What this makes easier:**
  - Complete data privacy: The default setup keeps all data local; zero credentials are required to run RAHBAR.
  - Zero 500 errors on bad generation: Token-constrained decoding plus Pydantic validation and safe fallback ensures robust error handling.
  - Test hermeticity: Offline tests run in < 2 seconds without external API keys or Ollama processes.
- **What this makes harder:**
  - Running 3B parameter models on laptop CPUs results in 15–40 s generation times per query. This is mitigated by our Redis semantic cache (ADR 0003), which delivers sub-second responses for repeated queries.
- **What we watch for:**
  - If larger models (e.g. 8B or hosted APIs) are introduced, rate limits and token cost counters (`TOKENS_IN`, `TOKENS_OUT`) must be monitored in Prometheus `/metrics`.
