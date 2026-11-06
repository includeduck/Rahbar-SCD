"""Stage 1: Extract. PDF (or HTML, or Markdown) to text, page by page."""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from pathlib import Path

from .models import Document, Page

logger = logging.getLogger("rahbar.indexer.extract")


# ---------------------------------------------------------------------------
# HTML extraction (stdlib only)
# ---------------------------------------------------------------------------

class _TableAwareHTMLParser(HTMLParser):
    """Extract text from HTML, keeping headings and rendering tables as rows."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._in_heading = False
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._row_cells: list[str] = []
        self._cell_text: list[str] = []
        self._skip_tags = {"script", "style", "nav", "footer", "header"}
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._skip_tags:
            self._skip_depth += 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = True
            level = int(tag[1])
            self.parts.append("\n" + "#" * level + " ")
        elif tag == "table":
            self._in_table = True
        elif tag == "tr":
            self._in_row = True
            self._row_cells = []
        elif tag in {"td", "th"}:
            self._in_cell = True
            self._cell_text = []
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "p":
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._in_heading = False
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self._in_cell = False
            self._row_cells.append("".join(self._cell_text).strip())
        elif tag == "tr":
            self._in_row = False
            if self._row_cells:
                self.parts.append("| " + " | ".join(self._row_cells) + " |\n")
        elif tag == "table":
            self._in_table = False
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_cell:
            self._cell_text.append(data)
        else:
            self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts).strip()


def _extract_html(path: str, source: str) -> list[Page]:
    """Parse an HTML file and return it as a single page."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parser = _TableAwareHTMLParser()
    parser.feed(text)
    content = parser.get_text()
    if content:
        return [Page(source=source, page=1, text=content)]
    return []


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------

def _extract_markdown(path: str, source: str) -> list[Page]:
    """Read a Markdown file, strip front matter, return as a single page."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    # Strip YAML front matter (between leading --- markers)
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                lines = lines[i + 1:]
                break
    content = "\n".join(lines).strip()
    if content:
        return [Page(source=source, page=1, text=content)]
    return []


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _extract_pdf(path: str, source: str) -> list[Page]:
    """Extract text from each PDF page using pdfplumber; OCR image-only pages."""
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        for i, pdf_page in enumerate(pdf.pages, 1):
            text = pdf_page.extract_text() or ""

            # Extract tables and render as Markdown rows
            tables = pdf_page.extract_tables() or []
            table_text_parts: list[str] = []
            for table in tables:
                for row in table:
                    cells = [str(cell).strip() if cell else "" for cell in row]
                    table_text_parts.append("| " + " | ".join(cells) + " |")
            if table_text_parts:
                text = text + "\n\n" + "\n".join(table_text_parts)

            # If very little text, try OCR
            if len(text.strip()) < 20:
                ocr_text = _ocr_page(path, i)
                if ocr_text and len(ocr_text.strip()) > len(text.strip()):
                    pages.append(Page(source=source, page=i, text=ocr_text.strip(), ocr=True))
                    continue

            if text.strip():
                pages.append(Page(source=source, page=i, text=text.strip()))
    return pages


def _ocr_page(pdf_path: str, page_number: int) -> str:
    """OCR a single page of a PDF. Returns empty string if OCR libs aren't available."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        logger.warning("pdf2image or pytesseract not installed; skipping OCR for page %d", page_number)
        return ""
    try:
        images = convert_from_path(
            pdf_path, first_page=page_number, last_page=page_number,
            dpi=200, grayscale=True,
        )
        if not images:
            return ""
        return pytesseract.image_to_string(images[0], lang="eng")
    except Exception as exc:
        logger.warning("OCR failed for page %d: %s", page_number, exc)
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    The OCR libraries need system packages in your Dockerfile (tesseract-ocr,
    poppler-utils), not only pip packages.
    """
    file_path = Path(path)
    source = file_path.name
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        pages = _extract_pdf(path, source)
    elif suffix in {".html", ".htm"}:
        pages = _extract_html(path, source)
    elif suffix in {".md", ".txt"}:
        pages = _extract_markdown(path, source)
    else:
        logger.warning("Unsupported file type: %s", suffix)
        pages = []

    return Document(
        source=source,
        doc_version=doc_version,
        superseded=superseded,
        pages=pages,
    )
