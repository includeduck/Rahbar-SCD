"""Assignment 03, Part A: verification (A1) and incremental update (A2).

The real extract, clean, verify and chunk stages run on small Markdown documents; Chroma and
the embedding model are replaced by in-memory fakes that count what they are asked to do.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import pipeline.embed as embed_mod
import pipeline.store as store_mod
from pipeline.config import IndexerSettings
from pipeline.lake import Ledger, cmd_restore
from pipeline.run import run_pipeline


class FakeCollection:
    def __init__(self, name: str, metadata: dict[str, Any] | None) -> None:
        self.name, self.metadata = name, dict(metadata or {})
        self.rows: dict[str, dict[str, Any]] = {}

    def _match(self, meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
        return all(meta.get(k) == v for k, v in (where or {}).items())

    def get(self, ids: list[str] | None = None, where: dict[str, Any] | None = None,
            include: list[str] | None = None) -> dict[str, Any]:
        keys = [i for i in self.rows if (ids is None or i in ids)
                and self._match(self.rows[i]["meta"], where)]
        return {"ids": keys, "metadatas": [dict(self.rows[i]["meta"]) for i in keys]}

    def add(self, ids: list[str], documents: list[str], embeddings: list[list[float]],
            metadatas: list[dict[str, Any]]) -> None:
        for i, doc, vec, meta in zip(ids, documents, embeddings, metadatas):
            assert i not in self.rows, f"duplicate id {i}"
            self.rows[i] = {"doc": doc, "vec": vec, "meta": dict(meta)}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self.rows.pop(i, None)

    def update(self, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        for i, meta in zip(ids, metadatas):
            self.rows[i]["meta"] = dict(meta)

    def count(self) -> int:
        return len(self.rows)

    def modify(self, metadata: dict[str, Any]) -> None:
        self.metadata = dict(metadata)


class FakeChroma:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, name: str, metadata: dict[str, Any] | None = None
                                 ) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def get_collection(self, name: str) -> FakeCollection:
        return self.collections[name]


class Harness:
    """A corpus folder, a fake Chroma and a counting embedder."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.chroma = FakeChroma()
        self.embedded: list[str] = []
        self.settings = IndexerSettings(
            embed_model="test-embed", chroma_host="x", chroma_port=0, collection="unused",
            corpus_dir=str(root), chunk_size=60, chunk_overlap=10, ollama_url="x")

        def fake_embed(texts: list[str], settings: Any) -> list[list[float]]:
            self.embedded.extend(texts)
            # Similar texts give similar vectors: a bag of hashed words.
            vecs = []
            for text in texts:
                vec = [0.0] * 64
                for word in text.lower().split():
                    vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1.0
                vecs.append(vec)
            return vecs

        monkeypatch.setattr(embed_mod, "embed", fake_embed)
        monkeypatch.setattr(store_mod, "get_client", lambda s: self.chroma)

    @property
    def col(self) -> FakeCollection:
        return self.chroma.collections["rahbar-test-embed-c60o10"]

    def run(self) -> dict[str, Any]:
        self.embedded.clear()
        run_pipeline(self.settings)
        reports = sorted((self.root / "reports").glob("ingest-*.json"))
        return json.loads(reports[-1].read_text())

    def chunk_embeds(self) -> int:
        """Embeddings of chunks only (fingerprints are one text per document, longer)."""
        return len(self.embedded)

    def chunks_of(self, doc_id: str) -> list[str]:
        return self.col.get(where={"doc_id": doc_id})["ids"]


RULES = ("Students must attend every lecture. Absence of up to twenty percent may be condoned "
         "by the head of department for genuine reasons such as illness. ") * 4
FEES = ("The tuition fee is twelve thousand rupees per credit hour for undergraduate "
        "programmes, payable before the semester starts. ") * 4
GRADES = ("A grade of B plus is worth three point three three grade points, and the "
          "cumulative grade point average decides academic standing. ") * 4


def write_doc(folder: Path, name: str, title: str, version: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(f"---\ntitle: {title}\ndoc_version: {version}\n---\n"
                               f"# {title}\n\n{body}\n", encoding="utf-8")


def index(folder: Path, entries: list[dict[str, Any]]) -> None:
    (folder / "_index.json").write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture()
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    write_doc(tmp_path, "rules.md", "Academic Rules", "2026", RULES)
    write_doc(tmp_path, "fees.md", "Fee Structure", "2026", FEES)
    write_doc(tmp_path, "grades.md", "Grading", "2026", GRADES)
    return Harness(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# A2: incremental update
# ---------------------------------------------------------------------------

def test_first_run_embeds_every_chunk_and_a_rerun_embeds_none(h: Harness) -> None:
    first = h.run()
    assert first["embedded_chunks"] > 0 and first["added"] == 3
    total = h.col.count()
    again = h.run()
    assert again["embedded_chunks"] == 0
    assert again["reused_chunks"] == total and again["unchanged"] == 3
    assert h.chunk_embeds() == 1          # only the dimension probe
    assert h.col.count() == total


def test_a_changed_document_replaces_only_its_own_chunks(h: Harness) -> None:
    h.run()
    fees_before, rules_before = h.chunks_of("fees"), h.chunks_of("rules")
    write_doc(h.root, "fees.md", "Fee Structure", "2026", FEES.replace("twelve", "thirteen"))
    report = h.run()
    assert report["replaced"] == 1 and report["documents"]["replaced"] == ["fees"]
    assert report["embedded_chunks"] == len(h.chunks_of("fees"))
    assert set(h.chunks_of("fees")).isdisjoint(fees_before)       # old chunks gone
    assert h.chunks_of("rules") == rules_before                    # others untouched


def test_a_deleted_document_leaves_no_orphan_chunks(h: Harness) -> None:
    h.run()
    assert h.chunks_of("grades")
    (h.root / "grades.md").unlink()
    report = h.run()
    assert report["documents"]["deleted"] == ["grades"]
    assert h.chunks_of("grades") == []
    assert Ledger(h.root).docs["grades"]["status"] == "deleted"
    expected = sum(e["chunks"] for e in Ledger(h.root).docs.values())
    assert h.col.count() == expected


def test_a_newer_version_archives_the_old_one_and_restore_brings_it_back(
        h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.run()
    batch = h.root / "incoming" / "b"
    write_doc(batch, "fees-2027.md", "Fee Structure 2027", "2027",
              FEES.replace("twelve thousand", "thirteen thousand five hundred") + " Revised.")
    index(batch, [{"file": "fees-2027.md", "name": "Fee Structure 2027", "family": "fees",
                   "doc_version": "2027"}])
    report = h.run()
    assert "fees" in report["documents"]["archived"]
    old = h.col.get(where={"doc_id": "fees"})["metadatas"]
    assert old and all(m["superseded"] for m in old)              # excluded by metadata
    assert (h.root / "archive" / "fees" / "2026" / "fees.md").exists()

    embedded_before = len(h.embedded)
    monkeypatch.setattr("pipeline.lake._collection", lambda s: h.col)
    assert cmd_restore(h.settings, h.root, "fees", "2026") == 0
    assert len(h.embedded) == embedded_before                      # no re-embedding
    assert not any(m["superseded"] for m in h.col.get(where={"doc_id": "fees"})["metadatas"])
    assert all(m["superseded"] for m in h.col.get(where={"doc_id": "fee-structure-2027"})
               ["metadatas"])
    rerun = h.run()                                                # the pin holds
    assert rerun["embedded_chunks"] == 0
    assert rerun["restored"] == 0 and rerun["archived"] == 0       # nothing left to flip
    assert not any(m["superseded"] for m in h.col.get(where={"doc_id": "fees"})["metadatas"])
    reason = verify_rows(h)["fees-2027.md"]["checks"]["recency"]["reason"]
    assert "pinned as current by a restore" in reason


# ---------------------------------------------------------------------------
# A1: verification
# ---------------------------------------------------------------------------

def verify_rows(h: Harness) -> dict[str, dict[str, Any]]:
    report = sorted((h.root / "reports").glob("verify-*.json"))[-1]
    return {r["file"]: r for r in json.loads(report.read_text())["documents"]}


def test_exact_and_near_duplicates_are_quarantined(h: Harness) -> None:
    h.run()
    batch = h.root / "incoming" / "b"
    batch.mkdir(parents=True)
    (batch / "fees-copy.md").write_bytes((h.root / "fees.md").read_bytes())
    write_doc(batch, "grades-near.md", "Grading", "2026", GRADES + " Note.")
    report = h.run()
    rows = verify_rows(h)
    assert rows["fees-copy.md"]["decision"] == "quarantine"
    assert "exact duplicate of fees" in rows["fees-copy.md"]["checks"]["uniqueness"]["reason"]
    assert rows["grades-near.md"]["decision"] == "quarantine"
    assert "near-duplicate of grades" in rows["grades-near.md"]["checks"]["uniqueness"]["reason"]
    assert report["embedded_chunks"] == 0
    assert (h.root / "quarantine").rglob("fees-copy.md.reason.json")
    assert not (batch / "fees-copy.md").exists()                   # moved, not copied


def test_empty_garbled_unversioned_and_truncated_documents_fail(h: Harness) -> None:
    batch = h.root / "incoming" / "b"
    batch.mkdir(parents=True)
    (batch / "empty.md").write_text("---\ntitle: Empty\ndoc_version: 1\n---\n# Empty\n",
                                    encoding="utf-8")
    (batch / "garbled.md").write_text("---\ntitle: G\ndoc_version: 1\n---\n"
                                      + "#%$ 0x1F 9981 @@ ;; 77 [] {} 1234 " * 30,
                                      encoding="utf-8")
    (batch / "untitled.md").write_text(RULES.replace("Students", "Staff"), encoding="utf-8")
    (batch / "cut.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n")
    h.run()
    rows = verify_rows(h)
    assert rows["empty.md"]["checks"]["quality"]["reason"].startswith("empty")
    assert rows["garbled.md"]["checks"]["quality"]["reason"].startswith("garbled")
    reason = rows["untitled.md"]["checks"]["completeness"]["reason"]
    assert "title" in reason and "date or version" in reason
    assert not rows["cut.pdf"]["checks"]["quality"]["pass"] \
        or "truncated" in rows["cut.pdf"]["checks"]["completeness"]["reason"]
    assert all(rows[f]["decision"] == "quarantine"
               for f in ("empty.md", "garbled.md", "untitled.md", "cut.pdf"))


def test_front_matter_without_an_index_and_a_separate_lake_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CI fixtures: no _index.json, front matter only, in a folder the Job cannot write;
    LAKE_DIR points elsewhere. The 2023 edition must stay superseded."""
    docs, lake = tmp_path / "fixtures", tmp_path / "lake"
    docs.mkdir()
    (docs / "rules-2023.md").write_text(
        "---\ndoc_version: 2023\nsuperseded: true\n---\n# Rules (2023)\n\n" + RULES,
        encoding="utf-8")
    (docs / "rules-2026.md").write_text(
        "---\ndoc_version: 2026\nsuperseded: false\n---\n# Rules (2026)\n\n"
        + RULES.replace("twenty", "fifteen"), encoding="utf-8")
    h = Harness(lake, monkeypatch)
    h.settings = IndexerSettings(**{**h.settings.__dict__, "corpus_dir": str(docs),
                                    "lake_dir": str(lake)})
    h.run()
    assert all(m["superseded"] for m in h.col.get(where={"doc_id": "rules-2023"})["metadatas"])
    assert not any(m["superseded"]
                   for m in h.col.get(where={"doc_id": "rules-2026"})["metadatas"])
    assert (lake / "state" / "ledger.json").exists() and not (docs / "state").exists()


def test_a_candidate_ingestion_builds_its_own_collection_and_leaves_live_alone(
        h: Harness) -> None:
    """Part C: other chunk settings, their own LAKE_DIR, UPDATE_POINTER=false. The live
    collection, its ledger and the rahbar-active pointer must not change."""
    h.run()
    live_ids = set(h.col.get()["ids"])
    pointer_before = dict(h.chroma.collections["rahbar-active"].metadata)
    live_ledger = (h.root / "state" / "ledger.json").read_text(encoding="utf-8")
    cand_lake = h.root / "candidates" / "c30o5"
    h.settings = IndexerSettings(**{**h.settings.__dict__, "chunk_size": 30,
                                    "chunk_overlap": 5, "lake_dir": str(cand_lake),
                                    "update_pointer": False})
    h.embedded.clear()
    run_pipeline(h.settings)
    cand = h.chroma.collections["rahbar-test-embed-c30o5"]
    assert cand.count() > len(live_ids)               # smaller chunks, more of them
    assert set(h.col.get()["ids"]) == live_ids
    assert h.chroma.collections["rahbar-active"].metadata == pointer_before
    assert (h.root / "state" / "ledger.json").read_text(encoding="utf-8") == live_ledger
    assert (cand_lake / "state" / "ledger.json").exists()
    # A second candidate run is incremental against its own ledger: nothing re-embedded.
    h.embedded.clear()
    run_pipeline(h.settings)
    assert h.chunk_embeds() == 1                      # only the dimension probe
