# ADR 0002: Structure-aware recursive chunking (400 words, 50-word overlap)

## Status
Accepted · 7 November 2026

## Context

University regulatory documents (such as FAST-NUCES academic handbooks, fee schedules, and grading policies) are hierarchically structured around numbered clauses (e.g. `1.21`, `5.4`, `5.8`), section titles, and tables. Retrieval quality and answer grounding in RAHBAR depend critically on how these documents are divided into chunks:

1. **Fixed-token chunking destroys clause boundaries.** Cutting text at fixed 100-token boundaries slices clauses in the middle of sentences and severs clause numbers from the text of the rule. When an answer is generated, the model cannot cite the true section, and citations fail the validity check.
2. **Coarse chunking dilutes specific clauses in vector space.** Packing 800 words into a single chunk bundles multiple distinct policy rules into one embedding vector. The embedding of a specific query (e.g. `"What CGPA triggers academic warning?"`) is diluted by the surrounding text, lowering similarity ranks below top-$k$.
3. **Storage amplification must remain bounded.** Chunk overlap and prepended headers duplicate text across chunks. We need an overlap that prevents context loss at chunk boundaries without ballooning storage or indexer embedding latency.

## Decision

We implement a **structure-aware recursive chunking strategy** in `indexer/pipeline/chunk.py`, configured with a target size of **400 words** and an overlap of **50 words**:

1. **Heading detection:** Sections are identified using numbered clauses (`^\d+(\.\d+)*\s`), Markdown headers (`^#{1,6}\s`), and capitalized policy titles.
2. **Recursive splitting:** Content within a section is split hierarchically: first by paragraph breaks (`\n\n`), then by sentences (`[.!?]\s+`), and finally by whitespace-bounded token windows if an individual sentence exceeds `chunk_size`.
3. **Bounded overlap within sections:** An overlap of 50 words is applied between sequential chunks of the same section. Overlap **never crosses section boundaries**, preventing rules from bleeding into neighboring policies.
4. **Prepended section context:** Every chunk prepends its section heading to its text: `f"{section}\n\n{chunk_text}"`. This ensures that even standalone chunks retain their hierarchical meaning during vector embedding.
5. **Deterministic SHA-1 chunk IDs:** Chunk identifiers are computed deterministically as `sha1(f"{source}|{section}|{chunk_index}")[:16]`, ensuring stable chunk identities across re-indexing runs.
6. **Metadata provenance:** Every chunk records `source`, `section`, `page`, `doc_version`, and `superseded` in ChromaDB metadata.

## Options considered and evaluated

We evaluated four configurations against the 18-question golden evaluation set on the real FAST-NUCES corpus (89,655 document words across 9 documents):

| Configuration | Chunk Size (words) | Overlap (words) | recall@5 | Storage Amplification | Chunks Stored | Evaluation Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Small / Fine | 200 | 40 | 0.5625 | 1.38x | 1,048 | Over-fragmentation; splits multi-clause policies across chunk boundaries. |
| **Shipped** | **400** | **50** | **0.6250** | **1.18x** | **710** | **Highest recall@5, clean section preservation, optimal context window.** |
| Large / Coarse | 800 | 100 | 0.5000 | 1.12x | 562 | Dilutes clause numbers in vector space; top-k hits contain too much irrelevant prose. |
| Baseline Broken | 100 | 0 | 0.4375 | 1.01x | 1,540 | Fixed window splits sentences; headings severed from clauses; citation validity collapses. |

## Consequences

- **What this makes easier:**
  - Optimal retrieval performance: `recall@5` reaches 0.6250 on the real corpus, outperforming all other tested configurations.
  - Citation validity: Because section headers are prepended to chunk text and recorded in metadata, generated citations match retrieved passages with 100% validity (`eval/results.json`).
  - Storage amplification is kept to a modest 1.18× (105,790 chunk words vs. 89,655 source words).
- **What this makes harder:**
  - Documents with multi-column layouts (such as `prospectus-2026-academic-rules-and-regulations.pdf`) occasionally interleave headings when extracted line-by-line; these require column-aware text cleaning in `indexer/pipeline/clean.py`.
- **What we watch for:**
  - If university rules are updated with larger tabular sections, tables should be converted to Markdown rows during extraction so table rows are not split mid-record.
