# ADR 0005: Verified, incremental ingestion

## Status
Accepted · 21 November 2026 (Assignment 03, Part A)

## Context

Until now the indexer rebuilt the whole index on every run: extract, chunk and embed all nine
documents into a new collection, then flip a pointer (ADR 0001). That keeps the api serving
during a rebuild, but every run embeds every chunk (678 of them) even when nothing changed, and
nothing checks a document before it is indexed. Assignment 03 asks for the data-management
lifecycle of the RAGOps paper (sections 4.2.1 to 4.2.4): verify each document, then update the
data lake and the retrieval source incrementally. An unchanged corpus must embed zero chunks, a
changed document must replace only its own chunks, a deleted one must leave no orphans, and an
old version must be archived, excluded from retrieval and restorable.

## Decision

- **Verify after extraction, before chunking** (`indexer/pipeline/verify.py`). Four checks per
  document, each pass or fail with a reason: quality, completeness, recency, uniqueness. A
  failed quality, completeness or uniqueness check moves the file to `quarantine/<run>/` and it
  is not indexed. A failed recency check alone archives the document: it stays indexed with
  `superseded: true`, which retrieval already excludes unless the question asks for an older
  version. Thresholds were measured on the corpus and Batch B
  (`docs/evidence/verification-thresholds.txt`).
- **One long-lived collection per pipeline configuration**, named from the embedding model and
  the chunk settings (`rahbar-nomic-embed-text-c400o50`). A different configuration is a
  different collection, which is what a release candidate needs (Part C).
- **A ledger on the volume** (`state/ledger.json`) records each document's SHA-256, version,
  family, chunk count and status. Chunk ids derive from document id, file hash and position, so
  an unchanged file maps to the same chunks and is skipped; a changed file's old chunks are
  deleted by `doc_id` before the new ones are added; a document gone from the input folders has
  every chunk with its `doc_id` deleted.
- **Archive and restore flip metadata, never re-embed.** `python -m pipeline.lake restore
  --family F --version V` makes an older version current again and pins it, so the next run
  does not archive it again. Files are kept under `lake/` and `archive/`, never deleted.
- **Families** (`family:` in `corpus/sources.yaml` or a batch's `_index.json`) say which
  documents are versions of one another; without one, each document is its own family.

## Consequences

- An unchanged rerun embeds 0 chunks, but still extracts and OCRs every document before
  verification can compare hashes (about 5 minutes on this laptop). Comparing the raw file hash
  before extraction would remove that cost; we kept one code path instead.
- There is no blue/green window any more: changes land in the live collection document by
  document. A reader can briefly see a changed document with no chunks between its delete and
  its add. Acceptable for a corpus this size; `--full` still does the old blue/green rebuild.
- The paper's fifth check, consistency (contradictions between documents), is not implemented.
- A newer version that fails verification removes the indexed old version of the same document
  until a good one arrives (fail closed).
