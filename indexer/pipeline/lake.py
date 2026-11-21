"""The data lake on the corpus volume (Assignment 03, Part A; RAGOps paper 4.2.1 to 4.2.4).

Layout under CORPUS_DIR (the corpus PersistentVolumeClaim, mounted at /corpus in the Jobs):

    <root>/                      batch "A": what corpus/fetch.py downloads, listed in _index.json
    <root>/incoming/<batch>/     further batches (Batch B, or documents dropped in for a demo),
                                 each with an optional _index.json giving name, doc_version, ...
    <root>/lake/<doc_id>/<ver>/  a copy of every document that passed verification
    <root>/archive/<doc_id>/<ver>/   superseded or deleted versions, kept for rollback
    <root>/quarantine/<run>/     documents that failed verification, with <file>.reason.json
    <root>/state/ledger.json     what is indexed: per document its hash, version, chunks, status
    <root>/reports/              one verification report and one ingestion report per run

    python -m pipeline.lake counts                          chunks per document, orphans
    python -m pipeline.lake restore --family F --version V  make version V current again
    python -m pipeline.lake unpin --family F                let the newest version win again
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPECIAL_DIRS = {"incoming", "lake", "archive", "quarantine", "state", "reports"}
DOC_SUFFIXES = {".pdf", ".html", ".htm", ".md", ".txt"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "document"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def version_key(version: str) -> tuple[int, ...]:
    """"2026-06" > "2023-08"; "2023-24" > "2019-20"; "unknown" sorts first."""
    numbers = tuple(int(n) for n in re.findall(r"\d+", version or ""))
    return numbers or (-1,)


@dataclass
class SourceDoc:
    """One document found in the lake's input folders, with the metadata it came with."""

    path: Path
    batch: str
    doc_id: str
    family: str
    title: str                 # "" when nothing declares one (completeness check)
    doc_version: str           # "" when nothing declares one (completeness check)
    superseded_declared: bool
    sha256: str
    declared: dict[str, Any] = field(default_factory=dict)


def _entries(folder: Path) -> list[tuple[Path, dict[str, Any]]]:
    index = folder / "_index.json"
    if index.exists():
        listed = json.loads(index.read_text(encoding="utf-8"))
        return [(folder / e["file"], e) for e in listed if (folder / e["file"]).exists()]
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in DOC_SUFFIXES \
                and not path.name.lower().startswith("readme"):
            found.append((path, {}))
    return found


def discover_sources(root: Path) -> list[SourceDoc]:
    """Every document in batch A (the root) and in each incoming/<batch> folder."""
    folders = [("A", root)]
    incoming = root / "incoming"
    if incoming.is_dir():
        folders += [(p.name, p) for p in sorted(incoming.iterdir()) if p.is_dir()]
    docs: list[SourceDoc] = []
    for batch, folder in folders:
        for path, entry in _entries(folder):
            name = entry.get("name") or ""
            doc_id = entry.get("doc_id") or slug(name or path.stem)
            docs.append(SourceDoc(
                path=path, batch=batch, doc_id=doc_id,
                family=entry.get("family") or doc_id,
                title=entry.get("title") or name,
                doc_version=str(entry.get("doc_version") or entry.get("date") or ""),
                superseded_declared=bool(entry.get("superseded", False)),
                sha256=sha256_file(path), declared=entry,
            ))
    return docs


class Ledger:
    """What the vector store holds, per document. Kept on the volume, next to the data."""

    def __init__(self, root: Path) -> None:
        self.path = root / "state" / "ledger.json"
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.docs: dict[str, dict[str, Any]] = data.get("docs", {})
        self.pins: dict[str, str] = data.get("pins", {})
        self.fingerprints: dict[str, list[float]] = data.get("fingerprints", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"docs": self.docs, "pins": self.pins,
                                   "fingerprints": self.fingerprints}, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)


def keep_copy(root: Path, area: str, doc: SourceDoc) -> Path:
    """Copy a document into lake/ or archive/<doc_id>/<version>/ (once per hash)."""
    target = root / area / doc.doc_id / (doc.doc_version or "unversioned") / doc.path.name
    if not (target.exists() and sha256_file(target) == doc.sha256):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(doc.path, target)
    return target


def quarantine(root: Path, run_id: str, doc: SourceDoc, reasons: dict[str, str]) -> Path:
    """Move a failed document out of the input folders, with the reasons next to it."""
    target = root / "quarantine" / run_id / doc.batch / doc.path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(doc.path), target)
    target.with_name(target.name + ".reason.json").write_text(
        json.dumps({"doc_id": doc.doc_id, "batch": doc.batch, "sha256": doc.sha256,
                    "failed_checks": reasons, "quarantined_at": run_id}, indent=1),
        encoding="utf-8")
    return target


def run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def write_report(root: Path, kind: str, rid: str, payload: dict[str, Any]) -> Path:
    path = root / "reports" / f"{kind}-{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Command line: counts, restore, unpin
# ---------------------------------------------------------------------------

def _collection(settings: Any) -> Any:
    from .incremental import collection_name
    from .store import get_client

    return get_client(settings).get_collection(collection_name(settings))


def cmd_counts(settings: Any, root: Path) -> int:
    ledger = Ledger(root)
    col = _collection(settings)
    got = col.get(include=["metadatas"])
    per_doc: dict[str, int] = {}
    for meta in got["metadatas"]:
        per_doc[meta.get("doc_id", "?")] = per_doc.get(meta.get("doc_id", "?"), 0) + 1
    print(f"collection {col.name}: {sum(per_doc.values())} chunks")
    print(f"{'doc_id':<58} {'status':<10} {'version':<9} {'ledger':>6} {'chroma':>6}")
    orphans = 0
    for doc_id in sorted(set(per_doc) | set(ledger.docs)):
        entry = ledger.docs.get(doc_id, {})
        status = entry.get("status", "NOT IN LEDGER")
        expected = entry.get("chunks", 0) if status in ("active", "archived") else 0
        have = per_doc.get(doc_id, 0)
        if have != expected:
            orphans += have - expected if have > expected else 0
        flag = "" if have == expected else "  <-- mismatch"
        print(f"{doc_id:<58} {status:<10} {entry.get('doc_version', ''):<9} "
              f"{expected:>6} {have:>6}{flag}")
    print(f"orphan chunks (in Chroma, not expected by the ledger): {orphans}")
    return 1 if orphans else 0


def set_superseded(col: Any, doc_id: str, superseded: bool) -> int:
    """Flip superseded on every chunk of one document: archive or restore, no re-embedding."""
    got = col.get(where={"doc_id": doc_id}, include=["metadatas"])
    if not got["ids"]:
        return 0
    metas = [{**m, "superseded": superseded} for m in got["metadatas"]]
    col.update(ids=got["ids"], metadatas=metas)
    return len(got["ids"])


def cmd_restore(settings: Any, root: Path, family: str, version: str) -> int:
    """Make one version of a family current again (rollback), and pin it so the next
    ingestion run does not archive it again just because a newer version exists."""
    ledger = Ledger(root)
    members = {d: e for d, e in ledger.docs.items()
               if e.get("family") == family and e.get("status") in ("active", "archived")}
    if not any(e.get("doc_version") == version for e in members.values()):
        print(f"no indexed version {version!r} in family {family!r}: "
              f"{sorted(str(e.get('doc_version')) for e in members.values())}", file=sys.stderr)
        return 1
    col = _collection(settings)
    for doc_id, entry in members.items():
        current = entry.get("doc_version") == version
        n = set_superseded(col, doc_id, superseded=not current)
        entry["status"] = "active" if current else "archived"
        print(f"{'restored' if current else 'archived'}  {doc_id} ({entry.get('doc_version')}): "
              f"{n} chunks, superseded={not current}")
    ledger.pins[family] = version
    ledger.save()
    print(f"pinned {family} to {version}; undo with: python -m pipeline.lake unpin --family {family}")
    return 0


def cmd_unpin(root: Path, family: str) -> int:
    ledger = Ledger(root)
    removed = ledger.pins.pop(family, None)
    ledger.save()
    print(f"unpinned {family} (was {removed}); the next ingestion run picks the newest version")
    return 0


def main() -> int:
    from .config import get_settings

    parser = argparse.ArgumentParser(description="Inspect and roll back the data lake.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("counts")
    restore = sub.add_parser("restore")
    restore.add_argument("--family", required=True)
    restore.add_argument("--version", required=True)
    unpin = sub.add_parser("unpin")
    unpin.add_argument("--family", required=True)
    args = parser.parse_args()
    settings = get_settings()
    root = Path(settings.corpus_dir)
    if args.cmd == "counts":
        return cmd_counts(settings, root)
    if args.cmd == "restore":
        return cmd_restore(settings, root, args.family, args.version)
    return cmd_unpin(root, args.family)


if __name__ == "__main__":
    sys.exit(main())

