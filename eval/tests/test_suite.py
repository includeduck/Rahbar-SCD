"""Tests for the Part B suite (Assignment 03): metrics, the golden set, noise, run comparison
and judge agreement. Offline: no cluster, no model."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval import metrics as M  # noqa: E402
from eval.agreement import agreement  # noqa: E402
from eval.suite import (  # noqa: E402
    HERE, MIN_COUNTS, golden_counts, load_golden, noise_chunks, stochasticity, summary_table,
)

A = {"source": "a.pdf", "section": "1.21"}
B = {"source": "a.pdf", "section": "6.7"}


def ret(*sections: str) -> list[dict]:
    return [{"source": "a.pdf", "section": s} for s in sections]


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------

def test_reciprocal_rank_is_one_over_the_first_relevant_position() -> None:
    assert M.reciprocal_rank([A], ret("1.21 Students must")) == 1.0
    assert M.reciprocal_rank([A], ret("9", "9.1", "1.21 x")) == pytest.approx(1 / 3)
    assert M.reciprocal_rank([A], ret("9", "1.211")) == 0.0          # 1.211 is not 1.21
    assert M.reciprocal_rank([], ret("1.21")) is None


def test_ndcg_rewards_rank_and_counts_each_label_once() -> None:
    assert M.ndcg_at([A], ret("1.21 a", "1.21 b")) == 1.0            # duplicate chunk: once
    assert M.ndcg_at([A], ret("x", "1.21")) == pytest.approx(1 / math.log2(3))
    both = M.ndcg_at([A, B], ret("6.7 summer", "x", "1.21"))
    ideal = 1 + 1 / math.log2(3)
    assert both == pytest.approx((1 + 1 / math.log2(4)) / ideal)
    assert M.ndcg_at([A], ret("x", "y", "z", "w", "v", "1.21")) == 0.0   # rank 6 is outside


def test_recall_counts_labels_not_chunks() -> None:
    assert M.recall_at([A, B], ret("1.21", "1.21 again"), 5) == 0.5
    assert M.recall_at([A, B], ret("x", "x", "x", "6.7"), 3) == 0.0
    assert M.recall_at([A, B], ret("x", "x", "x", "6.7"), 5) == 0.5


def test_retrieval_metrics_skip_unlabelled_questions() -> None:
    rows = [{"relevant": [A], "retrieved": ret("1.21")},
            {"relevant": [], "retrieved": ret("zz")},
            {"relevant": [A], "retrieved": ret("x", "1.21")}]
    got = M.retrieval_metrics(rows)
    assert got["questions"] == 2
    assert got["recall@5"] == 1.0 and got["recall@3"] == 1.0
    assert got["mrr"] == 0.75


def test_expected_phrases_ignore_case_and_thousands_separators() -> None:
    assert M.contains_expected("The fee is Rs. 13500 per hour", "13,500") is True
    assert M.contains_expected("100% in lectures, 80% in summer", ["100%", "80%"]) is True
    assert M.contains_expected("only 100%", ["100%", "80%"]) is False
    assert M.contains_expected("anything", None) is None


def test_typos_are_deterministic_and_always_change_something() -> None:
    q = "What CGPA does an undergraduate need to stay off academic warning?"
    assert M.add_typos(q, 42) == M.add_typos(q, 42)
    assert M.add_typos(q, 42) != q
    assert M.add_typos(q, 42) != M.add_typos(q, 43) or M.add_typos(q, 44) != M.add_typos(q, 42)
    assert M.add_typos("Is it ok?", 1) == "Is it ok?"                # nothing long enough


# ---------------------------------------------------------------------------
# The golden set
# ---------------------------------------------------------------------------

def test_the_golden_set_meets_the_assignment_minimums() -> None:
    golden = load_golden(HERE / "golden.jsonl")
    counts = golden_counts(golden)
    for key, minimum in MIN_COUNTS.items():
        assert counts[key] >= minimum, key
    assert counts["synthetic"] >= 8


def test_every_golden_question_is_well_formed() -> None:
    for q in load_golden(HERE / "golden.jsonl"):
        assert q["category"] in ("answerable", "unanswerable", "hard"), q["id"]
        assert q["author"] in ("human", "synthetic"), q["id"]
        if q["author"] == "synthetic":
            assert isinstance(q["reviewed"], bool), q["id"]
        if q["category"] == "unanswerable":
            assert q["must_abstain"] and not q["relevant"], q["id"]
        else:
            assert q["relevant"] and not q["must_abstain"], q["id"]
            for rel in q["relevant"]:     # ground truth: document and page
                assert rel["source"] and rel["section"] and isinstance(rel["page"], int)
        if q["category"] == "hard":
            assert q["hard"] in ("paraphrase", "multi-chunk", "batch-b"), q["id"]


# ---------------------------------------------------------------------------
# Noise, run comparison, summary, judge agreement
# ---------------------------------------------------------------------------

def test_noise_documents_split_at_their_sections() -> None:
    chunks = noise_chunks(HERE / "noise")
    assert len({c["source"] for c in chunks}) == 3
    sections = {(c["source"], c["section"]) for c in chunks}
    assert ("northvale-fee-schedule.md", "Tuition") in sections
    assert all(c["text"] and not c["source"].lower().startswith("readme") for c in chunks)


def _run(answer: str, supported: bool, recall: float) -> dict:
    row = {"id": "q01", "answer": answer, "judge": {"supported": supported, "reason": "r"},
           "category": "answerable"}
    return {"component": {"base": {"recall@5": recall},
                          "per_question": [{"id": "q01", "retrieved": [{"chunk_id": "c1"}]}]},
            "e2e": {"faithfulness": 1.0 if supported else 0.0, "per_question": [row]}}


def test_stochasticity_reports_each_difference_and_the_changed_answers() -> None:
    same = stochasticity([_run("2.00", True, 0.8), _run("2.00", True, 0.8)])
    assert same["answers_changed"] == [] and same["judge_verdicts_changed"] == []
    assert same["metric_differences"]["component.base.recall@5"] == 0
    diff = stochasticity([_run("2.00", True, 0.8), _run("2.0", False, 0.8)])
    assert diff["answers_changed"] == ["q01"] and diff["judge_verdicts_changed"] == ["q01"]
    assert diff["metric_differences"]["e2e.faithfulness"] == -1.0


def test_agreement_matches_labels_by_question_and_answer_text(tmp_path: Path) -> None:
    result = {"runs": [{"e2e": {"per_question": [
        {"id": "q01", "answer": "2.00", "judge": {"supported": True, "reason": "ok"}},
        {"id": "q02", "answer": "20%", "judge": {"supported": True, "reason": "ok"}},
        {"id": "q03", "answer": "dropped", "judge": {"supported": False, "reason": "no"}},
    ]}}]}
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(x) for x in [
        {"id": "q01", "answer": "2.00", "supported": True, "by": "W"},
        {"id": "q02", "answer": "20%", "supported": False, "by": "W"},
        {"id": "q03", "answer": "a different answer", "supported": False, "by": "W"},
        {"id": "q04", "answer": "x", "supported": None, "by": ""},       # not labelled yet
    ]), encoding="utf-8")
    got = agreement(result, labels)
    assert got is not None
    assert got["labelled"] == 2 and got["agree"] == 1 and got["rate"] == 0.5
    assert [d["id"] for d in got["disagreements"]] == ["q02"]
    assert agreement(result, tmp_path / "missing.jsonl") is None


def test_summary_table_has_one_row_per_metric_and_the_difference() -> None:
    run = {"component": {"base": {"recall@5": 0.75, "mrr": 0.6}},
           "e2e": {"faithfulness": 0.9, "latency_p95_s": 61.2}}
    result = {"runs": [run, run], "stochasticity": stochasticity([run, run]),
              "meta": {"label": "t", "timestamp": "x", "git": "g", "collection": "c",
                       "embed_model": "e", "llm_model": "l", "temperature": 0.1,
                       "llm_seed": 42, "judge_model": "j", "seed": 42},
              "golden": {"total": 30, "answerable": 16, "unanswerable": 6, "hard": 8,
                         "human": 18, "synthetic": 12, "synthetic_reviewed": 0}}
    table = summary_table(result)
    assert "| component | recall@5 | 0.75 | 0.75 | 0 |" in table
    assert "| e2e | latency p95 (s) | 61.2 | 61.2 | 0 |" in table
    assert "nDCG@5" not in table                                       # no value, no row
