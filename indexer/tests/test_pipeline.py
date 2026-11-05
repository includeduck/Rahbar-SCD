"""Tests for the indexer pipeline: clean, chunk, store."""

from __future__ import annotations


from pipeline.clean import clean
from pipeline.chunk import chunk
from pipeline.config import get_settings
from pipeline.models import Chunk, Document, Page
from pipeline.store import POINTER_COLLECTION, store


# ---------------------------------------------------------------------------
# Fake Chroma client for store tests
# ---------------------------------------------------------------------------

class FakeCollection:
    def __init__(self, name: str, metadata: dict) -> None:
        self.name = name
        self.metadata = metadata
        self.rows: dict[str, tuple] = {}

    def add(self, ids: list, documents: list, embeddings: list, metadatas: list) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.rows[i] = (d, e, m)

    def count(self) -> int:
        return len(self.rows)

    def modify(self, metadata: dict) -> None:
        self.metadata.update(metadata)


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def create_collection(self, name: str, metadata: dict) -> FakeCollection:
        self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def get_or_create_collection(self, name: str, metadata: dict) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        if name in self.collections:
            del self.collections[name]
        else:
            raise ValueError(f"no such collection: {name}")

    def list_collections(self) -> list[FakeCollection]:
        return list(self.collections.values())


# ---------------------------------------------------------------------------
# Clean tests
# ---------------------------------------------------------------------------

class TestClean:
    def test_strips_repeated_headers(self) -> None:
        """Lines that repeat on >= 50% of pages should be removed."""
        pages = [
            Page(source="test.pdf", page=i, text=f"HEADER LINE\nContent page {i}\nFOOTER LINE")
            for i in range(1, 5)
        ]
        doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=pages)
        cleaned = clean(doc)
        for page in cleaned.pages:
            assert "HEADER LINE" not in page.text
            assert "FOOTER LINE" not in page.text
            assert "Content page" in page.text

    def test_keeps_headings(self) -> None:
        """Headings (numbered clauses, Markdown #) should be preserved."""
        page = Page(
            source="test.pdf", page=1,
            text="1.21 Attendance\nStudents must attend 100% of classes.\n# Section Title",
        )
        doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=[page])
        cleaned = clean(doc)
        assert "1.21 Attendance" in cleaned.pages[0].text
        assert "# Section Title" in cleaned.pages[0].text

    def test_removes_bare_page_numbers(self) -> None:
        page = Page(source="test.pdf", page=1, text="Some text\n42\nMore text")
        doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=[page])
        cleaned = clean(doc)
        lines = cleaned.pages[0].text.splitlines()
        assert not any(line.strip() == "42" for line in lines)

    def test_normalises_unicode(self) -> None:
        page = Page(source="test.pdf", page=1, text="The oﬃce of ﬁnance\u2019s policy")
        doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=[page])
        cleaned = clean(doc)
        assert "office" in cleaned.pages[0].text
        assert "finance" in cleaned.pages[0].text
        assert "\u2019" not in cleaned.pages[0].text

    def test_keeps_clause_numbers(self) -> None:
        """Clause numbers like 5.4 should survive cleaning."""
        page = Page(source="test.pdf", page=1, text="5.4 Academic Warning\nCGPA below 2.00")
        doc = Document(source="test.pdf", doc_version="2026", superseded=False, pages=[page])
        cleaned = clean(doc)
        assert "5.4 Academic Warning" in cleaned.pages[0].text


# ---------------------------------------------------------------------------
# Chunk tests
# ---------------------------------------------------------------------------

class TestChunk:
    def _make_doc(self, text: str, source: str = "test.pdf") -> Document:
        return Document(
            source=source, doc_version="2026", superseded=False,
            pages=[Page(source=source, page=1, text=text)],
        )

    def test_basic_chunking(self) -> None:
        doc = self._make_doc("## 1. First section\nSome text here about rules.")
        chunks = chunk(doc, size=400, overlap=50)
        assert len(chunks) >= 1
        assert chunks[0].source == "test.pdf"
        assert chunks[0].doc_version == "2026"
        assert chunks[0].superseded is False

    def test_respects_size_limit(self) -> None:
        # Create a document with a lot of text in one section
        words = " ".join(f"word{i}" for i in range(200))
        doc = self._make_doc(f"## 1. Section\n{words}")
        chunks = chunk(doc, size=50, overlap=10)
        for c in chunks:
            # Each chunk should be roughly within the size limit
            # (allowing for heading prefix)
            word_count = len(c.text.split())
            assert word_count <= 70  # size + heading + tolerance

    def test_section_boundaries(self) -> None:
        """Chunks should not cross section boundaries."""
        doc = self._make_doc(
            "## 1. First\nContent of section one.\n\n"
            "## 2. Second\nContent of section two."
        )
        chunks = chunk(doc, size=400, overlap=50)
        # Should get at least 2 chunks (one per section)
        sections = {c.section for c in chunks}
        assert len(sections) >= 2

    def test_metadata_fields(self) -> None:
        doc = self._make_doc("## 1. Section\nSome text.")
        chunks = chunk(doc, size=400, overlap=50)
        assert len(chunks) >= 1
        c = chunks[0]
        # All 5 required metadata fields
        meta = c.metadata()
        assert "source" in meta
        assert "section" in meta
        assert "page" in meta
        assert "doc_version" in meta
        assert "superseded" in meta

    def test_stable_ids(self) -> None:
        """Same input should produce the same chunk IDs."""
        doc = self._make_doc("## 1. Section\nSome text.")
        chunks1 = chunk(doc, size=400, overlap=50)
        chunks2 = chunk(doc, size=400, overlap=50)
        assert [c.id for c in chunks1] == [c.id for c in chunks2]

    def test_overlap(self) -> None:
        """When chunks are split, there should be overlap between them."""
        words = " ".join(f"word{i}" for i in range(100))
        doc = self._make_doc(f"## 1. Section\n{words}")
        chunks = chunk(doc, size=30, overlap=10)
        if len(chunks) >= 2:
            # The end of chunk N should overlap with the start of chunk N+1
            # (After the section heading prefix)
            words_0 = chunks[0].text.split()
            words_1 = chunks[1].text.split()
            # Some words at the end of chunk 0 should appear at the start of chunk 1
            overlap_found = any(w in words_1[:20] for w in words_0[-15:])
            assert overlap_found, "Expected overlap between consecutive chunks"


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------

class TestStore:
    def _make_chunks_and_vectors(self, n: int = 5) -> tuple[list[Chunk], list[list[float]]]:
        chunks = [
            Chunk(
                id=f"chunk-{i}", text=f"Text {i}",
                source="test.pdf", section=f"Section {i}",
                page=1, doc_version="2026", superseded=False,
            )
            for i in range(n)
        ]
        vectors = [[float(i)] * 768 for i in range(n)]
        return chunks, vectors

    def test_writes_embed_model_and_dimension(self) -> None:
        """The collection metadata must record embed_model and dimension."""
        chunks, vectors = self._make_chunks_and_vectors()
        settings = get_settings()
        client = FakeClient()

        # Monkey-patch get_client to return our fake
        import pipeline.store as store_mod
        original = store_mod.get_client
        store_mod.get_client = lambda s: client
        try:
            count = store(chunks, vectors, settings)
        finally:
            store_mod.get_client = original

        assert count == 5
        # Find the generation collection
        gen_cols = [c for name, c in client.collections.items() if name.startswith("rahbar-g")]
        assert len(gen_cols) >= 1
        col = gen_cols[0]
        assert col.metadata["embed_model"] == settings.embed_model
        assert col.metadata["dimension"] == 768

    def test_flips_pointer(self) -> None:
        """After storing, the pointer collection should point to the new generation."""
        chunks, vectors = self._make_chunks_and_vectors()
        settings = get_settings()
        client = FakeClient()

        import pipeline.store as store_mod
        original = store_mod.get_client
        store_mod.get_client = lambda s: client
        try:
            store(chunks, vectors, settings)
        finally:
            store_mod.get_client = original

        assert POINTER_COLLECTION in client.collections
        pointer = client.collections[POINTER_COLLECTION]
        assert "active" in pointer.metadata
        assert pointer.metadata["active"].startswith("rahbar-g")

    def test_all_chunks_stored(self) -> None:
        """Every chunk should be in the collection."""
        chunks, vectors = self._make_chunks_and_vectors(10)
        settings = get_settings()
        client = FakeClient()

        import pipeline.store as store_mod
        original = store_mod.get_client
        store_mod.get_client = lambda s: client
        try:
            count = store(chunks, vectors, settings)
        finally:
            store_mod.get_client = original

        assert count == 10
        gen_cols = [c for name, c in client.collections.items() if name.startswith("rahbar-g")]
        assert gen_cols[0].count() == 10
