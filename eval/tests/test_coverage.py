"""Query coverage (Assignment 03, E2 and E4): the log reader, the measure, the grouping, the
exit code, and the thresholds in the ConfigMap. Embeddings are faked: no Ollama needed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval import coverage  # noqa: E402
from eval.gate import DEFAULT_CONFIG, load_config  # noqa: E402

# Three directions: a topic the golden set has (x), and two it does not (y, z).
X, Y, Z = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]


def near(v: list[float], eps: float = 0.1) -> list[float]:
    return [a + eps * (1 - a) / 2 for a in v]


def trace(tid: str, query: str, ts: str, **extra) -> str:
    return json.dumps({"type": "trace", "trace_id": tid, "ts": ts, "query": query,
                       "outcome": "answered", "collection": {"name": "c"},
                       "retrieved": [{"score": 0.7}], **extra})


def test_the_thresholds_are_in_the_gate_configmap() -> None:
    conf = load_config(DEFAULT_CONFIG)["coverage"]
    assert 0 < conf["group_threshold"] < conf["similarity"] < 1
    assert 0 < conf["min_share"] <= 1
    assert conf["why"]


def test_the_live_query_set_has_the_assignments_shape() -> None:
    rows = coverage.load_jsonl(ROOT / "eval" / "live_queries.jsonl")
    assert len(rows) >= 40
    assert len({r["id"] for r in rows}) == len(rows)
    assert len({r["query"] for r in rows}) == len(rows)
    domains = [r["domain"] for r in rows]
    assert domains.count("in") >= 28 and domains.count("out") >= 12


def test_the_log_reader_filters_dedupes_and_skips_redacted(tmp_path: Path) -> None:
    (tmp_path / "traces-a.jsonl").write_text("\n".join([
        trace("live-l02", "Second?", "2026-11-24T10:00:02Z"),
        trace("live-l01", "First?", "2026-11-24T10:00:01Z"),
        trace("other-1", "Not live", "2026-11-24T10:00:03Z"),
        trace("live-l03", "[not stored: self-harm support reply]", "2026-11-24T10:00:04Z"),
        "not json",
    ]), encoding="utf-8")
    (tmp_path / "traces-b.jsonl").write_text(
        trace("live-l01b", "First?", "2026-11-24T10:00:05Z"), encoding="utf-8")
    got = coverage.queries_from_log([str(tmp_path)], prefix="live-")
    assert [q["query"] for q in got] == ["First?", "Second?"]           # time order
    assert got[0]["count"] == 2 and got[0]["id"] == "live-l01"          # duplicates counted
    assert got[0]["top1"] == 0.7 and got[0]["outcome"] == "answered"
    late = coverage.queries_from_log([str(tmp_path)], since="2026-11-24T10:00:02Z")
    assert [q["query"] for q in late] == ["Second?", "Not live", "First?"]


def test_a_query_is_covered_at_the_threshold_and_the_share_decides() -> None:
    golden = [{"id": "g1", "question": "x"}]
    queries = [{"id": "a", "query": "x-ish", "domain": "in"},
               {"id": "b", "query": "y", "domain": "out"},
               {"id": "c", "query": "z", "domain": "out"}]
    res = coverage.measure(queries, golden, [near(X), Y, Z], [X], 0.7, 0.5, 0.55)
    assert [r["covered"] for r in res["rows"]] == [True, False, False]
    assert res["share"] == pytest.approx(1 / 3, abs=1e-4) and not res["passed"]
    assert res["by_domain"] == {"in": {"queries": 1, "covered": 1},
                                "out": {"queries": 2, "covered": 0}}
    assert res["rows"][0]["nearest"] == "g1"
    # Adding a golden question for the uncovered topic raises the share past the bar.
    golden2 = golden + [{"id": "g2", "question": "y"}]
    res2 = coverage.measure(queries, golden2, [near(X), Y, Z], [X, Y], 0.7, 0.5, 0.55)
    assert res2["share"] > res["share"] and res2["passed"]


def test_average_link_keeps_topics_apart() -> None:
    # Two tight topics; one bridge query close to one member of each. Single link would
    # chain all five; average link does not.
    vecs = [near(Y, 0.05), near(Y, 0.1), near(Z, 0.05), near(Z, 0.1),
            [0.0, 0.75, 0.66]]
    groups = coverage.group(list(range(5)), vecs, 0.8)
    assert sorted(map(sorted, groups))[:2] == [[0, 1], [2, 3]]
    assert [4] in groups


def test_exit_code_and_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"id": "g1", "question": "x"}) + "\n", encoding="utf-8")
    queries = tmp_path / "q.jsonl"
    queries.write_text("\n".join(json.dumps(q) for q in [
        {"id": "a", "query": "x", "domain": "in"}, {"id": "b", "query": "y", "domain": "out"},
    ]), encoding="utf-8")
    table = {"x": X, "y": Y}
    monkeypatch.setattr(coverage, "embed", lambda _c, _m, texts: [table[t] for t in texts])
    base = ["--queries", str(queries), "--golden", str(golden), "--notes", str(queries),
            "--out", str(tmp_path / "out")]
    assert coverage.main(base + ["--label", "gap"]) == 1        # 50% < the config's 85%
    assert coverage.main(base + ["--label", "ok", "--min-share", "0.5"]) == 0
    written = sorted(p.suffix for p in (tmp_path / "out").iterdir())
    assert written.count(".json") == 2 and written.count(".md") == 2
