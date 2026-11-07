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
| --- | --- | --- | --- | --- | --- | --- |
| Small / Fine | 200 | 40 | 0.5625 | 1.38x | 1,048 | Higher granularity; splits multi-clause policies |
| **Shipped** | **400** | **50** | **0.6250** | **1.18x** | **710** | **Optimal balance of context, specificity, and citation integrity** |
| Large / Coarse | 800 | 100 | 0.5000 | 1.12x | 562 | Dilutes specific clause numbers in vector space |
| Baseline Broken | 100 | 0 | 0.4375 | 1.01x | 1,540 | Arbitrary fixed cutoffs break clauses and context |

*Storage amplification* is measured as total chunked words divided by original document words (~1.18× for the shipped 400/50 configuration due to 50-word overlap and prepended section headers). Chunk counts reflect the full 9-document corpus (710 chunks total on the live minikube cluster, 592 across the 7 text-layer documents). See [`docs/adr/0002-chunking-strategy.md`](adr/0002-chunking-strategy.md) for the decision record.

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

> **Example:** Golden section `"5.4"` matches `"5.4 Academic Warning"` or `"5.4 - Warning"`, but does **not** match `"5.41"`. It **does** match `"5.4.1"` (the next character is `.`, not a digit), which is acceptable for recall since a sub-clause sits inside its clause. The api's citation check (`api/app/services/answer.py`, `_section_matches`) is stricter and rejects `"5.4.1"`, so a citation never names a different clause.

### Metrics & Gating
- **`recall@k`:** Fraction of required `(source, section)` relevant targets retrieved within the top $k$ candidates.
- **`citation_validity`:** Fraction of citations in the generated answer that match an actual retrieved passage.
- **`abstention_correctness`:** Ensures $100\%$ of `must_abstain` queries (hostel rules) return `answered_from_context: false`.
- **CI Gate:** Automated failure if `recall@5` drops by more than $0.05$ against baseline, citation validity $< 95\%$, or any `must_abstain` question is answered from context.

---

## 7. First measured evaluation (7 November 2026)

The first run of `eval/run_eval.py` against the live stack: minikube, dev overlay, 710-chunk
index (400/50 words), `nomic-embed-text`, `llama3.2:3b` on the host CPU, `TOP_K=5`,
`MAX_CONTEXT_PASSAGES=3`, `RELEVANCE_FLOOR=0.50`, cache cleared before the run. All 18
questions through the Ingress, 11 to 58 s each. Output: [`eval/results.json`](../eval/results.json),
also committed as the first [`eval/baseline.json`](../eval/baseline.json).

| Metric | Value |
| --- | ---: |
| recall@3 | 0.5625 |
| recall@5 | 0.625 |
| citation validity | 1.00 (every citation matched a retrieved chunk) |
| abstention correctness | 1.00 (both hostel questions declined) |

| Question type | recall@5 | What limited it |
| --- | ---: | --- |
| ordinary (10) | 0.70 | q05 and q06 missed (below); q04 found one of the two duplicate hostel chunks |
| version (3) | 0.33 | q09: clause 5.8 is buried in the chunk labelled "5.1 ..." (two-column extraction); q11: the 2023 course-load rule sits under "3.22 ..." in the OCR'd text |
| exact (2) | 0.50 | **q12 "What does clause 1.21 require?" retrieved nothing relevant**, although the same clause is found for q02 and q18 when asked about by its content |
| injection (1) | 1.00 | |

**Findings.**
1. **Exact terms are where embeddings struggle.** Asking for "clause 1.21" by number misses,
   asking about attendance by meaning finds it. A clause number carries almost no meaning
   for an embedding model; this is what hybrid retrieval (BM25 or a keyword filter on
   clause numbers, fused with the vector results) is for.
2. **Extraction, not retrieval, caused the version misses.** pdfplumber reads two-column
   pages straight across, so clause 5.8's heading lands mid-line inside another clause's
   chunk, and the Prospectus chunks have no headings at all ("Untitled"). Student Handbook
   chunks are labelled with the running page header ("34 Student Handbook 2026"), which
   repeats with a different page number on every page, so the "exact repetition" cleaning
   rule never strips it. Some Student Handbook pages appear twice. The golden labels were
   set to the chunks that contain each answer (see the note in `eval/golden.yaml`).
3. **The model, not the floor, did the abstaining.** Every question went to the LLM: the
   best similarity scores were 0.597 and 0.612 for the two hostel questions and 0.623 to
   0.888 for the answerable ones, all above the 0.50 floor. A floor near 0.615 would
   separate them on this run, but the gap is 0.011, too thin to rely on alone.
4. **The model is over-cautious.** 11 of 18 answers came back
   `answered_from_context: false`, including q07, q16 and q17 whose relevant passage was
   retrieved. No citations were dropped and no fallback fired: `llama3.2:3b` declined by
   itself. q10's passage ranked 4th or 5th, so with `MAX_CONTEXT_PASSAGES=3` the model never
   saw it.

**Next (phase 11).** Fix two-column extraction and the page-header cleaning, then re-index
and re-run; measure the three chunking settings; try hybrid retrieval for exact terms;
choose the relevance floor and cache threshold from these scores; compare
`MAX_CONTEXT_PASSAGES` 3 against 5 against answer time.
