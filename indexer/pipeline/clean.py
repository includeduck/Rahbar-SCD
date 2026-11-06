"""Stage 2: Clean. Remove artefacts without destroying structure."""

from __future__ import annotations

import re
from collections import Counter

from .models import Document, Page

# Unicode ligature replacements
_LIGATURES: dict[str, str] = {
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2013": "-",   # en dash
    "\u2014": "--",  # em dash
    "\u2026": "...", # ellipsis
    "\u00a0": " ",   # non-breaking space
}

# Bare page number: a line that is just digits (optionally with surrounding whitespace)
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")

# Heading patterns we must KEEP
_HEADING_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*\s"       # numbered clauses like 1.21, 5.4
    r"|#{1,6}\s"             # Markdown headings
    r")"
)


def _normalise_unicode(text: str) -> str:
    """Replace ligatures, smart quotes and other Unicode artefacts."""
    for old, new in _LIGATURES.items():
        text = text.replace(old, new)
    return text


def _find_repeated_lines(pages: list[Page], threshold: float = 0.5) -> set[str]:
    """Find lines that appear in the top or bottom of >= threshold fraction of pages."""
    if len(pages) < 3:
        return set()

    top_lines: Counter[str] = Counter()
    bottom_lines: Counter[str] = Counter()

    for page in pages:
        lines = page.text.splitlines()
        # Check top 3 lines
        for line in lines[:3]:
            stripped = line.strip()
            if stripped and not _HEADING_RE.match(stripped):
                top_lines[stripped] += 1
        # Check bottom 3 lines
        for line in lines[-3:]:
            stripped = line.strip()
            if stripped and not _HEADING_RE.match(stripped):
                bottom_lines[stripped] += 1

    repeated: set[str] = set()
    min_count = int(len(pages) * threshold)
    for line, count in top_lines.items():
        if count >= min_count:
            repeated.add(line)
    for line, count in bottom_lines.items():
        if count >= min_count:
            repeated.add(line)

    return repeated


def _clean_page_text(text: str, repeated: set[str]) -> str:
    """Clean a single page's text."""
    # Normalise Unicode
    text = _normalise_unicode(text)

    lines = text.splitlines()
    cleaned: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Remove repeated header/footer lines
        if stripped in repeated:
            continue

        # Remove bare page numbers
        if _PAGE_NUMBER_RE.match(stripped):
            continue

        cleaned.append(line)

    text = "\n".join(cleaned)

    # De-hyphenate line breaks: word- \n continuation → word-continuation
    # Only when a lowercase letter follows the hyphen at end of line
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)

    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    repeated = _find_repeated_lines(doc.pages)

    cleaned_pages: list[Page] = []
    for page in doc.pages:
        cleaned_text = _clean_page_text(page.text, repeated)
        if cleaned_text:
            cleaned_pages.append(Page(
                source=page.source,
                page=page.page,
                text=cleaned_text,
                ocr=page.ocr,
            ))

    return Document(
        source=doc.source,
        doc_version=doc.doc_version,
        superseded=doc.superseded,
        pages=cleaned_pages,
    )
