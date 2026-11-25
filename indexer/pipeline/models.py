"""The shapes passed between stages. Metadata fields are required (section 3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)


@dataclass
class Chunk:
    id: str                  # stable: same document + same position gives the same id
    text: str
    source: str
    section: str             # the heading this chunk sits under, e.g. "5.4 Academic Warning"
    page: int
    doc_version: str
    superseded: bool
    # Set by incremental ingestion (pipeline/incremental.py); empty for the dummy index.
    doc_id: str = ""          # stable per document, e.g. "fee-structure-page"
    family: str = ""          # documents that are versions of each other share a family
    title: str = ""
    content_hash: str = ""    # SHA-256 of the source file, so a changed file is detected

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool."""
        meta: dict[str, str | int | bool] = {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
        }
        for key in ("doc_id", "family", "title", "content_hash"):
            value = getattr(self, key)
            if value:
                meta[key] = value
        return meta
