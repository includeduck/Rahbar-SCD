"""Stage 3: Chunk. Structure-aware, with overlap."""

from __future__ import annotations

import hashlib
import re

from .models import Chunk, Document

# Heading patterns: numbered clauses, Markdown headings, ALL-CAPS lines
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s+.+"            # Markdown headings
    r"|\d+(?:\.\d+)*\s+.+"   # numbered clauses like "1.21 Attendance"
    r"|[A-Z][A-Z\s:]{8,}$"   # ALL-CAPS lines (at least ~4 words)
    r")"
)

# Match a clause-number prefix like "1.21" or "5.4"
_CLAUSE_RE = re.compile(r"^(\d+(?:\.\d+)*)\s")

# Match a Markdown heading prefix
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)")


def _word_count(text: str) -> int:
    """Token count approximation: whitespace word count."""
    return len(text.split())


def _stable_id(source: str, section: str, index: int) -> str:
    """Deterministic chunk id from source, section and index."""
    raw = f"{source}|{section}|{index}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _extract_section_label(heading_line: str) -> str:
    """Extract a clean section label from a heading line.

    Examples:
        '## 1. Attendance' -> '1. Attendance'
        '1.21 Something'   -> '1.21 Something'
        '# Title'          -> 'Title'
        'ALL CAPS HEADING'  -> 'ALL CAPS HEADING'
    """
    # Try Markdown heading first
    md_match = _MD_HEADING_RE.match(heading_line.strip())
    if md_match:
        return md_match.group(1).strip()
    return heading_line.strip()


def _split_into_sections(full_text: str) -> list[tuple[str, str]]:
    """Split text into (heading, body) pairs based on heading detection.

    Returns a list of (section_label, section_text) tuples.
    """
    lines = full_text.split("\n")
    sections: list[tuple[str, str]] = []
    current_heading = "Untitled"
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and _HEADING_RE.match(stripped):
            # Save the previous section
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    sections.append((current_heading, body))
            current_heading = _extract_section_label(stripped)
            current_lines = [line]
        else:
            current_lines.append(line)

    # Don't forget the last section
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_heading, body))

    return sections


def _split_to_sentences(text: str) -> list[str]:
    """Split text into sentences (rough heuristic)."""
    # Split on period/question mark/exclamation followed by space and uppercase
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


def _pack_chunks(
    pieces: list[str],
    section: str,
    source: str,
    page: int,
    doc_version: str,
    superseded: bool,
    size: int,
    overlap: int,
    start_index: int,
) -> list[Chunk]:
    """Pack text pieces into chunks respecting size and overlap constraints."""
    if not pieces:
        return []

    chunks: list[Chunk] = []
    current_words: list[str] = []
    chunk_index = start_index

    for piece in pieces:
        piece_words = piece.split()
        if not piece_words:
            continue

        if current_words and _word_count(" ".join(current_words)) + len(piece_words) > size:
            # Emit current chunk
            chunk_text = section + "\n" + " ".join(current_words)
            chunks.append(Chunk(
                id=_stable_id(source, section, chunk_index),
                text=chunk_text,
                source=source,
                section=section,
                page=page,
                doc_version=doc_version,
                superseded=superseded,
            ))
            chunk_index += 1

            # Keep overlap words from the end
            if overlap > 0 and current_words:
                overlap_words = current_words[-overlap:]
                current_words = overlap_words + piece_words
            else:
                current_words = piece_words
        else:
            current_words.extend(piece_words)

    # Emit the remaining chunk
    if current_words:
        chunk_text = section + "\n" + " ".join(current_words)
        chunks.append(Chunk(
            id=_stable_id(source, section, chunk_index),
            text=chunk_text,
            source=source,
            section=section,
            page=page,
            doc_version=doc_version,
            superseded=superseded,
        ))

    return chunks


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    if not doc.pages:
        return []

    # Build a mapping of character offset -> page number
    full_text_parts: list[str] = []
    page_boundaries: list[tuple[int, int]] = []  # (start_offset, page_number)
    offset = 0
    for page in doc.pages:
        page_boundaries.append((offset, page.page))
        full_text_parts.append(page.text)
        offset += len(page.text) + 1  # +1 for the joining newline
    full_text = "\n".join(full_text_parts)

    def _find_page(text_fragment: str) -> int:
        """Find which page a text fragment most likely belongs to."""
        pos = full_text.find(text_fragment[:50])
        if pos < 0:
            return doc.pages[0].page if doc.pages else 1
        for i in range(len(page_boundaries) - 1, -1, -1):
            if pos >= page_boundaries[i][0]:
                return page_boundaries[i][1]
        return doc.pages[0].page if doc.pages else 1

    # Split into sections
    sections = _split_into_sections(full_text)
    if not sections:
        # If no headings found, treat the whole document as one section
        sections = [("Untitled", full_text)]

    all_chunks: list[Chunk] = []
    global_index = 0

    for section_label, section_text in sections:
        page_num = _find_page(section_text)

        # Split section into paragraphs
        paragraphs = re.split(r"\n\s*\n", section_text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        # Collect pieces: if a paragraph is too large, split into sentences;
        # if a sentence is still too large, split by words
        pieces: list[str] = []
        for para in paragraphs:
            if _word_count(para) > size:
                sentences = _split_to_sentences(para)
                for sent in sentences:
                    if _word_count(sent) > size:
                        # Split very long sentences by words
                        words = sent.split()
                        for i in range(0, len(words), size):
                            piece = " ".join(words[i:i + size])
                            if piece:
                                pieces.append(piece)
                    else:
                        pieces.append(sent)
            else:
                pieces.append(para)

        section_chunks = _pack_chunks(
            pieces=pieces,
            section=section_label,
            source=doc.source,
            page=page_num,
            doc_version=doc.doc_version,
            superseded=doc.superseded,
            size=size,
            overlap=overlap,
            start_index=global_index,
        )
        global_index += len(section_chunks)
        all_chunks.extend(section_chunks)

    return all_chunks
