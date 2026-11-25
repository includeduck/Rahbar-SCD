"""Summarise the trace log over a time window (Assignment 03, D3).

    python -m app.trace_summary                              # everything in $TRACE_DIR
    python -m app.trace_summary --last 1h                    # the last hour (also 30m, 2d)
    python -m app.trace_summary --since 2026-11-21T10:00 --until 2026-11-21T12:00
    python -m app.trace_summary --dir traces/ --json         # machine-readable

Reports what /metrics exposes as counters and histograms, from the traces instead, so a
window can be chosen after the fact and the numbers survive a pod restart: the top-1
retrieval similarity distribution, the abstention rate, the cache hit rate, p50 and p95
latency per stage, and the number of injection attempts flagged per rail. Also the
outcome counts, the collections that served the window, and the guardrail actions.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .trace_tool import read_traces

STAGES = ["embed", "cache", "retrieve", "generate", "total"]
SIM_BUCKETS = [0.0, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 1.01]


def parse_time(text: str) -> datetime:
    text = text.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_last(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+)\s*([smhd])", text.strip())
    if not match:
        raise argparse.ArgumentTypeError("use e.g. 30m, 1h, 2d")
    n, unit = int(match.group(1)), match.group(2)
    unit_name = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
    return timedelta(**{unit_name: n})


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    outcomes = Counter(str(r.get("outcome")) for r in records)
    abstained = sum(v for k, v in outcomes.items() if k.startswith("abstain"))
    hits = sum(r.get("cache") == "hit" for r in records)
    misses = sum(r.get("cache") == "miss" for r in records)

    top1 = [r["retrieved"][0]["score"] for r in records
            if r.get("retrieved") and r.get("cache") != "hit"
            and r["retrieved"][0].get("score") is not None]
    histogram = []
    for lo, hi in zip(SIM_BUCKETS, SIM_BUCKETS[1:]):
        histogram.append({"range": f"[{lo:.2f}, {min(hi, 1.0):.2f}{']' if hi > 1 else ')'}",
                          "count": sum(lo <= s < hi for s in top1)})

    latency: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        vals = [r["latency_ms"][stage] for r in records if stage in (r.get("latency_ms") or {})]
        latency[stage] = {"n": len(vals), "p50_ms": percentile(vals, 50),
                          "p95_ms": percentile(vals, 95)}
    # Answers that reached the model, separately: their total is what a user waits for.
    generated = [r["latency_ms"]["total"] for r in records
                 if "generate" in (r.get("latency_ms") or {})]
    latency["total_when_generated"] = {"n": len(generated),
                                       "p50_ms": percentile(generated, 50),
                                       "p95_ms": percentile(generated, 95)}

    injections: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    for r in records:
        for g in r.get("guardrails", []):
            actions[f"{g.get('rail')}:{g.get('action')}"] += 1
            if g.get("action") in ("flagged", "dropped_passage"):
                injections[str(g.get("rail"))] += 1
    collections = Counter((r.get("collection") or {}).get("name") or "(no retrieval)"
                          for r in records if r.get("cache") != "hit")

    return {
        "window": {"first": min((r["ts"] for r in records), default=None),
                   "last": max((r["ts"] for r in records), default=None)},
        "queries": n,
        "outcomes": dict(outcomes.most_common()),
        "abstention_rate": round(abstained / n, 4) if n else None,
        "cache_hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
        "cache": {"hit": hits, "miss": misses, "skipped": n - hits - misses},
        "top1_similarity": {
            "n": len(top1),
            "min": min(top1, default=None), "p25": percentile(top1, 25),
            "p50": percentile(top1, 50), "p75": percentile(top1, 75),
            "max": max(top1, default=None),
            "histogram": histogram,
        },
        "latency_ms": latency,
        "injection_attempts_flagged": {"total": sum(injections.values()), **injections},
        "guardrail_actions": dict(actions.most_common()),
        "collections": dict(collections.most_common()),
    }


def _num(v: Any, fmt: str = "{:.0f}") -> str:
    return "-" if v is None else fmt.format(v)


def render(s: dict[str, Any], label: str) -> str:
    out = [f"trace summary, {label}",
           f"  {s['queries']} queries, first {s['window']['first']}, last {s['window']['last']}",
           ""]
    out.append("outcomes:  " + ", ".join(f"{k} {v}" for k, v in s["outcomes"].items()))
    out.append(f"abstention rate   {_num(s['abstention_rate'], '{:.1%}')}  "
               "(abstain_floor + abstain_model, over all queries)")
    c = s["cache"]
    out.append(f"cache hit rate    {_num(s['cache_hit_rate'], '{:.1%}')}  "
               f"({c['hit']} hit, {c['miss']} miss, {c['skipped']} not looked up)")
    flagged = s["injection_attempts_flagged"]
    out.append(f"injection attempts flagged  {flagged['total']}  "
               + ", ".join(f"{k} rail {v}" for k, v in flagged.items() if k != "total"))
    t = s["top1_similarity"]
    out.append("")
    out.append(f"top-1 similarity (n={t['n']}, cache hits excluded): min {_num(t['min'], '{:.3f}')}"
               f"  p25 {_num(t['p25'], '{:.3f}')}  median {_num(t['p50'], '{:.3f}')}"
               f"  p75 {_num(t['p75'], '{:.3f}')}  max {_num(t['max'], '{:.3f}')}")
    peak = max((b["count"] for b in t["histogram"]), default=0) or 1
    for b in t["histogram"]:
        out.append(f"  {b['range']:<14} {b['count']:>4} {'#' * round(30 * b['count'] / peak)}")
    out.append("")
    out.append(f"latency per stage    {'n':>5} {'p50 ms':>9} {'p95 ms':>9}")
    for stage, v in s["latency_ms"].items():
        out.append(f"  {stage:<20} {v['n']:>5} {_num(v['p50_ms']):>9} {_num(v['p95_ms']):>9}")
    out.append("")
    out.append("guardrail actions: " + (", ".join(f"{k} {v}" for k, v in
                                                 s["guardrail_actions"].items()) or "none"))
    out.append("collections served: " + ", ".join(f"{k} {v}" for k, v in
                                                  s["collections"].items()))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise the trace log over a window.")
    parser.add_argument("--dir", default=os.environ.get("TRACE_DIR") or "/traces")
    parser.add_argument("--since", type=parse_time)
    parser.add_argument("--until", type=parse_time)
    parser.add_argument("--last", type=parse_last, help="e.g. 30m, 1h, 2d (ends now)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    since, until = args.since, args.until
    if args.last:
        until = datetime.now(timezone.utc)
        since = until - args.last
    records = []
    for r in read_traces(args.dir):
        ts = parse_time(r["ts"])
        if (since and ts < since) or (until and ts >= until):
            continue
        records.append(r)
    label = (f"{since.isoformat() if since else 'start'} to "
             f"{until.isoformat() if until else 'now'}")
    summary = summarise(records)
    summary["window"]["requested"] = label
    if args.json:
        print(json.dumps(summary, indent=1))
    else:
        print(render(summary, label))
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
