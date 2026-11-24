"""Query coverage: do the golden questions cover what users actually ask? (Assignment 03, E2, E4)

For every live query, find the most similar golden question (cosine of nomic-embed-text
embeddings, the api's own embedding model). A query is covered when that similarity reaches
the threshold; the golden set covers the traffic when the covered share reaches min_share.
Below it the script exits 1 (a Job running it ends Failed), like the release gate.

The live queries come from the api's trace log (the traces volume), so they are what users
really sent, not a list we hope they sent:

    python -m eval.coverage --log /traces --request-prefix live-          # in the cluster
    python -m eval.coverage --queries eval/live_queries.jsonl             # locally, no api

The threshold and the share live in the rahbar-gate ConfigMap next to the gate's rules
(`coverage:`), so they are edited the same way. Uncovered queries are grouped into topics
(average link: two groups merge while their queries' mean similarity reaches group_threshold),
and each group is named by its most central query.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from eval.gate import DEFAULT_CONFIG, load_config

HERE = Path(__file__).resolve().parent
DEFAULTS = {"similarity": 0.70, "min_share": 0.85, "group_threshold": 0.55}
SENSITIVITY = (0.60, 0.65, 0.70, 0.75, 0.80)


# -- inputs ------------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def trace_files(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        files.extend(sorted(p.glob("*.jsonl")) if p.is_dir() else [p])
    return files


def queries_from_log(paths: Iterable[str], prefix: str = "", since: str = "",
                     until: str = "") -> list[dict[str, Any]]:
    """One entry per distinct query in the trace log, in time order. Redacted queries (the
    self-harm support reply) are skipped: their text was never stored."""
    records = []
    for f in trace_files(paths):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "trace" or not isinstance(rec.get("query"), str):
                continue
            if prefix and not str(rec.get("trace_id", "")).startswith(prefix):
                continue
            ts = str(rec.get("ts", ""))
            if (since and ts < since) or (until and ts >= until):
                continue
            if rec["query"].startswith("[not stored"):
                continue
            records.append(rec)
    records.sort(key=lambda r: str(r.get("ts", "")))
    seen: dict[str, dict[str, Any]] = {}
    for rec in records:
        q = rec["query"].strip()
        if q in seen:
            seen[q]["count"] += 1
            continue
        top1 = (rec.get("retrieved") or [{}])[0].get("score")
        seen[q] = {"id": rec.get("trace_id"), "query": q, "count": 1,
                   "outcome": rec.get("outcome"), "top1": top1,
                   "collection": (rec.get("collection") or {}).get("name")}
    return list(seen.values())


def annotate(queries: list[dict[str, Any]], notes_path: Path | None) -> None:
    """Add the author's domain and topic (eval/live_queries.jsonl) by exact query text. They
    are printed next to the automatic groups as a check, never used to make them."""
    if not notes_path or not notes_path.exists():
        return
    notes = {n["query"].strip(): n for n in load_jsonl(notes_path)}
    for q in queries:
        n = notes.get(q["query"])
        if n:
            q.setdefault("domain", n.get("domain"))
            q.setdefault("topic", n.get("topic"))


# -- the measure -------------------------------------------------------------------------

def golden_path(override: str) -> Path:
    """The golden set to measure: an override file when it exists (in the cluster, the
    ConfigMap coverage-golden, to check another golden set without a new image), else the
    one in the repository or image."""
    if override and Path(override).is_file():
        return Path(override)
    return HERE / "golden.jsonl"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest(qvecs: list[list[float]], gvecs: list[list[float]]) -> list[tuple[int, float]]:
    out = []
    for v in qvecs:
        sims = [cosine(v, g) for g in gvecs]
        i = max(range(len(sims)), key=sims.__getitem__)
        out.append((i, sims[i]))
    return out


def group(indices: list[int], vecs: list[list[float]], threshold: float) -> list[list[int]]:
    """Average-link groups of the given queries: start with one group per query and merge
    the two groups whose mean pairwise similarity is highest, while it reaches the threshold.
    (Single link chains topics together: every question here shares the university domain.)
    Each group is ordered with its most central query first, which names it."""
    sim = {(a, b): cosine(vecs[a], vecs[b]) for a in indices for b in indices if a < b}

    def s(a: int, b: int) -> float:
        return sim[(a, b) if a < b else (b, a)]

    def link(x: list[int], y: list[int]) -> float:
        return sum(s(a, b) for a in x for b in y) / (len(x) * len(y))

    groups = [[i] for i in indices]
    while len(groups) > 1:
        best, pair = -1.0, (0, 0)
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                v = link(groups[i], groups[j])
                if v > best:
                    best, pair = v, (i, j)
        if best < threshold:
            break
        i, j = pair
        groups[i] = groups[i] + groups.pop(j)

    def central_first(members: list[int]) -> list[int]:
        def mean_sim(i: int) -> float:
            others = [m for m in members if m != i]
            return sum(s(i, m) for m in others) / len(others) if others else 0.0
        return sorted(members, key=lambda i: -mean_sim(i))

    ordered = [central_first(m) for m in groups]
    return sorted(ordered, key=lambda m: (-len(m), m[0]))


def measure(queries: list[dict[str, Any]], golden: list[dict[str, Any]],
            qvecs: list[list[float]], gvecs: list[list[float]],
            similarity: float, min_share: float, group_threshold: float) -> dict[str, Any]:
    rows = []
    for q, (gi, sim) in zip(queries, nearest(qvecs, gvecs)):
        rows.append({**q, "nearest": golden[gi]["id"], "nearest_question": golden[gi]["question"],
                     "similarity": round(sim, 4), "covered": sim >= similarity})
    total = len(rows)
    covered = sum(r["covered"] for r in rows)
    share = covered / total if total else 0.0
    uncovered = [i for i, r in enumerate(rows) if not r["covered"]]
    groups = [{"topic": rows[m[0]]["query"], "queries": [rows[i]["id"] for i in m],
               "author_topics": sorted({str(rows[i]["topic"]) for i in m
                                        if rows[i].get("topic")})}
              for m in group(uncovered, qvecs, group_threshold)]
    sens = {f"{t:.2f}": round(sum(r["similarity"] >= t for r in rows) / total, 4) if total
            else 0 for t in SENSITIVITY}
    by_domain: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_domain.setdefault(str(r.get("domain", "?")), {"queries": 0, "covered": 0})
        d["queries"] += 1
        d["covered"] += int(r["covered"])
    return {"queries": total, "covered": covered, "share": round(share, 4),
            "similarity": similarity, "min_share": min_share,
            "group_threshold": group_threshold, "passed": share >= min_share,
            "golden_questions": len(golden), "by_domain": by_domain,
            "sensitivity": sens, "rows": rows, "groups": groups}


# -- output ------------------------------------------------------------------------------

def report(res: dict[str, Any]) -> str:
    rows = res["rows"]
    out = [f"golden set: {res['golden_file']}",
           f"Coverage of {res['queries']} live queries by {res['golden_questions']} golden "
           f"questions (similarity >= {res['similarity']}): {res['covered']} covered, share "
           f"{res['share']:.1%} (need {res['min_share']:.0%})", ""]
    out.append("| domain | queries | covered | share |")
    out.append("|---|---|---|---|")
    for d, c in sorted(res["by_domain"].items()):
        out.append(f"| {d} | {c['queries']} | {c['covered']} | {c['covered'] / c['queries']:.1%} |")
    out += ["", "Share covered at other similarity thresholds: " + ", ".join(
        f"{t}: {s:.1%}" for t, s in res["sensitivity"].items()), ""]
    has_log = any(r.get("outcome") for r in rows)
    head = "| query id | query | nearest golden | similarity | covered |"
    sep = "|---|---|---|---|---|"
    if has_log:
        head += " outcome | top-1 chunk |"
        sep += "---|---|"
    out += [head, sep]
    for r in rows:
        line = (f"| {r['id']} | {r['query']} | {r['nearest']} | {r['similarity']:.3f} | "
                f"{'yes' if r['covered'] else '**no**'} |")
        if has_log:
            top1 = f"{r['top1']:.3f}" if isinstance(r.get("top1"), (int, float)) else "-"
            line += f" {r.get('outcome', '-')} | {top1} |"
        out.append(line)
    out += ["", f"Uncovered queries, grouped into topics (similarity >= "
                f"{res['group_threshold']} joins two groups on average):", ""]
    by_id = {r["id"]: r for r in rows}
    for n, g in enumerate(res["groups"], 1):
        noun = "query" if len(g["queries"]) == 1 else "queries"
        out.append(f"{n}. \"{g['topic']}\" ({len(g['queries'])} {noun}; author's tags: "
                   f"{', '.join(g['author_topics']) or '-'})")
        for qid in g["queries"]:
            r = by_id[qid]
            out.append(f"   - {qid} ({r['similarity']:.3f} to {r['nearest']}): {r['query']}")
    out.append("")
    out.append("COVERAGE OK" if res["passed"] else
               f"COVERAGE GAP: {res['share']:.1%} of live queries have a golden question "
               f"(need {res['min_share']:.0%}); add golden questions for the topics above")
    return "\n".join(out)


def embed(client: httpx.Client, model: str, texts: list[str]) -> list[list[float]]:
    vecs: list[list[float]] = []
    for i in range(0, len(texts), 32):
        resp = client.post("/api/embed", json={"model": model, "input": texts[i:i + 32]})
        resp.raise_for_status()
        vecs.extend(resp.json()["embeddings"])
    return vecs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", nargs="+", help="trace JSONL files or folders (the api's traces)")
    src.add_argument("--queries", type=Path, help="a JSONL file of {id, query}")
    ap.add_argument("--request-prefix", default="", help="only traces whose id starts with it")
    ap.add_argument("--since", default="", help="only traces at or after this UTC time (ISO)")
    ap.add_argument("--until", default="", help="only traces before this UTC time (ISO)")
    ap.add_argument("--notes", type=Path, default=HERE / "live_queries.jsonl",
                    help="author's domain and topic per query, shown as a check")
    ap.add_argument("--golden", type=Path, default=None,
                    help="default: $COVERAGE_GOLDEN if that file exists (the Job mounts the "
                         "optional ConfigMap coverage-golden there), else eval/golden.jsonl")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="the gate ConfigMap or gate.yaml")
    ap.add_argument("--similarity", type=float, help="override the config's threshold")
    ap.add_argument("--min-share", type=float, help="override the config's share")
    ap.add_argument("--group-threshold", type=float)
    ap.add_argument("--label", default=os.environ.get("COVERAGE_LABEL", "live"))
    ap.add_argument("--out", type=Path, default=os.environ.get("EVAL_RESULTS_DIR") or None)
    ap.add_argument("--ollama", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    args = ap.parse_args(argv)

    conf = {**DEFAULTS, **(load_config(args.config).get("coverage") or {})}
    similarity = args.similarity if args.similarity is not None else float(conf["similarity"])
    min_share = args.min_share if args.min_share is not None else float(conf["min_share"])
    group_t = (args.group_threshold if args.group_threshold is not None
               else float(conf["group_threshold"]))

    if args.log:
        queries = queries_from_log(args.log, args.request_prefix, args.since, args.until)
        source = f"trace log {', '.join(args.log)} (prefix {args.request_prefix!r})"
    else:
        queries = [{"id": q["id"], "query": q["query"].strip(), "domain": q.get("domain"),
                    "topic": q.get("topic")} for q in load_jsonl(args.queries)]
        source = str(args.queries)
    annotate(queries, args.notes)
    if not queries:
        print(f"no queries found in {source}", file=sys.stderr)
        return 2
    args.golden = args.golden or golden_path(os.environ.get("COVERAGE_GOLDEN", ""))
    golden = load_jsonl(args.golden)

    with httpx.Client(base_url=args.ollama.rstrip("/"), timeout=300) as client:
        gvecs = embed(client, args.embed_model, [g["question"] for g in golden])
        qvecs = embed(client, args.embed_model, [q["query"] for q in queries])
    res = measure(queries, golden, qvecs, gvecs, similarity, min_share, group_t)
    res.update({"label": args.label, "source": source, "embed_model": args.embed_model,
                "golden_file": str(args.golden),
                "time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())})
    text = report(res)
    print(text)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = args.out / f"coverage-{args.label}-{res['time']}"
        stem.with_suffix(".json").write_text(json.dumps(res, indent=1, ensure_ascii=False),
                                             encoding="utf-8")
        stem.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
        print(f"results: {stem}.json (+ .md)")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
