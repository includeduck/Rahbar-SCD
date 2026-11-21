"""One trace record per query (Assignment 03, D1; RAGOps paper 5.1, observability).

Every /api/ask request builds a Trace while it runs. Each stage adds what it did: the cache
lookup, the retriever (collection, filters, chunks with document ids and versions), the
guardrails, the prompt, the model call. When the request ends the trace is written as one
JSON line, twice:

  stdout   {"type": "trace", ...}, next to the request's log lines, so `kubectl logs`
           and any log shipper see it
  a file   TRACE_DIR/traces-<pod>.jsonl on the traces volume, one file per pod (no two
           writers share a file), read by app.trace_tool and app.trace_summary

The full prompt is stored with its SHA-256, so app.trace_tool can print the exact context a
given answer came from. A question that mentions self-harm is not stored: its trace records
only that the support rail answered (the same rule as the logs).

The trace for the current request lives in a context variable, so the retriever and the
providers can add to it without changing their interfaces. Outside a request (unit tests of
a provider, the eval scripts) there is no trace and the calls do nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("rahbar.trace")

trace_var: ContextVar["Trace | None"] = ContextVar("trace", default=None)

_write_lock = threading.Lock()
_digests: dict[str, tuple[float, dict[str, str]]] = {}
_DIGEST_TTL = 300.0


def current() -> "Trace | None":
    return trace_var.get()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_digests(ollama_url: str) -> dict[str, str]:
    """Model name -> short digest from Ollama's /api/tags, cached for five minutes.

    The digest is the model's version: `ollama pull` of the same tag can change the weights,
    and the digest shows it. Empty when Ollama is not reachable (the stub mode).
    """
    now = time.monotonic()
    cached = _digests.get(ollama_url)
    if cached and now - cached[0] < _DIGEST_TTL:
        return cached[1]
    found: dict[str, str] = {}
    try:
        resp = httpx.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=2.0)
        resp.raise_for_status()
        for m in resp.json().get("models", []):
            found[m.get("name", "")] = str(m.get("digest", ""))[:12]
    except Exception as exc:  # the trace says "unknown"; answering must not depend on it
        log.debug("could not read model digests: %s", exc)
    _digests[ollama_url] = (now, found)
    return found


def _digest_for(digests: dict[str, str], model: str) -> str:
    return digests.get(model) or digests.get(f"{model}:latest") or "unknown"


class Trace:
    def __init__(self, request_id: str, question: str, settings: Any) -> None:
        self._t0 = time.perf_counter()
        self._settings = settings
        self.record: dict[str, Any] = {
            "type": "trace",
            "trace_id": request_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                  + f".{int(time.time() * 1000) % 1000:03d}Z",
            "pod": os.environ.get("HOSTNAME", "local"),
            "query": question,
            "rewritten_query": None,     # no query rewriting in RAHBAR; kept for the schema
            "embedding": {"model": settings.embed_model, "version": None},
            "collection": None,
            "filters": None,
            "retrieved": [],
            "reranker": None,            # results are ordered by similarity, no reranker
            "context": [],               # chunk ids that went into the prompt, in order
            "prompt_sha256": None,
            "prompt": None,
            "model": None,
            "response": None,
            "citations": [],
            "guardrails": [],
            "latency_ms": {},
            "cache": "skipped",
            "outcome": None,
        }

    # -- what the stages record -------------------------------------------------------
    def set(self, **fields: Any) -> None:
        self.record.update(fields)

    def stage(self, name: str, ms: float) -> None:
        self.record["latency_ms"][name] = round(self.record["latency_ms"].get(name, 0) + ms, 1)

    def guardrail(self, rail: str, action: str, **detail: Any) -> None:
        self.record["guardrails"].append({"rail": rail, "action": action, **detail})

    def model_call(self, **fields: Any) -> None:
        """One call to the LLM (a provider can call it more than once per request)."""
        model = self.record["model"] or {"calls": []}
        model["calls"].append(fields)
        self.record["model"] = model

    def redact_query(self) -> None:
        self.record["query"] = "[not stored: self-harm support reply]"

    # -- writing ------------------------------------------------------------------------
    def finish(self, outcome: str) -> dict[str, Any]:
        rec = self.record
        rec["outcome"] = outcome
        rec["latency_ms"]["total"] = round((time.perf_counter() - self._t0) * 1000, 1)
        s = self._settings
        if s.llm_provider.startswith("ollama") or s.retriever == "chroma":
            digests = model_digests(s.ollama_url)
            rec["embedding"]["version"] = _digest_for(digests, s.embed_model)
            if rec["model"] is not None and s.llm_provider.startswith("ollama"):
                rec["model"]["version"] = _digest_for(digests, s.llm_model)
        return rec

    def emit(self, outcome: str) -> dict[str, Any]:
        rec = self.finish(outcome)
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with _write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            trace_dir = os.environ.get("TRACE_DIR", "")
            if trace_dir:
                try:
                    path = Path(trace_dir) / f"traces-{rec['pod']}.jsonl"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError as exc:  # a full or missing volume must not fail the answer
                    log.warning("could not write the trace file: %s", exc)
        return rec


def record_model_call(**fields: Any) -> None:
    """Called by the providers after each LLM call; does nothing outside a request."""
    trace = trace_var.get()
    if trace is not None:
        trace.model_call(**fields)
