#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Given: talking to the API, reading the golden set, writing results.json, the exit
code. Yours: the four functions marked below. They are the evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


def _section_matches(golden_section: str, retrieved_section: str) -> bool:
    """Check if a retrieved section matches a golden section.

    Matching rule (documented in PIPELINE.md): same source AND the retrieved
    section equals the golden section OR starts with the golden section
    followed by a non-digit character.  So golden "5.4" matches
    "5.4 Academic Warning" but not "5.41".
    """
    if golden_section == retrieved_section:
        return True
    if retrieved_section.startswith(golden_section):
        rest = retrieved_section[len(golden_section):]
        if rest and not rest[0].isdigit():
            return True
    return False


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top_k = retrieved[:k]
    found = 0
    for rel in relevant:
        rel_source = rel.get("source", "")
        rel_section = str(rel.get("section", ""))
        for ret in top_k:
            ret_source = ret.get("source", "")
            ret_section = str(ret.get("section", ""))
            if rel_source == ret_source and _section_matches(rel_section, ret_section):
                found += 1
                break
    return found / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = response.get("retrieved") or []
    valid = 0
    for cit in citations:
        cit_source = cit.get("source", "")
        cit_section = str(cit.get("section", ""))
        for ret in retrieved:
            ret_source = ret.get("source", "")
            ret_section = str(ret.get("section", ""))
            if cit_source == ret_source and _section_matches(cit_section, ret_section):
                valid += 1
                break
    return valid / len(citations)


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    recall3_values: list[float] = []
    recall5_values: list[float] = []
    cit_values: list[float] = []
    abstain_total = 0
    abstain_correct = 0
    per_type: dict[str, list[float]] = {}
    per_question: list[dict] = []

    for row in rows:
        resp = row["response"]
        relevant = row.get("relevant") or []
        retrieved = resp.get("retrieved") or []
        must_abstain = row.get("must_abstain", False)
        q_type = row.get("type", "ordinary")

        r3 = recall_at_k(relevant, retrieved, 3)
        r5 = recall_at_k(relevant, retrieved, 5)
        cv = citation_validity(resp)

        if r3 is not None:
            recall3_values.append(r3)
        if r5 is not None:
            recall5_values.append(r5)
            per_type.setdefault(q_type, []).append(r5)
        if cv is not None:
            cit_values.append(cv)

        if must_abstain:
            abstain_total += 1
            answered = resp.get("answered_from_context", True)
            if not answered:
                abstain_correct += 1

        per_question.append({
            "id": row["id"],
            "type": q_type,
            "must_abstain": must_abstain,
            "recall_3": r3,
            "recall_5": r5,
            "citation_validity": cv,
            "answered_from_context": resp.get("answered_from_context"),
            "seconds": row.get("seconds"),
        })

    recall_3 = sum(recall3_values) / len(recall3_values) if recall3_values else 0.0
    recall_5 = sum(recall5_values) / len(recall5_values) if recall5_values else 0.0
    cit_valid = sum(cit_values) / len(cit_values) if cit_values else 1.0
    abstention_correctness = (abstain_correct / abstain_total) if abstain_total > 0 else 1.0

    type_recall5 = {
        t: (sum(vals) / len(vals) if vals else 0.0)
        for t, vals in per_type.items()
    }

    return {
        "recall_3": round(recall_3, 4),
        "recall_5": round(recall_5, 4),
        "citation_validity": round(cit_valid, 4),
        "abstention_correctness": round(abstention_correctness, 4),
        "per_type": {t: round(v, 4) for t, v in type_recall5.items()},
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures: list[str] = []

    # Gate 1: recall@5 must not drop more than MAX_RECALL5_DROP below baseline
    if baseline is not None:
        baseline_recall5 = baseline.get("recall_5", 0.0)
        current_recall5 = results.get("recall_5", 0.0)
        if current_recall5 < baseline_recall5 - MAX_RECALL5_DROP:
            failures.append(
                f"recall@5 dropped to {current_recall5:.4f} "
                f"(baseline {baseline_recall5:.4f}, max drop {MAX_RECALL5_DROP})"
            )

    # Gate 2: every must-abstain question must be declined
    abstention = results.get("abstention_correctness", 1.0)
    if abstention < REQUIRED_ABSTENTION:
        failures.append(
            f"abstention correctness {abstention:.4f} < required {REQUIRED_ABSTENTION}"
        )

    # Gate 3: citation validity must meet the minimum
    cit_valid = results.get("citation_validity", 1.0)
    if cit_valid < MIN_CITATION_VALIDITY:
        failures.append(
            f"citation validity {cit_valid:.4f} < required {MIN_CITATION_VALIDITY}"
        )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
