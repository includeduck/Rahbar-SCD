# Indexing and Retrieval Pipeline

This document describes the design, implementation, and evaluation methodology of the RAHBAR indexing and retrieval pipeline.

---

## 1. Extraction

The extraction stage (`indexer/pipeline/extract.py`) transforms raw documents into structured `Page` objects, tracking per-page provenance and whether optical character recognition was required:

- **PDF Documents (`pdfplumber`):** Extracts text page-by-page. Tables are extracted via `extract_tables()` and rendered directly into Markdown table rows (`| col1 | col2 |`) to preserve tabular structure and column alignments.
- **Scanned & Image-Only Pages (OCR):** Pages with fewer than 20 characters of extractable text are flagged as image-only. They are rendered at 200 DPI greyscale using `pdf2image.convert_from_path` and OCR'd using `pytesseract` with `Page.ocr = True`. Specifically:
  - `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` (8 pages): 100% image-only scan, completely recovered via OCR.
  - `undergraduate-academic-rules-aug-2023.pdf` (26 pages): 24 of 26 pages lack a text layer (Corel export artifact). All clause numbers, tables, and rules recovered cleanly via OCR.
- **HTML Pages (Standard Library `HTMLParser`):** `_TableAwareHTMLParser` converts HTML documents (`fee-structure-page.html`, `grading-page.html`) into structured text, mapping `<h1>`-`<h6>` tags to Markdown headings and `<table>`/`<tr>`/`<td>` tags into Markdown rows.
- **Markdown Fixtures:** YAML front matter (`--- ... ---`) is parsed and stripped, preserving document metadata (`doc_version`, `superseded`) while preserving section headings.

---

## 2. Cleaning

The cleaning stage (`indexer/pipeline/clean.py`) removes scanning and document artefacts without damaging hierarchical structure:

- **Repeated Headers and Footers:** Identifies lines that repeat in the top 3 or bottom 3 lines across $\ge 50\%$ of pages within a document and removes them.
- **Bare Page Numbers:** Lines matching `^\s*\d{1,4}\s*$` are stripped.
- **Unicode Normalization:** Replaces typographical ligatures (`\ufb01` $\rightarrow$ `fi`, `\ufb02` $\rightarrow$ `fl`), smart/curly quotes (`‘’“”` $\rightarrow$ `'"`), en/em dashes, and non-breaking spaces.
- **De-hyphenation:** Recombines words split across line breaks by hyphens (`word-\ncontinuation` $\rightarrow$ `wordcontinuation`).
- **Whitespace Collapsing:** Collapses multiple consecutive blank lines into clean paragraph breaks.
- **Deliberately Preserved:** Clause numbers (e.g., `1.21`, `5.4`, `5.8`), section headings, and table rows are explicitly protected from being stripped by header/footer filters.

---

## 3. Chunking

The chunking stage (`indexer/pipeline/chunk.py`) uses a recursive, structure-aware splitting strategy:

1. **Heading Detection:** Detects section boundaries using numbered clauses (`^\d+(\.\d+)*\s`), Markdown headings (`^#{1,6}\s`), and major titles.
2. **Recursive Splitting:** Oversized sections are recursively divided into paragraphs (`\n\n`), then sentences, and finally bounded token windows if needed. Section boundaries are never crossed by overlap.
3. **Heading Context:** Every chunk prepends its section heading to preserve standalone semantic context for embeddings and retrieval.
4. **Metadata & Stable IDs:** Every chunk carries `source`, `section`, `page`, `doc_version`, and `superseded`. Chunk IDs are generated deterministically via SHA-1: `sha1(f"{source}|{section}|{chunk_index}")[:16]`.

### Chunking Configuration Measurements

Token count is approximated via whitespace word count. Evaluated configurations against the golden evaluation set:

| Configuration | Chunk Size (words) | Overlap (words) | recall@5 | Storage Amplification | Chunks Stored | Notes |
| --- | --- | --- | --- | --- | --- |
| Small / Fine | 200 | 40 | 0.8125 | 1.48x | 142 | Higher granularity; splits multi-clause policies |
| **Shipped** | **400** | **50** | **0.8750** | **1.22x** | **84** | **Optimal balance of context and specificity** |
| Large / Coarse | 800 | 100 | 0.8125 | 1.15x | 48 | Dilutes specific clause numbers in vector space |
| Baseline Broken | 100 | 0 | 0.5625 | 1.02x | 210 | Arbitrary fixed cutoffs break clauses and context |

*Storage amplification* is measured as total chunked tokens divided by original document tokens ($\approx 1.22\times$ for the shipped 400/50 configuration due to overlap and prepended section headers).

---

## 4. Embedding

- **Model Pinning:** `nomic-embed-text` (768 dimensions), served via Ollama (`/api/embed`).
- **Unified Configuration:** `EMBED_MODEL` is declared once in the `rahbar-config` ConfigMap and shared by both the indexer Job and the API Deployment.
- **Batching & Resiliency:** Chunks are embedded in batches of 32 with a 60-second HTTP timeout and exponential backoff retry.
- **Guardrail Enforcement:** The indexer writes `embed_model` and `dimension` into collection metadata. The API validates this metadata during startup and in the `/readyz` probe, refusing to serve traffic if there is a mismatch.

---

## 5. Storage & Zero-Downtime Re-indexing

Storage (`indexer/pipeline/store.py`) uses a blue/green collection strategy against ChromaDB:

1. A new collection is created: `rahbar-g{timestamp}`.
2. Chunks and embeddings are written in batches of 100.
3. Once populated and verified, the pointer collection `rahbar-active` is atomically updated with metadata `{"active": "rahbar-g{timestamp}", "generation": timestamp}`.
4. Old generations (beyond the immediate predecessor kept for rollback) are safely deleted.
5. The API resolves the active collection through this pointer, eliminating downtime and empty collection states during re-indexing.

---

## 6. Evaluation Methodology & Matching Rules

Evaluation (`eval/run_eval.py`) scores system responses against `eval/golden.yaml` across 18 curated questions:

### Section-Matching Rule
When comparing a retrieved passage's `section` against a golden relevant item `(source, section)`:
- The `source` must match exactly.
- The retrieved section matches the golden section if:
  1. It is an exact string match (`golden_section == retrieved_section`), **OR**
  2. The retrieved section starts with the golden section followed immediately by a non-digit character (e.g. whitespace, punctuation).

> **Example:** Golden section `"5.4"` matches `"5.4 Academic Warning"` or `"5.4 - Warning"`, but does **not** match `"5.41"` or `"5.4.1"`.

### Metrics & Gating
- **`recall@k`:** Fraction of required `(source, section)` relevant targets retrieved within the top $k$ candidates.
- **`citation_validity`:** Fraction of citations in the generated answer that match an actual retrieved passage.
- **`abstention_correctness`:** Ensures $100\%$ of `must_abstain` queries (hostel rules) return `answered_from_context: false`.
- **CI Gate:** Automated failure if `recall@5` drops by more than $0.05$ against baseline, citation validity $< 95\%$, or any `must_abstain` question is answered from context.
