# Corpus

Source: **FAST-NUCES public website** (nu.edu.pk), public pages only · Documents: **9** ·
Pages: **279 PDF pages + 2 HTML pages**

We do not commit the documents. `corpus/sources.yaml` lists them, `corpus/fetch.py` downloads
them one at a time into `corpus/raw/` (gitignored), and `corpus/MANIFEST.md` records each URL and
its SHA-256. Reproduce and check with:

```bash
make fetch                                   # downloads what is missing into corpus/raw/
python corpus/fetch.py --verify              # re-hashes every file against MANIFEST.md
```

All nine were fetched and checked on **2 November 2026**.

## Documents

| Document | `doc_version` | `superseded` | Pages | Text layer | Main role |
| --- | --- | --- | ---: | --- | --- |
| Undergraduate Academic Rules (Aug 2023) | 2023-08 | **true** | 26 | **2 of 26 pages** (cover, back page) | Old side of the version pair; second OCR case |
| Academic Rules Handbook (Jun 2026) | 2026-06 | false | 58 | yes (5 cover/divider pages empty) | Current rules, numbered clauses |
| Prospectus 2026: Academic Rules and Regulations | 2026 | false | 5 | yes | Same rules, different clause numbers |
| Student Handbook (Jun 2026) | 2026-06 | false | 64 | yes (4 pages empty) | Long definitions (Generative AI policy) |
| Annual Report 2019-20 | 2019-20 | **true** | 33 | yes | Older edition of the report |
| Annual Report 2023-24 | 2023-24 | false | 85 | yes | Tables (faculty by campus and department) |
| Fee Structure page | 2026-27 | false | HTML | yes | Fee tables, exact amounts |
| Grading page | 2026 | false | HTML | yes | Grade-point table |
| Faculty Entrepreneurship and Technical Start-ups Policy (ORIC) | 2022-05 | false | 8 | **none** | Scanned, image-only PDF |

## The seven required properties

| # | Required property | Document(s) that provide it | Evidence or notes |
| --- | --- | --- | --- |
| 1 | At least 6 documents, 40 pages | All nine above | 9 documents; 279 PDF pages plus two HTML pages. The two handbooks alone are 122 pages. |
| 2 | Two versions of one policy | Undergraduate Academic Rules **Aug 2023** (superseded) and Academic Rules Handbook **Jun 2026** (current) | The 2026 handbook replaces the 2023 rules. `sources.yaml` sets `superseded: true` on the 2023 file; `fetch.py` copies it into `corpus/raw/_index.json`, and the indexer writes `doc_version` and `superseded` onto every chunk, so the retriever can exclude the old rules by default. Annual Report 2019-20 is also marked superseded by the 2023-24 edition, because whole passages repeat word for word. |
| 3 | Scanned, image-only PDF | ORIC Faculty Entrepreneurship and Technical Start-ups Policy | pypdf finds no fonts on any of its 8 pages, one image per page, and extracts 0 characters. `pdffonts` output (an empty font list) still to be pasted here from WSL. The Aug 2023 rules are a second, harder case: 24 of 26 pages have no text layer. |
| 4 | A table with real information | Fee Structure page; Grading page; Annual Report 2023-24 | Fee page: 22 table rows (tuition per credit hour, other fees). Grading page: 38 rows (grade, points, meaning). Annual Report 2023-24 p13: faculty by campus and rank, which pdfplumber returns as 26 columns, most of them empty merged-cell fragments. |
| 5 | Long definitions | Student Handbook 2026, section XII "Policy on Generative AI Based Tools" (pp. 48–62) | Numbered sub-sections (1. Introduction, 3. Guidelines for Use, ...) whose paragraphs run well past one chunk, so a fixed-size chunker cuts them mid-definition. |
| 6 | A deliberately absent topic | **Hostel rules**: we leave out the prospectus's "Transport and Hostel Facilities" PDF | See the next section. |
| 7 | Exact-term content | Handbook 2026, Prospectus 2026, Fee and Grading pages | Clause **1.21** (100 % attendance; up to 20 % absence condonable by the HoD), handbook p10. Clause **5.4** (minimum CGPA **2.00** to avoid academic warning), p24. Attendance is clause **1** in the Prospectus. BS tuition **Rs. 12,000** per credit hour. Grade points (A 4.00, A- 3.67, B+ 3.33, ...). |

## The absent topic: hostel rules

A student would plausibly ask how to get a hostel room, what it costs, or what the curfew is.
None of our documents answers that, because the one document that does (the prospectus's
"Transport and Hostel Facilities" PDF) is deliberately not in `sources.yaml`.

Two near misses make this a real test of abstention rather than an easy one:

- The Student Handbook (p44) says in-campus hostels exist only at the Chiniot-Faisalabad and
  Peshawar campuses, and both annual reports describe the Chiniot-Faisalabad hostels. So
  "Which campuses have hostels?" is **answerable**, and hostel-related chunks will score
  close to our relevance floor on hostel-rules questions.
- **Transport is not absent.** Both annual reports describe the bus service at each campus
  (Annual Report 2023-24 p78). We first considered transport as a second absent topic and
  dropped it after checking.

Our must-abstain golden questions therefore ask about hostel *rules* (application, fees,
conduct), never about whether hostels exist.

## Traps we expect

- **Outdated source (failure mode E).** The 2023 and 2026 rules disagree, and the 2023 text
  is what an unfiltered retriever might return. Our version-sensitive golden questions check
  that the current rule wins unless the user asks about the older one.
- **Same rule, two numbers.** Attendance is clause 1.21 in the handbook and clause 1 in the
  Prospectus. A citation must name the right document as well as the right clause.
- **OCR dependence.** Because the old side of the version pair is mostly image-only, questions
  about the 2023 rules depend on OCR quality. If OCR on that file turns out unusable, we will
  document the loss in `docs/PIPELINE.md` and use the two Annual Report editions as the
  version pair instead.

## Still to do before submission

- [ ] Paste `pdffonts` output for the ORIC policy and the Aug 2023 rules (run in WSL).
- [ ] Record OCR quality on the Aug 2023 rules, with one before-and-after sample.
- [ ] Confirm the exact `section` labels our chunker produces for the clauses above, so
      `eval/golden.yaml` matches them.
