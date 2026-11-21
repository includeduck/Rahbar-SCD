"""Measure the verification thresholds (Assignment 03, A1; indexer/pipeline/verify.py).

For every document in a corpus folder and its incoming/<batch> folders, runs our own extract
and clean stages and prints:

  - characters and the share of letters (the "garbled" quality check);
  - the cosine similarity of every pair of document fingerprints (the first 6,000 characters,
    embedded with the pinned model), for the near-duplicate threshold.

Run it in the indexer image (OCR needs tesseract and poppler):

    docker run --rm -v "$PWD:/repo:ro" -v "$CORPUS:/corpus:ro" -e PYTHONPATH=/repo/indexer \
      --entrypoint python rahbar-indexer:dev /repo/eval/verify_thresholds.py \
      --corpus /corpus --ollama http://host.docker.internal:11434
"""

from __future__ import annotations

import argparse
import itertools
import re
from pathlib import Path

import httpx

from pipeline.clean import clean
from pipeline.extract import extract
from pipeline.lake import discover_sources
from pipeline.verify import FINGERPRINT_CHARS, cosine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--model", default="nomic-embed-text")
    args = parser.parse_args()

    rows = []
    for doc in discover_sources(args.corpus):
        try:
            document = clean(extract(str(doc.path), doc.doc_version, False))
            text = "".join(p.text for p in document.pages)
            error = ""
        except Exception as exc:
            text, error = "", f"{type(exc).__name__}: {exc}"[:80]
        stripped = re.sub(r"\s+", "", text)
        letters = sum(ch.isalpha() for ch in stripped) / len(stripped) if stripped else 0.0
        rows.append((doc, text, len(stripped), letters, error))

    print("Quality: characters and share of letters per document")
    print(f"{'batch':<8} {'family':<22} {'file':<52} {'chars':>9} {'letters':>8}")
    for doc, _, chars, letters, error in rows:
        print(f"{doc.batch:<8} {doc.family[:22]:<22} {doc.path.name[:52]:<52} {chars:>9,} "
              f"{letters:>8.1%}  {error}")

    usable = [(doc, text) for doc, text, chars, _, _ in rows if chars]
    with httpx.Client(timeout=300) as client:
        response = client.post(f"{args.ollama.rstrip('/')}/api/embed", json={
            "model": args.model, "input": [t[:FINGERPRINT_CHARS] for _, t in usable]})
        response.raise_for_status()
        vectors = response.json()["embeddings"]

    pairs = []
    for (a, va), (b, vb) in itertools.combinations(zip([d for d, _ in usable], vectors), 2):
        same_family = a.family == b.family
        pairs.append((cosine(va, vb), a.path.name, b.path.name, same_family))
    pairs.sort(reverse=True)
    print("\nUniqueness: fingerprint cosine for every pair, highest first "
          "(same-family pairs are versions, not duplicates, and are not compared)")
    for sim, a, b, same in pairs:
        print(f"  {sim:.4f}  {'same family ' if same else 'different   '} {a[:44]:<44} {b[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
