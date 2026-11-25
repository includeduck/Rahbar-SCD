#!/usr/bin/env python3
"""Build Batch B, the documents with planted problems (Assignment 03, Part A).

    python corpus/batch_b.py --root corpus/raw        # locally, next to the fetched corpus
    python /app/corpus/batch_b.py --root /corpus      # in the cluster, on the corpus volume

Writes <root>/incoming/batch-b/: copies of fetched documents turned into duplicates, a
near-duplicate and a truncated file, a garbled file, and the synthetic documents written in
corpus/batch-b/ (see its README.md for what each one plants and which check should catch it).
Nothing third-party is committed: the copies are made from the fetched corpus at run time.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTHORED = HERE / "batch-b"

# file in the batch, then the _index.json entry it gets (no entry fields = no metadata)
AUTHORED_FILES = {
    "exam-hall-rules.md": {"name": "Examination Hall Rules", "doc_version": "2026-11"},
    "course-withdrawal-faq.md": {"name": "Course Withdrawal FAQ", "doc_version": "2026-11"},
    "library-services-guide.md": {"name": "Library Services Guide", "doc_version": "2026-11"},
    "fee-structure-2027-28.md": {"name": "Fee Structure (2027-28 revision)",
                                 "doc_version": "2027-28", "family": "fee-structure-page"},
    "untitled-notice.md": {},
}


def build(root: Path) -> Path:
    out = root / "incoming" / "batch-b"
    out.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []

    def need(name: str) -> Path:
        path = root / name
        if not path.exists():
            raise SystemExit(f"{path} is missing: fetch the corpus first (corpus/fetch.py)")
        return path

    # Exact duplicates: byte-for-byte copies under new names.
    shutil.copyfile(need("grading-page.html"), out / "grading-copy.html")
    entries.append({"file": "grading-copy.html", "name": "Grading page (copy)",
                    "doc_version": "2026"})
    shutil.copyfile(need("fee-structure-page.html"), out / "fee-structure-copy.html")
    entries.append({"file": "fee-structure-copy.html", "name": "Fee Structure page (copy)",
                    "doc_version": "2026-27"})

    # Near-duplicate: the Grading page with one sentence added, so the hash differs.
    html = need("grading-page.html").read_text(encoding="utf-8", errors="replace")
    marker = "</body>" if "</body>" in html else "</html>"
    html = html.replace(marker, "<p>This page is mirrored for offline reading.</p>\n" + marker, 1)
    (out / "grading-near-duplicate.html").write_text(html, encoding="utf-8")
    entries.append({"file": "grading-near-duplicate.html", "name": "Grading page (mirror)",
                    "doc_version": "2026"})

    # Truncated: the first 35% of the Academic Rules Handbook, as if a download was cut.
    pdf = need("academic-rules-handbook-jun-2026.pdf").read_bytes()
    (out / "academic-rules-truncated.pdf").write_bytes(pdf[: int(len(pdf) * 0.35)])
    entries.append({"file": "academic-rules-truncated.pdf",
                    "name": "Academic Rules Handbook (partial download)", "doc_version": "2026-06"})

    # Garbled: what a failed OCR of a scanned notice can look like.
    rng = random.Random(3071)
    noise = "".join(rng.choice("#%&@*0123456789|/\\{}[]<>~^;:") + (" " if rng.random() < 0.2
                                                                    else "") for _ in range(1500))
    (out / "garbled-scan.txt").write_text(
        "Synthetic test document (Assignment 03, Batch B).\n" + noise + "\n", encoding="utf-8")
    entries.append({"file": "garbled-scan.txt", "name": "Scanned notice (OCR output)",
                    "doc_version": "2026-11"})

    for name, entry in AUTHORED_FILES.items():
        shutil.copyfile(AUTHORED / name, out / name)
        entries.append({"file": name, **entry})

    (out / "_index.json").write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path,
                        help="the corpus folder: where fetch.py put the documents")
    args = parser.parse_args()
    out = build(args.root)
    print(f"Batch B written to {out}: {len(list(out.glob('*'))) - 1} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
