"""The release gate (Assignment 03, C2 and F2): compare a candidate's suite result with the
baseline's and exit 0 (release) or 1 (reject).

    python -m eval.gate --candidate eval/results/suite-candidate-<ts>.json \
                        --baseline eval/results/part-b/suite-baseline-<ts>.json
    python -m eval.gate ... --config k8s/base/gate-configmap.yaml     # the default locally

The thresholds are a ConfigMap (k8s/base/gate-configmap.yaml): the eval Job mounts its
gate.yaml at /config/gate.yaml (GATE_CONFIG), and this script also reads the manifest itself,
so there is one copy of the numbers. Rule kinds, per metric:

    min: x               candidate >= x
    max: x               candidate <= x
    max_drop: x          candidate >= baseline - x
    min_vs_baseline: x   candidate >= baseline - x (the same check, named for "not lower")

A metric is averaged over the result's runs unless the rule says `aggregate: max` or `min`.
Results on different golden sets are not compared: the gate fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = os.environ.get("GATE_CONFIG") or str(HERE.parent / "k8s" / "base"
                                                      / "gate-configmap.yaml")
GOLDEN_FIELDS = ("id", "question", "category", "relevant", "expect", "must_abstain",
                 "must_not_contain")


def load_config(path: str | Path) -> dict[str, Any]:
    """A plain gate.yaml, or the ConfigMap manifest that holds one under data.gate.yaml."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
        doc = yaml.safe_load(doc["data"]["gate.yaml"])
    if not isinstance(doc, dict) or not doc.get("rules"):
        raise SystemExit(f"no gate rules in {path}")
    return doc


def golden_fingerprint(result: dict[str, Any]) -> str:
    """What the questions ask and expect; review flags and notes do not count."""
    rows = [{k: q.get(k) for k in GOLDEN_FIELDS}
            for q in sorted(result.get("golden_questions", []), key=lambda q: q["id"])]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]


def metric(result: dict[str, Any], name: str, how: str = "mean") -> float | None:
    level, _, key = name.partition(".")
    values = []
    for run in result.get("runs", []):
        value: Any = run.get(level)
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    if not values:
        return None
    if how == "max":
        return max(values)
    if how == "min":
        return min(values)
    return sum(values) / len(values)


def evaluate(config: dict[str, Any], candidate: dict[str, Any],
             baseline: dict[str, Any] | None,
             rebase: bool = False) -> tuple[bool, list[dict[str, Any]]]:
    """Apply every rule. Results on different golden sets fail the gate, unless `rebase`
    says the golden set changed on purpose: then nothing is compared with the old baseline
    (its numbers answer other questions), only the absolute rules apply, and a pass makes
    this result the new baseline."""
    rows: list[dict[str, Any]] = []
    default_how = config.get("aggregate", "mean")
    if baseline is not None and golden_fingerprint(baseline) != golden_fingerprint(candidate):
        rows.append({"metric": "golden set", "baseline": golden_fingerprint(baseline),
                     "candidate": golden_fingerprint(candidate), "rule": "same questions",
                     "outcome": "REBASE" if rebase else "FAIL",
                     "why": ("the golden set changed on purpose (--rebase): the relative "
                             "rules are skipped and only the absolute ones apply") if rebase
                     else ("results on different golden sets are not comparable: re-run the "
                           "baseline, with --rebase (EVAL_REBASE=1) if the golden set "
                           "changed on purpose")})
        if rebase:
            baseline = None
    for rule in config["rules"]:
        name, how = rule["metric"], rule.get("aggregate", default_how)
        cand = metric(candidate, name, how)
        base = metric(baseline, name, how) if baseline is not None else None
        row: dict[str, Any] = {"metric": name, "aggregate": how, "baseline": base,
                               "candidate": cand, "why": " ".join(str(rule.get("why",
                                                                               "")).split())}
        if "min" in rule:
            row["rule"], ok = f">= {rule['min']}", cand is not None and cand >= rule["min"]
        elif "max" in rule:
            row["rule"], ok = f"<= {rule['max']}", cand is not None and cand <= rule["max"]
        else:
            allowed = rule.get("max_drop", rule.get("min_vs_baseline"))
            if allowed is None:
                raise SystemExit(f"rule for {name} has no min, max, max_drop or min_vs_baseline")
            if base is None:
                row["rule"], row["outcome"] = f">= baseline - {allowed}", "SKIP"
                row["why"] = "no baseline result to compare with"
                rows.append(row)
                continue
            row["rule"] = f">= baseline - {allowed} = {base - allowed:.4f}"
            ok = cand is not None and cand >= base - allowed - 1e-9
        row["outcome"] = "PASS" if ok else ("FAIL" if cand is not None else "FAIL (missing)")
        rows.append(row)
    passed = all(r["outcome"] in ("PASS", "SKIP", "REBASE") for r in rows)
    return passed, rows


def render(rows: list[dict[str, Any]], passed: bool, cand_label: str, base_label: str) -> str:
    def num(v: Any) -> str:
        return "-" if v is None else (f"{v:.4f}".rstrip("0").rstrip(".")
                                      if isinstance(v, float) else str(v))

    lines = [f"release gate: {cand_label} against {base_label}",
             f"| metric | {base_label} | {cand_label} | rule | outcome |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {num(r['baseline'])} | {num(r['candidate'])} | "
                     f"{r['rule']} | {r['outcome']} |")
    lines.append("")
    lines.append(f"GATE {'PASSED: release' if passed else 'FAILED: do not release'}")
    for r in rows:
        if r["outcome"] != "PASS":
            lines.append(f"  {r['metric']}: {r['outcome']} ({r['why']})")
    return "\n".join(lines)


def label(result: dict[str, Any] | None, fallback: str) -> str:
    if not result:
        return fallback
    meta = result.get("meta", {})
    return f"{meta.get('label', fallback)} ({meta.get('collection', '?')})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the release gate.")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--json", type=Path, help="also write the decision here")
    parser.add_argument("--rebase", action="store_true",
                        help="the golden set changed on purpose: absolute rules only")
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    baseline = (json.loads(args.baseline.read_text(encoding="utf-8"))
                if args.baseline and args.baseline.exists() else None)
    config = load_config(args.config)
    passed, rows = evaluate(config, candidate, baseline, rebase=args.rebase)
    print(render(rows, passed, label(candidate, "candidate"), label(baseline, "baseline")))
    if args.json:
        args.json.write_text(json.dumps({"passed": passed, "rows": rows, "config": args.config},
                                        indent=1), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
