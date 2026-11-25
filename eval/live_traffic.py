"""Play the live query set (eval/live_queries.jsonl) against the api as users would (Assignment
03, E1): no cache bypass, one request at a time, request ids `live-<id>` so the traces are
easy to find. Every request leaves a trace on the traces volume; that log is what
eval/coverage.py reads.

    python -m eval.live_traffic --api http://api:8000           # Job k8s/live-traffic-job.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", default=os.environ.get("EVAL_API", "http://localhost:8000"))
    ap.add_argument("--queries", type=Path, default=HERE / "live_queries.jsonl")
    ap.add_argument("--prefix", default="live-")
    ap.add_argument("--timeout", type=float, default=300)
    args = ap.parse_args(argv)

    queries = [json.loads(line) for line in args.queries.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    failures = 0
    with httpx.Client(base_url=args.api.rstrip("/"), timeout=args.timeout) as client:
        for q in queries:
            rid = f"{args.prefix}{q['id']}"
            t0 = time.perf_counter()
            try:
                resp = client.post("/api/ask", json={"question": q["query"]},
                                   headers={"X-Request-ID": rid})
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                print(f"{rid:<10} ERROR {exc}", flush=True)
                failures += 1
                continue
            state = "answered" if body.get("answered_from_context") else "not answered"
            print(f"{rid:<10} {q.get('domain', '?'):<4} {state:<13} "
                  f"cache={resp.headers.get('X-Cache', '?'):<4} "
                  f"{time.perf_counter() - t0:6.1f}s  {body.get('answer', '')[:60]!r}",
                  flush=True)
    print(f"{len(queries) - failures} of {len(queries)} queries sent")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
