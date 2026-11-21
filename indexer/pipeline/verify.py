"""Stage 2b: Verify, after extraction and before chunking (Assignment 03, A1; RAGOps paper 4.2.2).

Four checks per document, each recorded as pass or fail with a reason:

  quality       extraction worked, the text is not empty and not garbled
  completeness  source, title, date or version and page count are present; the file is
                not truncated
  recency       no newer version of the same document (same family) is indexed or arriving
  uniqueness    not an exact duplicate (SHA-256) or a near-duplicate (embedding similarity)
                of another document

Decision: a failed quality, completeness or uniqueness check quarantines the document (it is
not indexed). A failed recency check alone archives it: it stays indexed with superseded=true,
so retrieval excludes it through metadata, and it can be restored (pipeline/lake.py restore).

The paper's fifth check, consistency (contradictions between documents), is not implemented.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .lake import Ledger, SourceDoc, version_key
from .models import Document

# Measured on our corpus and Batch B (docs/evidence/verification-thresholds.txt).
MIN_CHARS = int(os.environ.get("VERIFY_MIN_CHARS", 200))
MIN_LETTER_RATIO = float(os.environ.get("VERIFY_MIN_LETTER_RATIO", 0.55))
MAX_REPLACEMENT_RATIO = 0.01
NEAR_DUP_THRESHOLD = float(os.environ.get("VERIFY_NEAR_DUP_THRESHOLD", 0.97))
FINGERPRINT_CHARS = 6000

CHECKS = ("quality", "completeness", "recency", "uniqueness")


@dataclass
class Check:
    passed: bool
    reason: str


@dataclass
class Verdict:
    doc: SourceDoc
    document: Document | None
    checks: dict[str, Check] = field(default_factory=dict)
    decision: str = "accept"          # accept | archive | quarantine
    fingerprint: list[float] | None = None
    embedded_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> dict[str, str]:
        return {name: c.reason for name, c in self.checks.items() if not c.passed}

    def row(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc.doc_id, "file": self.doc.path.name, "batch": self.doc.batch,
            "family": self.doc.family, "doc_version": self.doc.doc_version or None,
            "sha256": self.doc.sha256[:12], "decision": self.decision,
            "checks": {name: {"pass": c.passed, "reason": c.reason}
                       for name, c in self.checks.items()},
        }


# ---------------------------------------------------------------------------
# Metadata the file itself carries
# ---------------------------------------------------------------------------

def embedded_metadata(path: Path) -> dict[str, Any]:
    """Title, date and page count from the file, used when no index declares them."""
    suffix = path.suffix.lower()
    meta: dict[str, Any] = {}
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path), strict=False)
            meta["pages"] = len(reader.pages)
            info: Any = reader.metadata or {}
            if info.get("/Title"):
                meta["title"] = str(info.get("/Title")).strip()
            if info.get("/CreationDate"):
                raw = str(info.get("/CreationDate"))
                match = re.search(r"(\d{4})(\d{2})?", raw)
                if match:
                    meta["date"] = "-".join(g for g in match.groups() if g)
        elif suffix in {".html", ".htm"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            if match and match.group(1).strip():
                meta["title"] = " ".join(match.group(1).split())
        elif suffix in {".md", ".txt"}:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines and lines[0].strip() == "---":
                for line in lines[1:]:
                    if line.strip() == "---":
                        break
                    key, _, value = line.partition(":")
                    if key.strip() in ("title", "doc_version", "date") and value.strip():
                        meta["date" if key.strip() != "title" else "title"] = value.strip()
            heading = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "")
            if heading and "title" not in meta:
                meta["title"] = heading
    except Exception as exc:  # a corrupt file: completeness and quality will say why
        meta["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return meta


def truncation_reason(path: Path) -> str:
    """Return why the file looks truncated, or "" if it looks whole."""
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            return "no %PDF header"
        if b"%%EOF" not in data[-2048:]:
            return "no %%EOF marker at the end (file cut short)"
    elif suffix in {".html", ".htm"}:
        if b"</html>" not in data[-4096:].lower():
            return "no closing </html> tag (file cut short)"
    return ""


# ---------------------------------------------------------------------------
# The four checks
# ---------------------------------------------------------------------------

def check_quality(document: Document | None, error: str | None) -> Check:
    if error:
        return Check(False, f"extraction failed: {error}")
    text = "".join(p.text for p in document.pages) if document else ""
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < MIN_CHARS:
        return Check(False, f"empty: {len(stripped)} characters extracted (< {MIN_CHARS})")
    letters = sum(ch.isalpha() for ch in stripped) / len(stripped)
    replacement = stripped.count("�") / len(stripped)
    if replacement > MAX_REPLACEMENT_RATIO:
        return Check(False, f"garbled: {replacement:.1%} replacement characters")
    if letters < MIN_LETTER_RATIO:
        return Check(False, f"garbled: only {letters:.0%} of characters are letters "
                            f"(< {MIN_LETTER_RATIO:.0%})")
    return Check(True, f"{len(stripped):,} characters, {letters:.0%} letters")


def check_completeness(doc: SourceDoc, document: Document | None,
                       embedded: dict[str, Any]) -> Check:
    missing = []
    title = doc.title or embedded.get("title", "")
    version = doc.doc_version or embedded.get("date", "")
    pages = len(document.pages) if document else 0
    if not title:
        missing.append("title")
    if not version:
        missing.append("date or version")
    if pages == 0:
        missing.append("page count (no page extracted)")
    truncated = truncation_reason(doc.path)
    if truncated:
        missing.append(f"truncated: {truncated}")
    expected_pages = embedded.get("pages")
    if expected_pages and document and doc.path.suffix.lower() == ".pdf" \
            and 0 < pages < expected_pages // 2:
        missing.append(f"truncated: {pages} of {expected_pages} pages extracted")
    if missing:
        return Check(False, "missing " + "; ".join(missing))
    # Fill in what the file itself declared, so later stages and the report have it.
    doc.title, doc.doc_version = title, version
    return Check(True, f"title, version {version}, {pages} pages")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def verify_all(sources: list[tuple[SourceDoc, Document | None, str | None]], ledger: Ledger,
               embedder: Callable[[list[str]], list[list[float]]]) -> list[Verdict]:
    """Run the four checks over one ingestion run's documents, in a deterministic order."""
    verdicts: list[Verdict] = []
    for doc, document, error in sources:
        verdict = Verdict(doc=doc, document=document, embedded_meta=embedded_metadata(doc.path))
        verdict.checks["quality"] = check_quality(document, error)
        verdict.checks["completeness"] = check_completeness(doc, document, verdict.embedded_meta)
        verdicts.append(verdict)

    # Fingerprints for near-duplicates: one embedding per document, cached by file hash.
    need = [v for v in verdicts if v.checks["quality"].passed
            and v.doc.sha256 not in ledger.fingerprints]
    if need:
        texts = ["".join(p.text for p in v.document.pages)[:FINGERPRINT_CHARS]
                 for v in need if v.document]
        for v, vec in zip(need, embedder(texts)):
            ledger.fingerprints[v.doc.sha256] = vec
    for v in verdicts:
        v.fingerprint = ledger.fingerprints.get(v.doc.sha256)

    # Uniqueness: against documents already indexed and documents accepted earlier this run.
    seen: list[tuple[str, str, str, list[float] | None]] = [
        (doc_id, e["sha256"], e.get("family", doc_id), ledger.fingerprints.get(e["sha256"]))
        for doc_id, e in sorted(ledger.docs.items())
        if e.get("status") in ("active", "archived")
    ]
    for v in sorted(verdicts, key=lambda v: (v.doc.batch, v.doc.doc_id)):
        if not (v.checks["quality"].passed and v.checks["completeness"].passed):
            v.checks["uniqueness"] = Check(True, "not compared (failed an earlier check)")
            continue
        reason = ""
        for other_id, other_sha, other_family, other_fp in seen:
            if other_id == v.doc.doc_id:
                continue
            if other_sha == v.doc.sha256:
                reason = f"exact duplicate of {other_id} (same SHA-256)"
                break
            if v.fingerprint and other_fp and other_family != v.doc.family:
                sim = cosine(v.fingerprint, other_fp)
                if sim >= NEAR_DUP_THRESHOLD:
                    reason = (f"near-duplicate of {other_id} (cosine {sim:.4f} >= "
                              f"{NEAR_DUP_THRESHOLD})")
                    break
        v.checks["uniqueness"] = Check(not reason, reason or "no duplicate found")
        if not reason:
            seen.append((v.doc.doc_id, v.doc.sha256, v.doc.family, v.fingerprint))

    # Recency: within each family the newest version (or a pinned one) is current.
    candidates: dict[str, list[tuple[str, str]]] = {}
    for doc_id, e in ledger.docs.items():
        if e.get("status") in ("active", "archived"):
            candidates.setdefault(e.get("family", doc_id), []).append((doc_id, e["doc_version"]))
    survivors = [v for v in verdicts
                 if all(v.checks[c].passed for c in ("quality", "completeness", "uniqueness"))]
    for v in survivors:
        fam = candidates.setdefault(v.doc.family, [])
        fam[:] = [(d, ver) for d, ver in fam if d != v.doc.doc_id] + [(v.doc.doc_id,
                                                                       v.doc.doc_version)]
    for v in verdicts:
        if v not in survivors:
            v.checks["recency"] = Check(True, "not compared (failed an earlier check)")
            continue
        family = candidates[v.doc.family]
        pinned = ledger.pins.get(v.doc.family)
        pinned_match = [(d, ver) for d, ver in family if ver == pinned]
        current_id, current_ver = (pinned_match[0] if pinned_match
                                   else max(family, key=lambda dv: version_key(dv[1])))
        if current_id == v.doc.doc_id:
            note = " (pinned by restore)" if pinned else ""
            v.checks["recency"] = Check(True, f"current version {v.doc.doc_version}{note}")
        else:
            why = (f"version {current_ver} ({current_id}) is pinned as current by a restore"
                   if pinned_match else f"newer version {current_ver} ({current_id})")
            v.checks["recency"] = Check(False, f"{why} in family {v.doc.family}")

    for v in verdicts:
        if any(not v.checks[c].passed for c in ("quality", "completeness", "uniqueness")):
            v.decision = "quarantine"
        elif not v.checks["recency"].passed:
            v.decision = "archive"
        else:
            v.decision = "accept"
    return verdicts


def report_table(verdicts: list[Verdict]) -> str:
    """The plain-text table printed by the ingestion Job (for the A1 screenshot)."""
    lines = [f"{'batch':<6} {'document':<46} " + " ".join(f"{c:<12}" for c in CHECKS)
             + " decision"]
    for v in sorted(verdicts, key=lambda v: (v.doc.batch, v.doc.doc_id)):
        marks = " ".join(f"{'pass' if v.checks[c].passed else 'FAIL':<12}" for c in CHECKS)
        lines.append(f"{v.doc.batch:<6} {v.doc.path.name[:46]:<46} {marks} {v.decision}")
        for name, reason in v.failed.items():
            lines.append(f"{'':<8}{name}: {reason}")
    return "\n".join(lines)
