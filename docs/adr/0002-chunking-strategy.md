# ADR 0002: Structure-aware recursive chunking (400 words, 50-word overlap)

## Status
Accepted · 7 November 2026 · measurements replaced with the offline sweep on 8 November 2026

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

Measured with [`eval/chunk_sweep.py`](../../eval/chunk_sweep.py), which runs our own extract, clean and chunk stages inside the indexer image (with OCR), embeds with nomic-embed-text and ranks by cosine with the api's superseded filter, scoring with `eval/run_eval.py`'s `recall_at_k` over the 16 golden questions that have relevant passages. Its 400/50 row reproduces the live minikube evaluation exactly (710 chunks, recall@3 0.5625, recall@5 0.625), so the other rows are comparable. Output: [`docs/evidence/chunk-sweep.txt`](../evidence/chunk-sweep.txt).

| Size / overlap (words) | Chunks | Amplification | recall@3 | recall@5 | Hostel (must-abstain) best scores |
| --- | ---: | ---: | ---: | ---: | --- |
| 100 / 0 | 1,438 | 1.105× | 0.6250 | **0.6875** | 0.642 / 0.620 |
| 200 / 40 | 1,005 | 1.284× | 0.5938 | 0.6562 | 0.612 / 0.609 |
| **400 / 50 (shipped)** | **710** | **1.165×** | **0.5625** | **0.6250** | **0.612 / 0.597** |
| 800 / 100 | 588 | 1.139× | 0.5000 | 0.5625 | 0.612 / 0.597 |

Amplification is the number of words stored in chunks divided by the words in the cleaned pages (96,586). It stays above 1 even with no overlap, because every chunk repeats its heading.

**Why we kept 400/50 although smaller chunks score higher.**
1. **The gains are two questions.** 14 of the 16 questions score the same in every configuration. Both gains are questions with two relevant labels, where smaller chunks fit the second label into the top 5: q04 (two Student Handbook pages about hostels) at 200/40 and 100/0, and q18 at 100/0. 800/100 loses q08.
2. **100/0 breaks abstention.** Its hostel questions score 0.642 and 0.620, above the weakest answerable question (q02, 0.6225), so no relevance floor could separate them. At 400/50 and 200/40 the hostel scores stay below q02.
3. **Generation was not measured per configuration.** The model reads only `MAX_CONTEXT_PASSAGES=3` passages, so 100-word chunks would give it about 300 words to answer from. A live evaluation with the LLM takes about an hour on our CPU, and we ran it only for 400/50.

**200/40 is the open candidate:** one more question found (q04), nothing lost, hostel scores still below q02, at the cost of 42% more chunks. Switching to it needs a re-index, a full live evaluation and a new `eval/baseline.json`. We did not do that before the deadline.

## Consequences

- **What this makes easier:**
  - Every chunk carries a clause or heading label, so citations can be checked: citation validity was 1.00 on the live evaluation (`eval/results.json`).
  - At 710 chunks, the live re-index takes 500 s on our laptop (most of it embedding).
- **What this makes harder:**
  - Section labels are only as good as extraction. Two-column pages in `academic-rules-handbook-jun-2026.pdf` bury clause 5.8 inside the chunk labelled 5.1. Student Handbook chunks take the running page header ("34 Student Handbook 2026") as their label, and Prospectus chunks often have none. Recall is limited by these problems, not by the chunk size: q05, q06, q09, q11 and q12 miss in every configuration.
- **What we watch for:**
  - Re-run `eval/chunk_sweep.py` whenever the extractor, cleaner or golden set changes; the six runs took 23 minutes on our laptop, starting from an empty embedding cache.
  - 200/40, measured end to end with the LLM before switching.
