"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # ingest, verify, update the lake, update the index (A03)
    python -m pipeline.run --full     # Assignment 02: rebuild everything into a new collection

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import IndexerSettings, get_settings
from .lake import SourceDoc
from .models import Chunk, Document


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def run_full(settings: IndexerSettings) -> int:
    """Assignment 02's pipeline: rebuild everything into a new blue/green collection."""
    from .chunk import chunk
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .store import store

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    all_chunks: list[Chunk] = []
    for path, doc_version, superseded in documents:
        doc = clean(extract(str(path), doc_version, superseded))
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        log("chunked", source=path.name, chunks=len(pieces))
        all_chunks.extend(pieces)
    vectors = embed([c.text for c in all_chunks], settings)
    return store(all_chunks, vectors, settings)


def run_pipeline(settings: IndexerSettings) -> int:
    """Assignment 03: ingest, verify, update the data lake, update the retrieval source.

    Every batch folder is read (lake.discover_sources), each document is extracted and
    cleaned, then verified (verify.py) before anything is chunked. Failures are quarantined,
    older versions archived, and only new or changed documents are embedded
    (incremental.py). Prints the verification table and the counts, and writes both as
    JSON reports on the volume.
    """
    from .chunk import chunk
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .incremental import sync
    from .lake import Ledger, discover_sources, quarantine, run_id, write_report
    from .store import get_client
    from .verify import report_table, verify_all

    root = Path(settings.lake_dir or settings.corpus_dir)
    rid = run_id()
    sources = discover_sources(Path(settings.corpus_dir))
    if not sources:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    ledger = Ledger(root)
    extracted: list[tuple[SourceDoc, Document | None, str | None]] = []
    for doc in sources:
        try:
            document = clean(extract(str(doc.path), doc.doc_version, doc.superseded_declared))
            extracted.append((doc, document, None))
        except Exception as exc:  # a corrupt file is a quality failure, not a crash
            extracted.append((doc, None, f"{type(exc).__name__}: {exc}"[:200]))

    embedded_fingerprints = 0

    def fingerprint_embedder(texts: list[str]) -> list[list[float]]:
        nonlocal embedded_fingerprints
        embedded_fingerprints += len(texts)
        return embed(texts, settings)

    verdicts = verify_all(extracted, ledger, fingerprint_embedder)
    print(report_table(verdicts), flush=True)
    for v in verdicts:
        if v.decision == "quarantine":
            moved = quarantine(root, rid, v.doc, v.failed)
            log("quarantined", doc_id=v.doc.doc_id, file=v.doc.path.name, to=str(moved),
                failed=v.failed)
    verify_path = write_report(root, "verify", rid, {
        "run": rid, "documents": [v.row() for v in verdicts],
        "totals": {d: sum(v.decision == d for v in verdicts)
                   for d in ("accept", "archive", "quarantine")}})

    result = sync(verdicts, ledger, settings, get_client(settings),
                  lambda texts: embed(texts, settings), chunk, root)
    ledger.save()
    summary = result.summary()
    summary["fingerprints_embedded"] = embedded_fingerprints
    ingest_path = write_report(root, "ingest", rid, {"run": rid, **summary})
    print(f"\ningestion run {rid} into {result.collection}: "
          f"{result.embedded_chunks} chunks embedded, {result.reused_chunks} reused, "
          f"{result.deleted_chunks} deleted; documents: "
          + ", ".join(f"{k} {len(v)}" for k, v in result.docs.items()), flush=True)
    log("ingestion finished", **{k: v for k, v in summary.items() if k != "documents"},
        verify_report=str(verify_path), ingest_report=str(ingest_path))
    return result.embedded_chunks + result.reused_chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--full", action="store_true",
                        help="Assignment 02 behaviour: rebuild everything into a new collection")
    args = parser.parse_args()
    settings = get_settings()
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        elif args.full:
            count = run_full(settings)
        else:
            count = run_pipeline(settings)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
