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

- **Repeated Headers and Footers:** A line is a running header or footer when its *words* (numbers and punctuation ignored) appear in the top 3 or bottom 3 lines of $\ge 50\%$ of a document's pages; every such line is removed. Comparing words only matters because footers carry the page number: the Student Handbook alternates `Student Handbook 2026 3` and `4 Student Handbook 2026` on facing pages, and pdfplumber also returns the same footer as a table row (`|  | Student Handbook 2026 | 3 |`). All of these reduce to `Student Handbook`. Until 11 November lines were compared exactly and number-led lines were never candidates, so this footer survived on 60 of the handbook's 62 extracted pages and became the section label of most of its chunks; now it is left on 6. A line with no word (`1.21`, `2.00`) is never a candidate, so clause numbers and table values cannot be stripped by this rule.
- **Empty Table Rows:** Rows whose cells are all empty (`|  |  |  |`), which pdfplumber returns for the handbook's footer area, are removed.
- **Bare Page Numbers:** Lines matching `^\s*\d{1,4}\s*$` are stripped.
- **Unicode Normalization:** Replaces typographical ligatures (`\ufb01` $\rightarrow$ `fi`, `\ufb02` $\rightarrow$ `fl`), smart/curly quotes (`‘’“”` $\rightarrow$ `'"`), en/em dashes, and non-breaking spaces.
- **De-hyphenation:** Recombines words split across line breaks by hyphens (`word-\ncontinuation` $\rightarrow$ `wordcontinuation`).
- **Whitespace Collapsing:** Collapses multiple consecutive blank lines into clean paragraph breaks.
- **Deliberately Preserved:** Clause numbers (e.g., `1.21`, `5.4`, `5.8`), section headings, and table rows with any content. A numbered clause can only be removed if the same words repeat at the top or bottom of half the pages, which no clause does.

### Before and after: real samples from the pipeline

Produced on 11 November by running our own `extract` and `clean` stages in the indexer image on the corpus.

| Stage | Before | After |
| --- | --- | --- |
| OCR (Aug 2023 rules, p12, image-only) | Text layer: 0 characters | 1,864 characters; clauses read back, e.g. `3.13 A former student of the university seeking readmission in an ...`, `3.14 A former student who has been on disciplinary warning shall not be ...` |
| Cleaning (Student Handbook p11, first and last lines) | `NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES` / ... / `Student Handbook 2026 3` / `\|  \|  \|  \|` / `\|  \| Student Handbook 2026 \| 3 \|` | `conduct that may lead to the disruption of any academic ...` / ... / `designated waste bins should be used for the purpose.` |
| Cleaning (Student Handbook p12, last lines) | `4 Student Handbook 2026` / `\|  \|  \|  \|` / `\| 4 \| Student Handbook 2026 \|  \|` | `talking; signs; gestures; copying from another` |
| Table (fee page, HTML) | `<tr> <td>BBA / BS</td> <td class="text-center">Rs. 12,000</td> </tr>` | `\| BBA / BS \| Rs. 12,000 \|` (one Markdown row per HTML row, kept in the chunk text) |

The handbook samples also show what cleaning does **not** fix: pdfplumber reads its two-column pages straight across, so `conduct that may lead to the disruption of any academic 11. The students are advised to refrain ...` joins a line from each column (section 7).

---

## 3. Chunking

The chunking stage (`indexer/pipeline/chunk.py`) uses a recursive, structure-aware splitting strategy:

1. **Heading Detection:** Detects section boundaries using numbered clauses (`^\d+(\.\d+)*\s`), Markdown headings (`^#{1,6}\s`), and major titles.
2. **Recursive Splitting:** Oversized sections are recursively divided into paragraphs (`\n\n`), then sentences, and finally bounded token windows if needed. Section boundaries are never crossed by overlap.
3. **Heading Context:** Every chunk prepends its section heading to preserve standalone semantic context for embeddings and retrieval.
4. **Metadata & Stable IDs:** Every chunk carries `source`, `section`, `page`, `doc_version`, and `superseded`. Chunk IDs are generated deterministically via SHA-1: `sha1(f"{source}|{section}|{chunk_index}")[:16]`.

### Chunking Configuration Measurements

Sizes are in words (whitespace tokens). Measured on 8 November with [`eval/chunk_sweep.py`](../eval/chunk_sweep.py): our own extract → clean → chunk stages in the indexer image (with OCR), nomic-embed-text, cosine top-5 with the api's superseded filter, scored by `eval/run_eval.py`'s `recall_at_k` over the 16 golden questions that have relevant passages. The 400/50 row reproduces the live evaluation in section 7 exactly, which shows the offline sweep and the live stack agree. Output: [`docs/evidence/chunk-sweep.txt`](evidence/chunk-sweep.txt) (per question and per document) and `chunk-sweep.json`.

| Size / overlap | Chunks | Amplification | recall@3 | recall@5 | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| 100 / 0 | 1,438 | 1.105× | 0.6250 | 0.6875 | +q04, +q18; hostel questions now outscore the weakest answerable one (0.642 vs 0.6225), so no floor separates them |
| 200 / 40 | 1,005 | 1.284× | 0.5938 | 0.6562 | +q04, nothing lost; the candidate to test end to end |
| **400 / 50 (shipped)** | **710** | **1.165×** | **0.5625** | **0.6250** | Matches the live run |
| 800 / 100 | 588 | 1.139× | 0.5000 | 0.5625 | −q08 |

*Storage amplification* is the number of words stored in chunks divided by the words in the cleaned pages (112,496 / 96,586 for 400/50). The prepended heading keeps it above 1 even with no overlap. Only two questions (q04, q18) change between configurations; five (q05, q06, q09, q11, q12) miss in all of them, because of extraction and labelling problems (section 7), not chunk size. We kept 400/50 for now; [ADR 0002](adr/0002-chunking-strategy.md) explains why and what switching to 200/40 would take.

**Re-measured on 11 November, after the header and footer fix above** (400/50 only): 678 chunks (Student Handbook 85 → 71), 92,195 source words, amplification 1.176×. Retrieval moved for one question: q04's answer passage went from rank 1 to rank 4. Its golden labels named the stripped footer sections, so q04 was relabelled to the chunk that now holds the answer (`eval/golden.yaml`). With labels on the answer chunk, recall@5 is 0.6562 before and after the fix, and recall@3 falls from 0.5938 to 0.5312 (q04 only). The hostel questions still score below the weakest answerable one (0.612 and 0.606 against 0.6225).

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
- **`recall@k` by type:** `per_type` holds recall@5 and `per_type_recall_3` recall@3, for each question type.
- **`injection_resistance`:** For injection questions that list `must_not_contain` phrases (`eval/golden.yaml` q16), the fraction whose answer contains none of them. Answers from the stub provider are counted in `injection_skipped`, not checked: the stub quotes the first retrieved passage word for word, so in CI it repeats the fixture's poisoned clause (`eval/fixtures/academic-rules-2026.md`, 3.2) without obeying anything. A real model is checked in the live eval.
- **CI Gate:** Automated failure if `recall@5` drops by more than $0.05$ against baseline, citation validity $< 95\%$, any `must_abstain` question is answered from context, or (when a real model answered) any injection question is obeyed.

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
   separate them on this run, but the gap is 0.011, too thin to rely on alone. (The floor
   was later raised to 0.58 to decline injection and coercion attempts, which score 0.55 to
   0.56; see `docs/FAILURE-ANALYSIS.md`, stage D.)
4. **The model is over-cautious.** 11 of 18 answers came back
   `answered_from_context: false`, including q07, q16 and q17 whose relevant passage was
   retrieved. No citations were dropped and no fallback fired: `llama3.2:3b` declined by
   itself. q10's passage ranked 4th or 5th, so with `MAX_CONTEXT_PASSAGES=3` the model never
   saw it.

**Next (phase 11).** Fix two-column extraction and the page-header cleaning, then re-index
and re-run; measure the three chunking settings; try hybrid retrieval for exact terms;
choose the relevance floor and cache threshold from these scores; compare
`MAX_CONTEXT_PASSAGES` 3 against 5 against answer time.
