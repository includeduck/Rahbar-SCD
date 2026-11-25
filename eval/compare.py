"""Baseline against candidate at all three levels (Assignment 03, C1).

    python -m eval.compare <baseline.json> <candidate.json> [--out comparison.md]

Every metric of the suite's summary table, averaged over each result's runs (latency p95: the
worst run, as the gate counts it), with the change and the questions whose outcome moved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from eval.gate import golden_fingerprint, metric
from eval.suite import SUMMARY_ROWS, fmt


def _per_question(result: dict[str, Any], level: str) -> dict[str, dict[str, Any]]:
    run = result["runs"][0].get(level) or {}
    return {r["id"]: r for r in run.get("per_question", [])}


def compare(base: dict[str, Any], cand: dict[str, Any]) -> str:
    def name(r: dict[str, Any]) -> str:
        m = r["meta"]
        return f"{m['label']} ({m['collection']}, {m['runs']} run{'s' if m['runs'] > 1 else ''})"

    lines = [f"Baseline: {name(base)}  \nCandidate: {name(cand)}  ",
             f"Same golden set: {golden_fingerprint(base) == golden_fingerprint(cand)}", "",
             "| level | metric | baseline | candidate | change |", "|---|---|---|---|---|"]
    for level, key, label in SUMMARY_ROWS:
        how = "max" if key == "latency_p95_s" else "mean"
        b, c = metric(base, f"{level}.{key}", how), metric(cand, f"{level}.{key}", how)
        if b is None and c is None:
            continue
        change = None if b is None or c is None else round(c - b, 4)
        lines.append(f"| {level} | {label} | {fmt(b)} | {fmt(c)} | {fmt(change)} |")

    moved = []
    bc, cc = _per_question(base, "component"), _per_question(cand, "component")
    for qid in bc:
        if qid in cc and bc[qid].get("recall@5") != cc[qid].get("recall@5"):
            moved.append(f"{qid} recall@5 {fmt(bc[qid]['recall@5'])} -> "
                         f"{fmt(cc[qid]['recall@5'])}")
    be, ce = _per_question(base, "e2e"), _per_question(cand, "e2e")
    for qid in be:
        if qid in ce and be[qid]["answered"] != ce[qid]["answered"]:
            moved.append(f"{qid} {'answered' if be[qid]['answered'] else 'abstained'} -> "
                         f"{'answered' if ce[qid]['answered'] else 'abstained'}")
        elif qid in ce and be[qid].get("expect_ok") != ce[qid].get("expect_ok"):
            moved.append(f"{qid} expected phrase {be[qid].get('expect_ok')} -> "
                         f"{ce[qid].get('expect_ok')}")
    lines += ["", "Questions that moved (run 1 of each):", ""]
    lines += [f"- {m}" for m in moved] or ["- none"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two suite results.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = compare(json.loads(args.baseline.read_text(encoding="utf-8")),
                   json.loads(args.candidate.read_text(encoding="utf-8")))
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
