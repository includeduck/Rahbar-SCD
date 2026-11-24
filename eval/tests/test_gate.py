"""The release gate (Assignment 03, C2): its config, its rules, and the committed baseline."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.gate import (  # noqa: E402
    DEFAULT_CONFIG, evaluate, golden_fingerprint, load_config, main, metric,
)

BASELINE = ROOT / "eval" / "results" / "part-b" / "suite-baseline-20261121T151433Z.json"


def result(recall5: float = 0.70, faithfulness: float = 0.80) -> dict:
    """A two-run suite result whose metrics pass every rule in the real config."""
    def run(p95: float) -> dict:
        return {"component": {"base": {"recall@5": recall5, "ndcg@5": 0.55}},
                "e2e": {"faithfulness": faithfulness, "abstention_correctness": 1.0,
                        "answer_accuracy": 0.45, "citation_validity": 1.0,
                        "injection_resistance": 1.0, "latency_p95_s": p95}}
    return {"meta": {"label": "x", "collection": "c"}, "runs": [run(30.0), run(40.0)],
            "golden_questions": [{"id": "q01", "question": "Q?", "category": "answerable"}]}


def test_the_configmap_is_the_config() -> None:
    config = load_config(DEFAULT_CONFIG)
    names = [r["metric"] for r in config["rules"]]
    assert "component.base.recall@5" in names and "e2e.latency_p95_s" in names
    assert all(r.get("why") for r in config["rules"])            # every threshold explained


def test_the_part_b_baseline_passes_against_itself() -> None:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    passed, rows = evaluate(load_config(DEFAULT_CONFIG), base, base)
    assert passed, [r for r in rows if r["outcome"] != "PASS"]


def test_a_recall_drop_beyond_two_points_fails_and_within_passes() -> None:
    config = load_config(DEFAULT_CONFIG)
    base = result()
    ok, _ = evaluate(config, result(recall5=0.685), base)
    assert ok
    bad, rows = evaluate(config, result(recall5=0.65), base)
    assert not bad
    assert [r["metric"] for r in rows if r["outcome"] == "FAIL"] == ["component.base.recall@5"]


def test_latency_counts_the_worst_run() -> None:
    config = load_config(DEFAULT_CONFIG)
    cand = result()
    cand["runs"][1]["e2e"]["latency_p95_s"] = 120.0            # one slow run
    assert metric(cand, "e2e.latency_p95_s", "mean") == 75.0     # the mean would pass
    passed, rows = evaluate(config, cand, result())
    assert not passed
    assert [r for r in rows if r["metric"] == "e2e.latency_p95_s"][0]["candidate"] == 120.0


def test_absolute_rules_apply_without_a_baseline_and_relative_ones_skip() -> None:
    passed, rows = evaluate(load_config(DEFAULT_CONFIG), result(), None)
    assert passed
    assert {r["outcome"] for r in rows if "baseline" in r["rule"]} == {"SKIP"}


def test_a_missing_metric_fails() -> None:
    cand = result()
    for run in cand["runs"]:
        del run["e2e"]["faithfulness"]
    passed, rows = evaluate(load_config(DEFAULT_CONFIG), cand, result())
    assert not passed
    assert [r["outcome"] for r in rows if r["metric"] == "e2e.faithfulness"] == ["FAIL (missing)"]


def test_different_golden_sets_are_not_compared() -> None:
    base, cand = result(), result()
    cand["golden_questions"][0]["question"] = "A different question?"
    passed, rows = evaluate(load_config(DEFAULT_CONFIG), cand, base)
    assert not passed and rows[0]["metric"] == "golden set"
    reviewed = copy.deepcopy(base)
    reviewed["golden_questions"][0]["reviewed"] = True             # review flags do not count
    assert golden_fingerprint(reviewed) == golden_fingerprint(base)


def test_rebase_applies_only_the_absolute_rules_to_a_new_golden_set() -> None:
    base = result(recall5=0.90)                       # would fail max_drop if compared
    cand = result(recall5=0.60)
    cand["golden_questions"][0]["question"] = "A different question?"
    passed, rows = evaluate(load_config(DEFAULT_CONFIG), cand, base, rebase=True)
    outcome = {r["metric"]: r["outcome"] for r in rows}
    assert passed and outcome["golden set"] == "REBASE"
    assert outcome["component.base.recall@5"] == "SKIP"            # nothing to compare with
    assert outcome["e2e.faithfulness"] == "PASS"                    # absolute rules still apply
    low = result(faithfulness=0.50)
    low["golden_questions"][0]["question"] = "A different question?"
    assert not evaluate(load_config(DEFAULT_CONFIG), low, base, rebase=True)[0]


def test_a_threshold_edit_changes_the_outcome(tmp_path: Path) -> None:
    """F2: the same results, a stricter number in the config, a different decision."""
    cand_path, base_path = tmp_path / "cand.json", tmp_path / "base.json"
    cand_path.write_text(json.dumps(result()), encoding="utf-8")
    base_path.write_text(json.dumps(result()), encoding="utf-8")
    assert main(["--candidate", str(cand_path), "--baseline", str(base_path)]) == 0
    strict = Path(DEFAULT_CONFIG).read_text(encoding="utf-8").replace("min: 0.75", "min: 0.85")
    (tmp_path / "gate.yaml").write_text(strict, encoding="utf-8")
    assert main(["--candidate", str(cand_path), "--baseline", str(base_path),
                 "--config", str(tmp_path / "gate.yaml")]) == 1


@pytest.mark.parametrize("rule", [{"min": 0.5}, {"max_drop": 0.1}])
def test_rules_need_a_known_kind(rule: dict) -> None:
    config = {"rules": [{"metric": "e2e.faithfulness", **rule}]}
    passed, _ = evaluate(config, result(), result())
    assert passed
    with pytest.raises(SystemExit):
        evaluate({"rules": [{"metric": "e2e.faithfulness", "above": 1}]}, result(), result())


def test_compare_lists_every_level_and_the_questions_that_moved() -> None:
    from eval.compare import compare

    def res(label: str, recall: float, answered: bool) -> dict:
        r = result(recall5=recall)
        r["meta"] = {"label": label, "collection": label, "runs": 2}
        r["runs"][0]["component"]["per_question"] = [{"id": "q01", "recall@5": recall}]
        r["runs"][0]["e2e"]["per_question"] = [{"id": "q01", "answered": answered,
                                                "expect_ok": answered}]
        return r

    text = compare(res("base", 1.0, False), res("cand", 0.0, True))
    assert "| component | recall@5 | 1 | 0 | -1 |" in text
    assert "| e2e | latency p95 (s) | 40 | 40 | 0 |" in text          # the worst run
    assert "q01 recall@5 1 -> 0" in text and "q01 abstained -> answered" in text
