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

# A table row with every cell empty ("|  |  |  |"): pdfplumber returns one for each page of
# the Student Handbook, where it reads the footer area as a table. It carries nothing.
_EMPTY_ROW_RE = re.compile(r"^\|(?:\s*\|)+$")

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


_LETTERS_RE = re.compile(r"[A-Za-z]+")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def _line_key(line: str) -> str:
    """The words of a line, without numbers or punctuation. A running footer that carries
    the page number is then one repeated line however it is laid out: the Student Handbook
    alternates "Student Handbook 2026 3" and "4 Student Handbook 2026" on facing pages, and
    pdfplumber also reads it as a table row ("|  | Student Handbook 2026 | 3 |"); all four
    become "Student Handbook"."""
    return " ".join(_LETTERS_RE.findall(line))


def _header_candidate(line: str) -> bool:
    """May this line be a running header or footer? Only if it contains a word: a header
    such as "6 Student Handbook 2026" or "Page 3 of 85" does, while a lone number ("1.21",
    "2.00") does not, and with numbers masked every such line would share one key, so
    clause numbers and table values could be stripped everywhere. Bare page numbers are
    removed by their own rule. A line that looks like a numbered clause can qualify, but a
    real clause never repeats on half the pages, so the repetition test below is what tells
    a header from a clause."""
    return bool(line) and bool(_WORD_RE.search(line))


def _find_repeated_lines(pages: list[Page], threshold: float = 0.5) -> set[str]:
    """Find lines that appear in the top or bottom of >= threshold fraction of pages.

    Returns their keys (the words only, see _line_key). Before 11 Nov lines were compared
    exactly and number-led lines were never candidates, so the Student Handbook's running
    header ("<page> Student Handbook 2026") survived on every page and became the section
    label of most of its chunks (docs/PIPELINE.md section 7)."""
    if len(pages) < 3:
        return set()

    top_lines: Counter[str] = Counter()
    bottom_lines: Counter[str] = Counter()

    for page in pages:
        lines = page.text.splitlines()
        # Count each key once per page, in the top 3 and the bottom 3 lines
        top_lines.update({_line_key(ln) for ln in lines[:3] if _header_candidate(ln.strip())})
        bottom_lines.update({_line_key(ln) for ln in lines[-3:]
                             if _header_candidate(ln.strip())})

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

        # Remove repeated header/footer lines (compared by their words only)
        if stripped and _line_key(stripped) in repeated:
            continue

        # Remove bare page numbers
        if _PAGE_NUMBER_RE.match(stripped):
            continue

        # Remove table rows whose cells are all empty
        if _EMPTY_ROW_RE.match(stripped):
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
