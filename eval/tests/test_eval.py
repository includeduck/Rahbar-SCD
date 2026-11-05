"""Unit tests for eval/run_eval.py: recall_at_k, citation_validity, summarise, gate."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the eval package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.run_eval import (
    citation_validity,
    gate,
    recall_at_k,
    summarise,
)


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------

class TestRecallAtK:
    def test_none_when_no_relevant(self) -> None:
        assert recall_at_k([], [{"source": "a.pdf", "section": "1"}], 5) is None

    def test_perfect_recall(self) -> None:
        relevant = [{"source": "a.pdf", "section": "1.1"}]
        retrieved = [{"source": "a.pdf", "section": "1.1", "score": 0.9}]
        assert recall_at_k(relevant, retrieved, 5) == 1.0

    def test_zero_recall(self) -> None:
        relevant = [{"source": "a.pdf", "section": "1.1"}]
        retrieved = [{"source": "b.pdf", "section": "2.2", "score": 0.5}]
        assert recall_at_k(relevant, retrieved, 5) == 0.0

    def test_partial_recall(self) -> None:
        relevant = [
            {"source": "a.pdf", "section": "1.1"},
            {"source": "a.pdf", "section": "2.2"},
        ]
        retrieved = [
            {"source": "a.pdf", "section": "1.1", "score": 0.9},
            {"source": "c.pdf", "section": "3.3", "score": 0.5},
        ]
        assert recall_at_k(relevant, retrieved, 5) == 0.5

    def test_section_prefix_matching(self) -> None:
        """Golden '5.4' should match '5.4 Academic Warning' but not '5.41'."""
        relevant = [{"source": "a.pdf", "section": "5.4"}]
        # Matches: section starts with "5.4" and the next char is a space (non-digit)
        retrieved_good = [{"source": "a.pdf", "section": "5.4 Academic Warning", "score": 0.9}]
        assert recall_at_k(relevant, retrieved_good, 5) == 1.0
        # Does NOT match: "5.41" starts with "5.4" but next char is digit
        retrieved_bad = [{"source": "a.pdf", "section": "5.41", "score": 0.9}]
        assert recall_at_k(relevant, retrieved_bad, 5) == 0.0

    def test_respects_k_limit(self) -> None:
        relevant = [{"source": "a.pdf", "section": "1.1"}]
        retrieved = [
            {"source": "x.pdf", "section": "9.9", "score": 0.9},
            {"source": "x.pdf", "section": "8.8", "score": 0.8},
            {"source": "a.pdf", "section": "1.1", "score": 0.5},
        ]
        # With k=2, the relevant item is at position 3, so not found
        assert recall_at_k(relevant, retrieved, 2) == 0.0
        # With k=3, it is found
        assert recall_at_k(relevant, retrieved, 3) == 1.0


# ---------------------------------------------------------------------------
# citation_validity
# ---------------------------------------------------------------------------

class TestCitationValidity:
    def test_none_when_no_citations(self) -> None:
        assert citation_validity({"citations": [], "retrieved": []}) is None
        assert citation_validity({"retrieved": []}) is None

    def test_all_valid(self) -> None:
        resp = {
            "citations": [{"source": "a.pdf", "section": "1.1", "page": 1}],
            "retrieved": [{"source": "a.pdf", "section": "1.1", "score": 0.9}],
        }
        assert citation_validity(resp) == 1.0

    def test_none_valid(self) -> None:
        resp = {
            "citations": [{"source": "a.pdf", "section": "1.1", "page": 1}],
            "retrieved": [{"source": "b.pdf", "section": "2.2", "score": 0.9}],
        }
        assert citation_validity(resp) == 0.0

    def test_partial_validity(self) -> None:
        resp = {
            "citations": [
                {"source": "a.pdf", "section": "1.1", "page": 1},
                {"source": "c.pdf", "section": "3.3", "page": 3},
            ],
            "retrieved": [
                {"source": "a.pdf", "section": "1.1", "score": 0.9},
                {"source": "b.pdf", "section": "2.2", "score": 0.5},
            ],
        }
        assert citation_validity(resp) == 0.5

    def test_section_prefix_matching_in_citations(self) -> None:
        resp = {
            "citations": [{"source": "a.pdf", "section": "5.4", "page": 1}],
            "retrieved": [{"source": "a.pdf", "section": "5.4 Academic Warning", "score": 0.9}],
        }
        assert citation_validity(resp) == 1.0


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------

class TestSummarise:
    def _make_row(self, *, id: str = "q01", qtype: str = "ordinary",
                  must_abstain: bool = False, relevant: list | None = None,
                  retrieved: list | None = None, citations: list | None = None,
                  answered: bool = True) -> dict:
        return {
            "id": id,
            "type": qtype,
            "must_abstain": must_abstain,
            "relevant": relevant or [],
            "expect": None,
            "response": {
                "retrieved": retrieved or [],
                "citations": citations or [],
                "answered_from_context": answered,
            },
            "seconds": 1.0,
        }

    def test_basic_summarise(self) -> None:
        rows = [
            self._make_row(
                id="q01", qtype="ordinary",
                relevant=[{"source": "a.pdf", "section": "1.1"}],
                retrieved=[{"source": "a.pdf", "section": "1.1", "score": 0.9}],
                citations=[{"source": "a.pdf", "section": "1.1", "page": 1}],
            ),
        ]
        result = summarise(rows)
        assert result["recall_3"] == 1.0
        assert result["recall_5"] == 1.0
        assert result["citation_validity"] == 1.0
        assert result["abstention_correctness"] == 1.0
        assert "ordinary" in result["per_type"]
        assert len(result["per_question"]) == 1

    def test_abstention_correctness(self) -> None:
        rows = [
            self._make_row(id="q01", must_abstain=True, answered=False),
            self._make_row(id="q02", must_abstain=True, answered=True),  # wrong!
        ]
        result = summarise(rows)
        assert result["abstention_correctness"] == 0.5

    def test_per_type_breakdown(self) -> None:
        rows = [
            self._make_row(
                id="q01", qtype="ordinary",
                relevant=[{"source": "a.pdf", "section": "1.1"}],
                retrieved=[{"source": "a.pdf", "section": "1.1", "score": 0.9}],
            ),
            self._make_row(
                id="q02", qtype="exact",
                relevant=[{"source": "b.pdf", "section": "2.2"}],
                retrieved=[{"source": "c.pdf", "section": "3.3", "score": 0.5}],
            ),
        ]
        result = summarise(rows)
        assert result["per_type"]["ordinary"] == 1.0
        assert result["per_type"]["exact"] == 0.0


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

class TestGate:
    def test_passes_when_all_good(self) -> None:
        results = {
            "recall_5": 0.8,
            "citation_validity": 1.0,
            "abstention_correctness": 1.0,
        }
        baseline = {"recall_5": 0.8}
        assert gate(results, baseline) == []

    def test_fails_on_recall_drop(self) -> None:
        results = {
            "recall_5": 0.5,
            "citation_validity": 1.0,
            "abstention_correctness": 1.0,
        }
        baseline = {"recall_5": 0.8}
        failures = gate(results, baseline)
        assert len(failures) == 1
        assert "recall@5" in failures[0]

    def test_fails_on_abstention(self) -> None:
        results = {
            "recall_5": 0.8,
            "citation_validity": 1.0,
            "abstention_correctness": 0.5,
        }
        failures = gate(results, None)
        assert len(failures) == 1
        assert "abstention" in failures[0]

    def test_fails_on_citation_validity(self) -> None:
        results = {
            "recall_5": 0.8,
            "citation_validity": 0.7,
            "abstention_correctness": 1.0,
        }
        failures = gate(results, None)
        assert len(failures) == 1
        assert "citation validity" in failures[0]

    def test_no_baseline_skips_recall_check(self) -> None:
        results = {
            "recall_5": 0.0,
            "citation_validity": 1.0,
            "abstention_correctness": 1.0,
        }
        assert gate(results, None) == []

    def test_multiple_failures(self) -> None:
        results = {
            "recall_5": 0.1,
            "citation_validity": 0.5,
            "abstention_correctness": 0.0,
        }
        baseline = {"recall_5": 0.9}
        failures = gate(results, baseline)
        assert len(failures) == 3
