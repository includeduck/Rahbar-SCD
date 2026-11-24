"""The Part B test suite: three levels, run twice with fixed seeds (Assignment 03; RAGOps paper
4.2.5 and Table 1). One command:

    python -m eval.suite                                  # in the eval Job: k8s/eval-job.yaml
    python -m eval.suite --api http://localhost:8000      # anywhere that reaches the stack
    python -m eval.suite --levels module,component --runs 1      # fast: no generation
    python -m eval.suite --label candidate --collection <name> --api http://api-candidate:8000

  module     embeddings: mean cosine of paraphrase pairs vs unrelated pairs and the gap;
             embedding time per chunk; determinism (the same text embedded twice)
  component  retrieval on the golden set: recall@3, recall@5, MRR, nDCG@5; then the noise
             test: typos in every question, and three distractor documents
  e2e        answers from the running api: faithfulness (LLM judge), abstention correctness,
             citation validity, answer accuracy, injection resistance, latency p50 and p95

Writes eval/results/suite-<label>-<UTC timestamp>.json (every number and every answer), a
.md summary table next to it, and a -judge-sheet.jsonl for labelling answers by hand
(eval/agreement.py reports how often the judge agrees with those labels).

Retrieval uses the api's own ChromaRetriever and guardrails, so the component level measures
the code that serves users. Settings come from the same environment variables as the api
(the rahbar-config ConfigMap in the cluster).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
try:
    import app  # noqa: F401  (the eval image has the api package next to eval/)
except ImportError:
    sys.path.insert(0, str(ROOT / "api"))

from app.config import Settings, get_settings  # noqa: E402
from app.retrieval.chroma import COLLECTIONS, ChromaRetriever  # noqa: E402
from app.schemas import Passage  # noqa: E402
from app.services.guardrails import split_poisoned  # noqa: E402

from eval import metrics as M  # noqa: E402
from eval.run_eval import citation_validity, injection_outcome  # noqa: E402

LEVELS = ("module", "component", "e2e")
MIN_COUNTS = {"total": 25, "answerable": 15, "unanswerable": 5, "hard": 5, "human": 8}


# ---------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------

def load_golden(path: Path) -> list[dict[str, Any]]:
    questions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"duplicate ids in {path}")
    return questions


def golden_counts(questions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(questions),
        "answerable": sum(q["category"] == "answerable" for q in questions),
        "unanswerable": sum(q["category"] == "unanswerable" for q in questions),
        "hard": sum(q["category"] == "hard" for q in questions),
        "human": sum(q["author"] == "human" for q in questions),
        "synthetic": sum(q["author"] == "synthetic" for q in questions),
        "synthetic_reviewed": sum(q["author"] == "synthetic" and bool(q.get("reviewed"))
                                  for q in questions),
    }


def noise_chunks(folder: Path) -> list[dict[str, str]]:
    """The distractor documents, split at their '## ' headings like the indexer's chunker."""
    chunks = []
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        title, section = path.stem, ""
        lines: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines() + ["## "]:
            if line.startswith("## "):
                text = " ".join(x for x in lines if x.strip())
                if section and text:
                    chunks.append({"chunk_id": f"noise:{path.stem}:{section}",
                                   "source": path.name, "section": section,
                                   "text": f"{title} {section} {text}"})
                section, lines = line[3:].strip(), []
            elif not line.startswith("# "):
                lines.append(line)
    return chunks


# ---------------------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------------------

class Suite:
    def __init__(self, args: argparse.Namespace, settings: Settings) -> None:
        self.args = args
        self.settings = settings
        self.seed = args.seed
        self.retriever = ChromaRetriever(settings)
        if args.collection:
            self.retriever._resolve_collection_name = lambda: args.collection  # type: ignore
        self.ollama = httpx.Client(base_url=settings.ollama_url.rstrip("/"), timeout=300)
        self.chroma = httpx.Client(
            base_url=f"http://{settings.chroma_host}:{settings.chroma_port}", timeout=30)
        self.api = httpx.Client(base_url=args.api.rstrip("/"), timeout=args.timeout)
        self.judge_model = args.judge_model or settings.llm_model
        self.stamp = args.stamp
        self._collection_id: str | None = None

    # -- helpers ---------------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self.ollama.post("/api/embed", json={"model": self.settings.embed_model,
                                                    "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def collection(self) -> tuple[str, str]:
        name = self.retriever._resolve_collection_name()
        if self._collection_id is None:
            resp = self.chroma.get(f"{COLLECTIONS}/{name}")
            resp.raise_for_status()
            self._collection_id = resp.json()["id"]
        return name, self._collection_id

    def chunk_texts(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        _, cid = self.collection()
        resp = self.chroma.post(f"{COLLECTIONS}/{cid}/get",
                                json={"ids": ids, "include": ["documents", "metadatas"]})
        resp.raise_for_status()
        got = resp.json()
        return {i: {"text": d, **m} for i, d, m in zip(got["ids"], got["documents"],
                                                       got["metadatas"])}

    @staticmethod
    def as_dicts(passages: list[Passage]) -> list[dict[str, Any]]:
        return [{"chunk_id": p.chunk_id, "source": p.source, "section": p.section,
                 "page": p.page, "doc_version": p.doc_version, "score": round(p.score, 4)}
                for p in passages]

    # -- module level ----------------------------------------------------------------
    def module_level(self) -> dict[str, Any]:
        pairs = json.loads((HERE / "embedding_pairs.json").read_text(encoding="utf-8"))
        result: dict[str, Any] = {}
        for kind in ("paraphrase", "unrelated"):
            sims = []
            for a, b in pairs[kind]:
                va, vb = self.embed([a, b])
                sims.append(M.cosine(va, vb))
            result[f"{kind}_pairs"] = len(sims)
            result[f"{kind}_mean_cosine"] = round(sum(sims) / len(sims), 4)
            result[f"{kind}_min_cosine"] = round(min(sims), 4)
            result[f"{kind}_max_cosine"] = round(max(sims), 4)
        result["gap"] = round(result["paraphrase_mean_cosine"]
                              - result["unrelated_mean_cosine"], 4)
        result["overlap"] = result["unrelated_max_cosine"] >= result["paraphrase_min_cosine"]

        # Embedding time per chunk: 40 indexed chunks, one request each, then one batch.
        _, cid = self.collection()
        got = self.chroma.post(f"{COLLECTIONS}/{cid}/get",
                               json={"limit": 40, "offset": 0, "include": ["documents"]}).json()
        texts = [t for t in got["documents"] if t][:40]
        self.embed(["warm-up"])                       # load the model before timing
        times = []
        for text in texts:
            t0 = time.perf_counter()
            self.embed([text])
            times.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        self.embed(texts)
        batch_ms = (time.perf_counter() - t0) * 1000
        result["chunks_timed"] = len(texts)
        result["embed_ms_per_chunk_mean"] = round(sum(times) / len(times), 1)
        result["embed_ms_per_chunk_p50"] = round(M.percentile(times, 50) or 0, 1)
        result["embed_ms_per_chunk_p95"] = round(M.percentile(times, 95) or 0, 1)
        result["embed_ms_per_chunk_batched"] = round(batch_ms / len(texts), 1)

        # Determinism: the same texts embedded twice, in separate requests.
        sample = texts[:5] + [pairs["paraphrase"][0][0]]
        first, second = self.embed(sample), self.embed(sample)
        diffs = [max(abs(x - y) for x, y in zip(a, b)) for a, b in zip(first, second)]
        result["determinism_texts"] = len(sample)
        result["determinism_max_abs_diff"] = max(diffs)
        result["determinism_min_cosine"] = round(min(M.cosine(a, b)
                                                     for a, b in zip(first, second)), 8)
        return result

    # -- component level -------------------------------------------------------------
    def retrieve(self, question: str) -> tuple[list[float], list[Passage]]:
        vec = self.embed([question])[0]
        return vec, self.retriever.retrieve(question, k=5, query_vec=vec)

    def component_level(self, golden: list[dict[str, Any]]) -> dict[str, Any]:
        noise = noise_chunks(HERE / "noise")
        noise_vecs = self.embed([c["text"] for c in noise])
        rows: dict[str, list[dict[str, Any]]] = {"base": [], "typos": [], "distractors": []}
        per_question = []
        for i, q in enumerate(golden):
            vec, passages = self.retrieve(q["question"])
            base = self.as_dicts(passages)
            # Distractors: what Chroma would return had the noise documents been indexed.
            extra = [{**c, "page": None, "doc_version": "noise",
                      "score": round(M.cosine(vec, v), 4)} for c, v in zip(noise, noise_vecs)]
            merged = sorted(base + [{k: v for k, v in e.items() if k != "text"} for e in extra],
                            key=lambda r: r["score"], reverse=True)[:5]
            typo_q = M.add_typos(q["question"], self.seed + i)
            _, typo_passages = self.retrieve(typo_q)
            typo = self.as_dicts(typo_passages)
            for name, retrieved in (("base", base), ("typos", typo), ("distractors", merged)):
                rows[name].append({"relevant": q["relevant"], "retrieved": retrieved})
            per_question.append({
                "id": q["id"], "category": q["category"],
                "recall@5": M.recall_at(q["relevant"], base, 5),
                "rr": M.reciprocal_rank(q["relevant"], base),
                "top1": base[0]["score"] if base else None,
                "retrieved": base,
                "typo_question": typo_q,
                "typo_recall@5": M.recall_at(q["relevant"], typo, 5),
                "distractors_in_top5": sum(r["doc_version"] == "noise" for r in merged),
                "distractor_recall@5": M.recall_at(q["relevant"], merged, 5),
            })
        result: dict[str, Any] = {name: M.retrieval_metrics(r) for name, r in rows.items()}
        for name in ("typos", "distractors"):
            result[f"{name}_change"] = {
                k: (None if result[name][k] is None or result["base"][k] is None
                    else round(result[name][k] - result["base"][k], 4))
                for k in ("recall@3", "recall@5", "mrr", "ndcg@5")}
        result["noise_documents"] = sorted({c["source"] for c in noise})
        result["noise_chunks"] = len(noise)
        result["questions_with_a_distractor_in_top5"] = sum(
            p["distractors_in_top5"] > 0 for p in per_question)
        result["per_question"] = per_question
        return result

    # -- end-to-end level -------------------------------------------------------------
    def judge(self, question: str, answer: str, context: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = "\n\n".join(f"[{i}] ({c['source']}, {c['section']})\n{c['text']}"
                               for i, c in enumerate(context, 1))
        prompt = (
            "You check a question-answering system. Decide whether the ANSWER is supported "
            "by the EVIDENCE.\nSupported means every fact in the answer (numbers, names, "
            "conditions) is stated in the evidence or follows directly from it. An answer "
            "that adds a fact the evidence does not give, or changes a number, is not "
            "supported. Do not use your own knowledge.\n\n"
            f"EVIDENCE:\n{evidence}\n\nQUESTION: {question}\n\nANSWER: {answer}\n\n"
            'Respond with JSON: {"reason": "<one sentence>", "supported": true or false}')
        schema = {"type": "object", "properties": {"reason": {"type": "string"},
                                                   "supported": {"type": "boolean"}},
                  "required": ["reason", "supported"]}
        t0 = time.perf_counter()
        resp = self.ollama.post("/api/chat", json={
            "model": self.judge_model, "messages": [{"role": "user", "content": prompt}],
            "format": schema, "stream": False,
            "options": {"temperature": 0, "seed": self.seed}})
        resp.raise_for_status()
        verdict = json.loads(resp.json()["message"]["content"])
        return {"supported": bool(verdict.get("supported")),
                "reason": str(verdict.get("reason", ""))[:300],
                "ms": round((time.perf_counter() - t0) * 1000)}

    def context_of(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """The passages the api put in the prompt: the retrieved ones minus any the
        retrieval rail drops, best first, max_context_passages of them (answer.py)."""
        ids = [r["chunk_id"] for r in response.get("retrieved", [])]
        found = self.chunk_texts(ids) if ids else {}
        passages = [Passage(chunk_id=i, text=found[i]["text"] or "",
                            source=str(found[i].get("source")),
                            section=str(found[i].get("section")),
                            page=found[i].get("page"), score=r["score"])
                    for i, r in zip(ids, response["retrieved"]) if i in found]
        clean, _ = split_poisoned(passages)
        return [{"source": p.source, "section": p.section, "page": p.page, "text": p.text}
                for p in clean[: max(1, self.settings.max_context_passages)]]

    def e2e_level(self, golden: list[dict[str, Any]], run: int) -> dict[str, Any]:
        per_question = []
        for q in golden:
            rid = f"eval-{self.args.label}-{self.stamp}-r{run}-{q['id']}"
            t0 = time.perf_counter()
            resp = self.api.post("/api/ask", json={"question": q["question"]},
                                 headers={"X-Request-ID": rid, "Cache-Control": "no-cache"})
            resp.raise_for_status()
            body = resp.json()
            seconds = time.perf_counter() - t0
            answered = bool(body["answered_from_context"])
            row: dict[str, Any] = {
                "id": q["id"], "category": q["category"], "type": q.get("type"),
                "request_id": rid, "seconds": round(seconds, 2), "answered": answered,
                "answer": body["answer"], "citations": body["citations"],
                "citation_validity": citation_validity(body),
                "expect_ok": (M.contains_expected(body["answer"], q.get("expect"))
                              if answered else (False if q.get("expect") else None)),
                "injection": injection_outcome({"type": q.get("type"), "response": body,
                                                "must_not_contain": q.get("must_not_contain")}),
            }
            if answered:
                context = self.context_of(body)
                row["context"] = context
                row["judge"] = self.judge(q["question"], body["answer"], context)
            per_question.append(row)
            print(f"    {q['id']:<4} {seconds:6.1f}s  "
                  f"{'answered' if answered else 'abstained':<9} "
                  f"{'' if 'judge' not in row else 'supported' if row['judge']['supported'] else 'NOT SUPPORTED'}",
                  flush=True)

        unanswerable = [r for r in per_question if r["category"] == "unanswerable"]
        answerable = [r for r in per_question if r["category"] != "unanswerable"]
        judged = [r for r in per_question if "judge" in r]
        with_expect = [r for r in answerable if r["expect_ok"] is not None]
        injections = [r["injection"] for r in per_question
                      if r["injection"] not in (None, "skipped (stub)")]
        latencies = [r["seconds"] for r in per_question]
        answered_seconds = [r["seconds"] for r in per_question if r["answered"]]

        def rate(num: int, den: int) -> float | None:
            return round(num / den, 4) if den else None

        return {
            "questions": len(per_question),
            "answered": sum(r["answered"] for r in per_question),
            "faithfulness": rate(sum(r["judge"]["supported"] for r in judged), len(judged)),
            "judged": len(judged),
            "abstention_correctness": rate(sum(not r["answered"] for r in unanswerable),
                                           len(unanswerable)),
            "false_abstention_rate": rate(sum(not r["answered"] for r in answerable),
                                          len(answerable)),
            "citation_validity": (round(v, 4) if (v := M.mean(
                [r["citation_validity"] for r in per_question])) is not None else None),
            "answer_accuracy": rate(sum(bool(r["expect_ok"]) for r in with_expect),
                                    len(with_expect)),
            "injection_resistance": rate(injections.count("resisted"), len(injections)),
            "latency_p50_s": M.percentile(latencies, 50),
            "latency_p95_s": M.percentile(latencies, 95),
            "latency_answered_p50_s": M.percentile(answered_seconds, 50),
            "latency_answered_p95_s": M.percentile(answered_seconds, 95),
            "per_question": per_question,
        }

    # -- one run ------------------------------------------------------------------------
    def run(self, golden: list[dict[str, Any]], run: int, levels: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for level in levels:
            t0 = time.perf_counter()
            print(f"  run {run}: {level} level", flush=True)
            if level == "module":
                out[level] = self.module_level()
            elif level == "component":
                out[level] = self.component_level(golden)
            else:
                out[level] = self.e2e_level(golden, run)
            out[level]["seconds"] = round(time.perf_counter() - t0, 1)
        return out


# ---------------------------------------------------------------------------------------
# Comparing runs and writing results
# ---------------------------------------------------------------------------------------

SUMMARY_ROWS = [
    ("module", "paraphrase_mean_cosine", "paraphrase pairs, mean cosine"),
    ("module", "unrelated_mean_cosine", "unrelated pairs, mean cosine"),
    ("module", "gap", "gap (paraphrase - unrelated)"),
    ("module", "embed_ms_per_chunk_mean", "embedding ms per chunk (one per request)"),
    ("module", "embed_ms_per_chunk_batched", "embedding ms per chunk (batched)"),
    ("module", "determinism_max_abs_diff", "determinism: largest difference"),
    ("component", "base.recall@3", "recall@3"),
    ("component", "base.recall@5", "recall@5"),
    ("component", "base.mrr", "MRR"),
    ("component", "base.ndcg@5", "nDCG@5"),
    ("component", "typos.recall@5", "recall@5 with typos"),
    ("component", "typos.mrr", "MRR with typos"),
    ("component", "typos.ndcg@5", "nDCG@5 with typos"),
    ("component", "distractors.recall@5", "recall@5 with 3 distractor documents"),
    ("component", "distractors.mrr", "MRR with distractors"),
    ("component", "distractors.ndcg@5", "nDCG@5 with distractors"),
    ("e2e", "faithfulness", "faithfulness (judge)"),
    ("e2e", "abstention_correctness", "abstention correctness (unanswerable)"),
    ("e2e", "false_abstention_rate", "false abstention rate (answerable)"),
    ("e2e", "citation_validity", "citation validity"),
    ("e2e", "answer_accuracy", "answer accuracy (expected phrase)"),
    ("e2e", "injection_resistance", "injection resistance"),
    ("e2e", "latency_p50_s", "latency p50 (s)"),
    ("e2e", "latency_p95_s", "latency p95 (s)"),
]


def lookup(run: dict[str, Any], level: str, key: str) -> Any:
    value: Any = run.get(level, {})
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def stochasticity(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Run 2 minus run 1 for every summary metric, and which answers changed."""
    if len(runs) < 2:
        return {}
    a, b = runs[0], runs[1]
    diffs = {}
    for level, key, _ in SUMMARY_ROWS:
        x, y = lookup(a, level, key), lookup(b, level, key)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            diffs[f"{level}.{key}"] = round(y - x, 6)
    out: dict[str, Any] = {"metric_differences": diffs}
    if "e2e" in a and "e2e" in b:
        pa = {r["id"]: r for r in a["e2e"].get("per_question", [])}
        pb = {r["id"]: r for r in b["e2e"].get("per_question", [])}
        changed = [i for i in pa if i in pb and pa[i]["answer"] != pb[i]["answer"]]
        verdicts = [i for i in pa if i in pb and "judge" in pa[i] and "judge" in pb[i]
                    and pa[i]["judge"]["supported"] != pb[i]["judge"]["supported"]]
        out["answers_identical"] = len(pa) - len(changed)
        out["answers_changed"] = changed
        out["judge_verdicts_changed"] = verdicts
    if "component" in a and "component" in b:
        ra = {r["id"]: [x["chunk_id"] for x in r["retrieved"]]
              for r in a["component"].get("per_question", [])}
        rb = {r["id"]: [x["chunk_id"] for x in r["retrieved"]]
              for r in b["component"].get("per_question", [])}
        out["retrieval_rankings_changed"] = [i for i in ra if ra[i] != rb.get(i)]
    return out


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value == 0:
            return "0"
        if abs(value) < 1e-3:
            return f"{value:.2e}"
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def summary_table(result: dict[str, Any]) -> str:
    runs = result["runs"]
    head = "| level | metric | " + " | ".join(f"run {i + 1}" for i in range(len(runs)))
    head += " | difference |" if len(runs) > 1 else " |"
    lines = [head, "|" + "---|" * (2 + len(runs) + (1 if len(runs) > 1 else 0))]
    diffs = result.get("stochasticity", {}).get("metric_differences", {})
    for level, key, label in SUMMARY_ROWS:
        values = [lookup(r, level, key) for r in runs]
        if all(v is None for v in values):
            continue
        row = f"| {level} | {label} | " + " | ".join(fmt(v) for v in values)
        if len(runs) > 1:
            row += f" | {fmt(diffs.get(f'{level}.{key}'))}"
        lines.append(row + " |")
    meta, counts = result["meta"], result["golden"]
    agreement = result.get("judge_agreement") or {}
    stoch = result.get("stochasticity", {})
    notes = [
        f"Suite `{meta['label']}` at {meta['timestamp']} (git {meta['git']}), collection "
        f"`{meta['collection']}`, embedding `{meta['embed_model']}`, generation "
        f"`{meta['llm_model']}` (temperature {meta['temperature']}, seed {meta['llm_seed']}), "
        f"judge `{meta['judge_model']}`, suite seed {meta['seed']}.",
        f"Golden set: {counts['total']} questions, {counts['answerable']} answerable, "
        f"{counts['unanswerable']} unanswerable, {counts['hard']} hard; {counts['human']} human, "
        f"{counts['synthetic']} synthetic ({counts['synthetic_reviewed']} reviewed by hand).",
    ]
    if stoch.get("answers_changed") is not None:
        notes.append(f"Between the runs {stoch['answers_identical']} answers were identical; "
                     f"changed: {', '.join(stoch['answers_changed']) or 'none'}; judge "
                     f"verdicts changed: {', '.join(stoch['judge_verdicts_changed']) or 'none'}; "
                     f"retrieval rankings changed: "
                     f"{', '.join(stoch.get('retrieval_rankings_changed', [])) or 'none'}.")
    if agreement:
        notes.append(f"Judge vs hand labels: {agreement.get('agree')} of "
                     f"{agreement.get('labelled')} agree ({fmt(agreement.get('rate'))}).")
    return "\n".join(lines) + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"


def git_sha() -> str:
    if os.environ.get("GIT_SHA"):
        return os.environ["GIT_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "-"
    except Exception:
        return "-"


def write_outputs(result: dict[str, Any], out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    gate = result.get("gate", {}).get("text")
    (out_dir / f"{name}.md").write_text(
        f"# {name}\n\n{summary_table(result)}" + (f"\n```\n{gate}\n```\n" if gate else ""),
        encoding="utf-8")
    first = result["runs"][0].get("e2e")
    if first:
        golden = {q["id"]: q for q in result["golden_questions"]}
        with (out_dir / f"{name}-judge-sheet.jsonl").open("w", encoding="utf-8") as sheet:
            for r in first["per_question"]:
                if "judge" in r:
                    sheet.write(json.dumps({
                        "id": r["id"], "question": golden[r["id"]]["question"],
                        "answer": r["answer"], "context": r["context"],
                        "supported": None, "by": ""}, ensure_ascii=False) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the three-level suite.")
    parser.add_argument("--api", default=os.environ.get("EVAL_API", "http://api:8000"))
    parser.add_argument("--golden", type=Path, default=HERE / "golden.jsonl")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(os.environ.get("EVAL_RESULTS_DIR", HERE / "results")))
    parser.add_argument("--label", default=os.environ.get("EVAL_LABEL", "baseline"))
    parser.add_argument("--levels", default=",".join(LEVELS))
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--collection", default=os.environ.get("EVAL_COLLECTION") or None,
                        help="evaluate this collection instead of the rahbar-active one")
    parser.add_argument("--judge-model", default=os.environ.get("JUDGE_MODEL") or None)
    parser.add_argument("--limit", type=int, help="only the first N questions (debugging)")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--gate", action="store_true",
                        default=os.environ.get("EVAL_GATE", "") == "1",
                        help="apply the release gate against --baseline; exit 1 if it fails")
    parser.add_argument("--baseline", type=Path,
                        default=Path(os.environ["EVAL_BASELINE"])
                        if os.environ.get("EVAL_BASELINE") else None,
                        help="the result to gate against (default <out-dir>/baseline.json)")
    parser.add_argument("--save-baseline", action="store_true",
                        default=os.environ.get("EVAL_SAVE_BASELINE", "") == "1",
                        help="if the gate passes (or is not applied), make this the baseline")
    args = parser.parse_args(argv)
    args.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    if unknown := set(levels) - set(LEVELS):
        parser.error(f"unknown levels {sorted(unknown)}")

    settings = get_settings()
    golden = load_golden(args.golden)
    counts = golden_counts(golden)
    short = [f"{k} {counts[k]} < {v}" for k, v in MIN_COUNTS.items() if counts[k] < v]
    if short:
        print("golden set below the assignment's minimum: " + "; ".join(short), flush=True)
    if args.limit:
        golden = golden[: args.limit]

    suite = Suite(args, settings)
    collection, _ = suite.collection()
    print(f"suite {args.label}: {len(golden)} questions, levels {levels}, {args.runs} run(s), "
          f"collection {collection}, api {args.api}", flush=True)
    runs = [suite.run(golden, i + 1, levels) for i in range(args.runs)]
    result: dict[str, Any] = {
        "meta": {"label": args.label, "timestamp": args.stamp, "git": git_sha(),
                 "collection": collection, "embed_model": settings.embed_model,
                 "llm_model": settings.llm_model, "temperature": settings.temperature,
                 "llm_seed": settings.llm_seed, "top_k": settings.top_k,
                 "max_context_passages": settings.max_context_passages,
                 "relevance_floor": settings.relevance_floor, "judge_model": suite.judge_model,
                 "seed": args.seed, "api": args.api, "levels": levels, "runs": args.runs},
        "golden": counts,
        "golden_questions": golden,
        "runs": runs,
        "stochasticity": stochasticity(runs),
    }
    from eval.agreement import agreement

    result["judge_agreement"] = agreement(result, HERE / "judge_labels.jsonl")
    name = f"suite-{args.label}-{args.stamp}"
    path = write_outputs(result, args.out_dir, name)
    print("\n" + summary_table(result))
    status = 0
    baseline_path = args.baseline or args.out_dir / "baseline.json"
    if args.gate:
        from eval.gate import DEFAULT_CONFIG, evaluate, label, load_config, render

        baseline = (json.loads(baseline_path.read_text(encoding="utf-8"))
                    if baseline_path.exists() else None)
        passed, rows = evaluate(load_config(DEFAULT_CONFIG), result, baseline)
        text = render(rows, passed, label(result, "candidate"), label(baseline, "baseline"))
        result["gate"] = {"passed": passed, "rows": rows, "config": DEFAULT_CONFIG,
                          "baseline": str(baseline_path) if baseline else None, "text": text}
        path = write_outputs(result, args.out_dir, name)
        print(text, flush=True)
        status = 0 if passed else 1
    print(f"results: {path} (+ .md summary, -judge-sheet.jsonl)", flush=True)
    if args.save_baseline and status == 0:
        shutil.copyfile(path, baseline_path)
        print(f"saved as the baseline: {baseline_path}", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
