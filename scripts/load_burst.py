"""A short concurrent burst against the api, to measure its CPU and memory under load
(Assignment 03, F5). Standard library only, so it runs inside any pod the NetworkPolicy lets
reach the api (the eval helper pod):

    kubectl -n rahbar exec -i results-shell -- python - --clients 12 --seconds 180 < scripts/load_burst.py

Each client asks a few fixed questions in turn. After the first round they are semantic-cache
hits, so the burst loads the api itself (HTTP, embedding call, cache, JSON), not the model;
the uncached first round shows the model-bound path. Prints throughput and latency.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.request

QUESTIONS = [
    "What CGPA does an undergraduate need to stay off academic warning?",
    "How much absence can the Head of Department condone?",
    "How early must students reach the examination hall before a paper starts?",
    "Until which week of a regular semester can I withdraw from a course?",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://api:8000")
    ap.add_argument("--clients", type=int, default=12)
    ap.add_argument("--seconds", type=float, default=180)
    args = ap.parse_args()

    lat: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop = time.time() + args.seconds

    def client(n: int) -> None:
        i = n
        while time.time() < stop:
            q = QUESTIONS[i % len(QUESTIONS)]
            i += 1
            req = urllib.request.Request(
                f"{args.api}/api/ask", data=json.dumps({"question": q}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Request-ID": f"load-{n}-{i}"}, method="POST")
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    resp.read()
                with lock:
                    lat.append(time.perf_counter() - t0)
            except Exception as exc:  # noqa: BLE001 - count every failure
                with lock:
                    errors.append(type(exc).__name__)

    threads = [threading.Thread(target=client, args=(n,)) for n in range(args.clients)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.time() - t0
    lat.sort()
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0.0
    print(f"{args.clients} clients, {took:.0f} s: {len(lat)} answers ({len(lat) / took:.1f}/s), "
          f"{len(errors)} errors; latency p50 {statistics.median(lat) if lat else 0:.2f} s, "
          f"p95 {p95:.2f} s, max {lat[-1] if lat else 0:.2f} s")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
