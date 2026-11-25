"""Print the exact context and prompt behind one answer (Assignment 03, D2).

    python -m app.trace_tool <trace id>                    # reads $TRACE_DIR (default /traces)
    python -m app.trace_tool <trace id> --dir traces/      # a folder of traces-*.jsonl files
    python -m app.trace_tool <trace id> --prompt-only      # just the prompt, byte for byte
    python -m app.trace_tool --list [--last 20]            # recent trace ids with their outcome

The trace id is the request id: the X-Request-ID response header, `request_id` in the API's
JSON response and in every log line of that request. On the cluster:

    kubectl -n rahbar exec deploy/api -- python -m app.trace_tool <trace id>

The prompt printed is the one stored in the trace; its SHA-256 is checked against the
recorded hash, so an edited trace file shows up. An answer served from the cache has no
prompt of its own: the tool follows `cache_source_trace` to the request that produced it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def read_traces(folder: str) -> Iterator[dict[str, Any]]:
    """Every trace record in every traces-*.jsonl file under the folder, oldest file first."""
    root = Path(folder)
    files = [root] if root.is_file() else sorted(root.glob("**/traces-*.jsonl"))
    for path in files:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue      # a line cut short by a crash; skip it
                if rec.get("type") == "trace":
                    yield rec


def find(folder: str, trace_id: str) -> dict[str, Any] | None:
    found = None
    for rec in read_traces(folder):
        if rec.get("trace_id") == trace_id:
            found = rec       # the last one wins if a client reused an id
    return found


def _fmt_ms(lat: dict[str, float]) -> str:
    order = ["embed", "cache", "retrieve", "generate", "total"]
    keys = [k for k in order if k in lat] + [k for k in lat if k not in order]
    return ", ".join(f"{k} {lat[k]:.0f} ms" for k in keys)


def show(rec: dict[str, Any], folder: str, out: Any = None) -> int:
    out = out or sys.stdout

    def p(text: str = "") -> None:
        print(text, file=out)

    p(f"trace     {rec['trace_id']}")
    p(f"time      {rec.get('ts')}   pod {rec.get('pod')}")
    p(f"query     {rec.get('query')}")
    if rec.get("rewritten_query"):
        p(f"rewritten {rec['rewritten_query']}")
    p(f"outcome   {rec.get('outcome')}   cache {rec.get('cache')}"
      + (f" (generation {rec['cache_generation']})" if rec.get("cache_generation") else ""))
    emb = rec.get("embedding") or {}
    p(f"embedding {emb.get('model')} @ {emb.get('version')}")
    col = rec.get("collection") or {}
    if col:
        p(f"collection {col.get('name')} ({col.get('chunks')} chunks, "
          f"embed_model {col.get('embed_model')}, dim {col.get('dimension')})")
    flt = rec.get("filters") or {}
    if flt:
        p(f"filters   where={json.dumps(flt.get('where'))} k={flt.get('k')} "
          f"n_results={flt.get('n_results')}  ({flt.get('why')})")
    p(f"latency   {_fmt_ms(rec.get('latency_ms') or {})}")

    dropped = {g.get("chunk_id") for g in rec.get("guardrails", [])
               if g.get("action") == "dropped_passage"}
    context = rec.get("context") or []
    p("")
    if rec.get("cache") == "hit":
        p(f"served from the cache: collection, filters, document versions and the prompt are "
          f"in trace {rec.get('cache_source_trace')}")
    p(f"retrieved ({rec.get('reranker') or 'ordered by similarity'}):")
    p(f"  {'#':>2} {'score':>6}  {'in prompt':<10} {'chunk':<18} {'document @ version':<52} section")
    for r in rec.get("retrieved", []):
        cid = r.get("chunk_id", "")
        where = (f"#{context.index(cid) + 1}" if cid in context
                 else "DROPPED" if cid in dropped else "-")
        doc = (f"{r.get('doc_id') or r.get('source')} @ {r.get('doc_version')}"
               if r.get("doc_version") is not None else str(r.get("source")))
        if r.get("superseded"):
            doc += " (superseded)"
        p(f"  {r.get('rank', ''):>2} {r.get('score', 0):>6.4f}  {where:<10} {cid[:18]:<18} "
          f"{doc[:52]:<52} {str(r.get('section'))[:50]}")

    p("")
    p("guardrails:")
    if not rec.get("guardrails"):
        p("  (none fired)")
    for g in rec.get("guardrails", []):
        detail = {k: v for k, v in g.items() if k not in ("rail", "action")}
        p(f"  {g.get('rail'):<9} {g.get('action'):<20} {json.dumps(detail, ensure_ascii=False)}")

    model = rec.get("model") or {}
    if model:
        p("")
        p(f"model     version {model.get('version', 'unknown')}")
        for c in model.get("calls", []):
            p(f"  call {c.get('attempt')}: {c.get('provider')} {c.get('model')} "
              f"params={json.dumps(c.get('params'))} tokens {c.get('tokens_in')} in / "
              f"{c.get('tokens_out')} out, {c.get('ms')} ms, valid={c.get('valid')}"
              + (f", error {c['error']}" if c.get("error") else ""))
            if c.get("raw_output"):
                p(f"    raw output: {c['raw_output']}")

    p("")
    resp = rec.get("response") or {}
    p(f"response  {resp.get('answer')}")
    p(f"          answered_from_context={resp.get('answered_from_context')} "
      f"confidence={resp.get('confidence')}")
    for c in rec.get("citations", []):
        p(f"  cites   {c.get('source')} / {c.get('section')} / page {c.get('page')}")

    p("")
    status = print_prompt(rec, folder, out)
    return status


def print_prompt(rec: dict[str, Any], folder: str, out: Any = None,
                 header: bool = True) -> int:
    out = out or sys.stdout
    prompt = rec.get("prompt")
    if prompt is None and rec.get("cache") == "hit" and rec.get("cache_source_trace"):
        source = find(folder, rec["cache_source_trace"])
        if source is None:
            print(f"served from the cache; the source trace {rec['cache_source_trace']} is not "
                  f"in {folder}", file=out)
            return 1
        if header:
            print(f"served from the cache: the prompt below is from trace "
                  f"{source['trace_id']} ({source.get('ts')})", file=out)
        rec, prompt = source, source.get("prompt")
    if prompt is None:
        print(f"no prompt: the request ended before generation ({rec.get('outcome')})",
              file=out)
        return 0
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    ok = digest == rec.get("prompt_sha256")
    if header:
        print(f"prompt sha256 {digest} "
              f"({'matches the trace' if ok else 'DOES NOT MATCH ' + str(rec.get('prompt_sha256'))})",
              file=out)
        print("context chunks, in prompt order: " + ", ".join(rec.get("context") or []),
              file=out)
        print("=" * 30 + " exact prompt " + "=" * 30, file=out)
    print(prompt, file=out)
    if header:
        print("=" * 74, file=out)
    return 0 if ok else 2


def list_recent(folder: str, last: int) -> int:
    recs = sorted(read_traces(folder), key=lambda r: r.get("ts", ""))[-last:]
    for r in recs:
        top = (r.get("retrieved") or [{}])[0].get("score")
        print(f"{r.get('ts')}  {str(r.get('trace_id')):<32} {str(r.get('outcome')):<15} "
              f"top1 {f'{top:.4f}' if top is not None else '-':<6}  {str(r.get('query'))[:60]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the context and prompt of one answer.")
    parser.add_argument("trace_id", nargs="?")
    parser.add_argument("--dir", default=os.environ.get("TRACE_DIR") or "/traces",
                        help="folder (or one file) of traces-*.jsonl")
    parser.add_argument("--prompt-only", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the raw trace record")
    parser.add_argument("--list", action="store_true", help="list recent traces")
    parser.add_argument("--last", type=int, default=20)
    args = parser.parse_args(argv)
    if args.list:
        return list_recent(args.dir, args.last)
    if not args.trace_id:
        parser.error("give a trace id, or --list")
    rec = find(args.dir, args.trace_id)
    if rec is None:
        print(f"trace {args.trace_id} not found under {args.dir}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rec, indent=1, ensure_ascii=False))
        return 0
    if args.prompt_only:
        return print_prompt(rec, args.dir, header=False)
    return show(rec, args.dir)


if __name__ == "__main__":
    sys.exit(main())
