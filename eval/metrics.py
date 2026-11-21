"""Metric functions for the Part B suite (Assignment 03). Pure functions, no I/O: tested in
eval/tests/test_suite.py.

Relevance is binary and judged per golden label: a retrieved passage matches a label when its
source is the same and its section equals the label or starts with it followed by a
non-digit ("5.4" matches "5.4 Minimum CGPA ..." but not "5.41"), the rule run_eval.py has
used since Assignment 02.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Sequence
from typing import Any


def section_matches(label: str, section: str) -> bool:
    label, section = str(label).strip(), str(section).strip()
    if not label:
        return False
    if label == section:
        return True
    return section.startswith(label) and not section[len(label)].isdigit()


def matches(rel: dict[str, Any], ret: dict[str, Any]) -> bool:
    return rel.get("source") == ret.get("source") and section_matches(
        str(rel.get("section", "")), str(ret.get("section", "")))


def first_hits(relevant: Sequence[dict], retrieved: Sequence[dict], k: int) -> list[int]:
    """1-based ranks within the top k at which a not-yet-found relevant label is found.
    Each label counts once, at the first passage that matches it."""
    found: set[int] = set()
    ranks: list[int] = []
    for rank, ret in enumerate(retrieved[:k], 1):
        for i, rel in enumerate(relevant):
            if i not in found and matches(rel, ret):
                found.add(i)
                ranks.append(rank)
                break
    return ranks


def recall_at(relevant: Sequence[dict], retrieved: Sequence[dict], k: int) -> float | None:
    if not relevant:
        return None
    return len(first_hits(relevant, retrieved, k)) / len(relevant)


def reciprocal_rank(relevant: Sequence[dict], retrieved: Sequence[dict],
                    k: int = 5) -> float | None:
    """1 / rank of the first retrieved passage that matches any label (0 if none in top k)."""
    if not relevant:
        return None
    for rank, ret in enumerate(retrieved[:k], 1):
        if any(matches(rel, ret) for rel in relevant):
            return 1.0 / rank
    return 0.0


def ndcg_at(relevant: Sequence[dict], retrieved: Sequence[dict], k: int = 5) -> float | None:
    """Binary-gain nDCG@k. A label found twice is credited once (at its best rank), so a
    question with one label and two chunks of the same clause cannot score above 1."""
    if not relevant:
        return None
    dcg = sum(1.0 / math.log2(rank + 1) for rank in first_hits(relevant, retrieved, k))
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal


def mean(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in 0..100."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def contains_expected(answer: str, expect: str | list[str] | None) -> bool | None:
    """Every expected phrase appears in the answer (case and thousands separators ignored).
    None when the question has no expected phrase."""
    if not expect:
        return None
    phrases = [expect] if isinstance(expect, str) else list(expect)

    def norm(text: str) -> str:
        return re.sub(r"(?<=\d),(?=\d{3})", "", text.lower())

    return all(norm(p) in norm(answer or "") for p in phrases)


def add_typos(text: str, seed: int, rate: float = 0.2) -> str:
    """Deterministic keyboard noise: in about `rate` of the words of four letters or more,
    swap two neighbouring letters, drop one, or double one. At least one word is changed.
    The same (text, seed) always gives the same result."""
    rng = random.Random(seed)
    words = text.split(" ")
    candidates = [i for i, w in enumerate(words) if sum(c.isalpha() for c in w) >= 4]
    if not candidates:
        return text
    chosen = [i for i in candidates if rng.random() < rate] or [rng.choice(candidates)]
    for i in chosen:
        w = words[i]
        letters = [j for j, c in enumerate(w) if c.isalpha()]
        j = rng.choice(letters[1:-1] or letters)
        op = rng.choice(("swap", "drop", "double"))
        if op == "swap" and j + 1 < len(w) and w[j + 1].isalpha():
            w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
        elif op == "drop":
            w = w[:j] + w[j + 1:]
        else:
            w = w[:j] + w[j] + w[j:]
        words[i] = w
    return " ".join(words)


def retrieval_metrics(rows: Sequence[dict]) -> dict[str, float | None]:
    """Mean recall@3, recall@5, MRR and nDCG@5 over rows {relevant, retrieved}; questions
    without labels (must-abstain) are left out."""
    def r(v: float | None) -> float | None:
        return None if v is None else round(v, 4)

    labelled = [row for row in rows if row["relevant"]]
    return {
        "recall@3": r(mean([recall_at(x["relevant"], x["retrieved"], 3) for x in labelled])),
        "recall@5": r(mean([recall_at(x["relevant"], x["retrieved"], 5) for x in labelled])),
        "mrr": r(mean([reciprocal_rank(x["relevant"], x["retrieved"]) for x in labelled])),
        "ndcg@5": r(mean([ndcg_at(x["relevant"], x["retrieved"]) for x in labelled])),
        "questions": len(labelled),
    }
