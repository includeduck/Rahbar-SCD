"""Offline retrieval sweep: chunk size and overlap, OCR on or off, superseded filter on or off.

Measures recall@3 and recall@5 on eval/golden.yaml for each configuration without Chroma
or the LLM. It runs the indexer's own stages (pipeline.extract, pipeline.clean,
pipeline.chunk), embeds with the pinned model through Ollama's /api/embed, and ranks
chunks by cosine similarity exactly as the Chroma collection does (hnsw:space cosine).
It applies the api's superseded filter (same regex as api/app/retrieval/chroma.py) and
scores with eval/run_eval.py's recall_at_k. The shipped configuration (400/50, OCR on,
filter on) should reproduce the live stack's recall@5, which shows the sweep is faithful.

Run it inside the indexer image, which has tesseract and poppler for OCR:

    docker run --rm -v "$PWD:/repo:ro" -v "$OUT:/out" \
      -e PYTHONPATH=/repo/indexer:/repo --entrypoint python rahbar-indexer:dev \
      /repo/eval/chunk_sweep.py --corpus /repo/corpus/raw \
      --ollama http://host.docker.internal:11434 --work /out

Embeddings are cached in --work, so a re-run only embeds text it has not seen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from eval.run_eval import load_golden, recall_at_k
from pipeline import extract as extract_module
from pipeline.chunk import chunk
from pipeline.clean import clean
from pipeline.models import Chunk, Document, Page
from pipeline.run import discover

# Copied from api/app/retrieval/chroma.py: questions matching it may see superseded documents.
_VERSION_RE = re.compile(
    r"(older\s+version|previous\s+version|superseded|former\s+rules|"
    r"original\s+rules|before\s+(the\s+)?change|aug(ust)?\s*2023\s+rules)", re.I,
)

# (name, ocr, chunk size, overlap, superseded filter)
EXPERIMENTS = [
    ("shipped 400/50", True, 400, 50, True),
    ("small 200/40", True, 200, 40, True),
    ("large 800/100", True, 800, 100, True),
    ("tiny 100/0", True, 100, 0, True),
    ("400/50 without OCR", False, 400, 50, True),
    ("400/50 without superseded filter", True, 400, 50, False),
]


def extract_corpus(corpus: str, ocr: bool, work: Path) -> list[Document]:
    """Extract and clean every document once per OCR setting; cache the result."""
    cache = work / f"documents-{'ocr' if ocr else 'no-ocr'}.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return [Document(source=d["source"], doc_version=d["doc_version"],
                         superseded=d["superseded"],
                         pages=[Page(**p) for p in d["pages"]]) for d in raw]
    real_ocr = extract_module._ocr_page
    if not ocr:
        extract_module._ocr_page = lambda pdf_path, page_number: ""  # type: ignore[assignment]
    try:
        docs = [clean(extract_module.extract(str(path), version, superseded))
                for path, version, superseded in discover(corpus)]
    finally:
        extract_module._ocr_page = real_ocr  # type: ignore[assignment]
    cache.write_text(json.dumps([asdict(d) for d in docs]), encoding="utf-8")
    return docs


class Embedder:
    """Ollama /api/embed with an on-disk cache keyed by the text's SHA-1."""

    def __init__(self, url: str, model: str, work: Path) -> None:
        self.url, self.model = url.rstrip("/"), model
        self.path = work / f"embeddings-{model}.json"
        self.cache: dict[str, list[float]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {})
        self.client = httpx.Client(timeout=300.0)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
        missing = sorted({k: t for k, t in zip(keys, texts) if k not in self.cache}.items())
        for start in range(0, len(missing), 32):
            batch = missing[start:start + 32]
            response = self.client.post(f"{self.url}/api/embed",
                                        json={"model": self.model,
                                              "input": [t for _, t in batch]})
            response.raise_for_status()
            for (key, _), vector in zip(batch, response.json()["embeddings"]):
                self.cache[key] = vector
            if (start // 32) % 5 == 4 or start + 32 >= len(missing):
                self.path.write_text(json.dumps(self.cache), encoding="utf-8")
                print(f"    embedded {min(start + 32, len(missing))}/{len(missing)}",
                      flush=True)
        return [self.cache[k] for k in keys]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _words(text: str) -> int:
    return len(text.split())


def run_experiment(docs: list[Document], size: int, overlap: int, use_filter: bool,
                   golden: list[dict], embedder: Embedder, k: int = 5) -> dict:
    chunks: list[Chunk] = []
    for doc in docs:
        chunks.extend(chunk(doc, size, overlap))
    vectors = embedder([c.text for c in chunks])
    source_words = sum(_words(p.text) for d in docs for p in d.pages)
    chunk_words = sum(_words(c.text) for c in chunks)
    per_source: dict[str, int] = {}
    for c in chunks:
        per_source[c.source] = per_source.get(c.source, 0) + 1

    rows = []
    for item in golden:
        question = item["question"]
        query = embedder([question])[0]
        include_superseded = (not use_filter) or bool(_VERSION_RE.search(question))
        scored = sorted(
            ((_cosine(query, v), c) for c, v in zip(chunks, vectors)
             if include_superseded or not c.superseded),
            key=lambda pair: pair[0], reverse=True)[:k]
        retrieved = [{"source": c.source, "section": c.section} for _, c in scored]
        relevant = item.get("relevant") or []
        rows.append({
            "id": item["id"], "type": item.get("type", "ordinary"),
            "must_abstain": bool(item.get("must_abstain", False)),
            "recall_3": recall_at_k(relevant, retrieved, 3),
            "recall_5": recall_at_k(relevant, retrieved, 5),
            "top_score": round(scored[0][0], 4) if scored else None,
            "superseded_in_top5": sum(1 for _, c in scored if c.superseded),
            "top5": [f"{c.source} | {c.section[:60]}" for _, c in scored],
        })

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    scored_rows = [r for r in rows if r["recall_5"] is not None]
    types = sorted({r["type"] for r in scored_rows})
    return {
        "chunks": len(chunks),
        "per_source": per_source,
        "source_words": source_words,
        "chunk_words": chunk_words,
        "amplification": round(chunk_words / source_words, 3) if source_words else 0.0,
        "recall_3": mean([r["recall_3"] for r in scored_rows]),
        "recall_5": mean([r["recall_5"] for r in scored_rows]),
        "per_type": {t: mean([r["recall_5"] for r in scored_rows if r["type"] == t])
                     for t in types},
        "questions": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--golden", default=str(Path(__file__).with_name("golden.yaml")))
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--model", default="nomic-embed-text")
    parser.add_argument("--work", required=True, help="cache and output directory")
    parser.add_argument("--only", help="run only experiments whose name contains this")
    args = parser.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    golden = load_golden(Path(args.golden))
    embedder = Embedder(args.ollama, args.model, work)
    results: dict[str, dict] = {}
    for name, ocr, size, overlap, use_filter in EXPERIMENTS:
        if args.only and args.only not in name:
            continue
        started = time.time()
        print(f"==> {name}", flush=True)
        docs = extract_corpus(args.corpus, ocr, work)
        result = run_experiment(docs, size, overlap, use_filter, golden, embedder)
        result["seconds"] = round(time.time() - started, 1)
        results[name] = result
        print(f"    {result['chunks']} chunks, amplification {result['amplification']}x, "
              f"recall@3 {result['recall_3']}, recall@5 {result['recall_5']}, "
              f"per type {result['per_type']}", flush=True)
    (work / "sweep.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
