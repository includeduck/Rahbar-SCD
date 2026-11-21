# Batch B: documents with planted problems (Assignment 03, Part A)

`python corpus/batch_b.py --root <corpus folder>` builds `<root>/incoming/batch-b/` from the
documents written here and from copies of the fetched corpus (so no third-party document is
committed). Every document written here is a synthetic test document, not an official
FAST-NUCES document, and says so in its text.

| File in the batch | Planted problem | Expected check |
| --- | --- | --- |
| `grading-copy.html` | exact duplicate of the Grading page | uniqueness (SHA-256) |
| `fee-structure-copy.html` | exact duplicate of the Fee Structure page | uniqueness (SHA-256) |
| `grading-near-duplicate.html` | the Grading page with one sentence added | uniqueness (embedding) |
| `academic-rules-truncated.pdf` | first 35% of the Academic Rules Handbook | completeness (truncated) |
| `untitled-notice.md` | no title, no date or version anywhere | completeness |
| `garbled-scan.txt` | unreadable OCR output | quality |
| `fee-structure-2027-28.md` | a newer version of the Fee Structure page | recency: archives the 2026-27 page |
| `library-services-guide.md` | contains an injected instruction | none: verification passes it; the retrieval guardrail (Part D) must flag it |
| `exam-hall-rules.md` | none (a clean new document) | passes |
| `course-withdrawal-faq.md` | none (a clean new document) | passes |
