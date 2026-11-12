"""Tests for FR-05 / A4. IDs match the Stage 3 traceability table.

A small fake embedder is used so the tests run offline and fast; the real
MiniLM model is exercised by eval/retrieval_check.py.
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from cloudserve_support.pipeline.retrieve import Retriever, chunk_article

DOCS = Path(__file__).resolve().parents[1] / "data" / "documentation.json"
pytestmark = pytest.mark.skipif(not DOCS.exists(), reason="documentation.json not in data/")


class FakeEmbedder:
    """Deterministic bag-of-words vectors: similar wording gives similar vectors."""

    def encode(self, texts):
        out = np.zeros((len(texts), 256), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


@pytest.fixture(scope="module")
def retriever():
    return Retriever(DOCS, embedder=FakeEmbedder(), min_score=0.05)


def test_hybrid_mode_with_vector_store(retriever):
    assert retriever.mode == "hybrid"
    assert retriever.store in {"chroma", "numpy"}


# A11: Chroma cannot load -> same search done in memory, still hybrid
def test_falls_back_to_in_memory_vectors_when_chroma_fails(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "chromadb", None)  # makes "import chromadb" fail
    r = Retriever(DOCS, embedder=FakeEmbedder(), min_score=0.05)
    assert r.mode == "hybrid" and r.store == "numpy"
    assert r.search("webhook signature verification fails")[0].doc_id == "DOC-API-003"


# TC-05-02: the index holds only the 29 articles
def test_tc_05_02_index_is_only_the_29_articles(retriever):
    corpus = {a["doc_id"] for a in json.loads(DOCS.read_text(encoding="utf-8"))}
    assert len(corpus) == 29
    assert {c.doc_id for c in retriever.chunks} == corpus


# TC-05-03: resolution steps are never split across chunks
def test_tc_05_03_resolution_steps_kept_whole(retriever):
    for article in retriever.articles.values():
        resolution = [c for c in retriever.chunks if c.doc_id == article["doc_id"] and c.section == "Resolution"]
        assert len(resolution) == 1
        steps = [line for line in article["content"].splitlines() if line[:2] in {"1.", "2.", "3.", "4."}]
        assert all(step in resolution[0].text for step in steps)


# A4: returned identifiers resolve back to real passages in the corpus
def test_returned_passages_resolve_to_corpus(retriever):
    results = retriever.search("webhook signature verification fails")
    assert results
    for p in results:
        assert retriever.article(p.doc_id) is not None
        chunk = next(c for c in retriever.chunks if c.chunk_id == p.chunk_id)
        assert chunk.text == p.text
    assert results[0].doc_id == "DOC-API-003"


def test_one_passage_per_article_and_at_most_k(retriever):
    results = retriever.search("API key returns 401 unauthorized", k=3)
    assert len(results) <= 3
    assert len({p.doc_id for p in results}) == len(results)


# A5 support: same query, same result
def test_search_is_deterministic(retriever):
    q = "export download link expired"
    assert retriever.search(q) == retriever.search(q)


# Build spec: returns nothing rather than something irrelevant
def test_irrelevant_query_returns_nothing():
    strict = Retriever(DOCS, embedder=FakeEmbedder(), min_score=0.99)
    assert strict.search("what is your favourite colour") == []


def test_empty_query_returns_nothing(retriever):
    assert retriever.search("   ") == []


# A11: no embedding model available -> keyword search, not a crash
def test_falls_back_to_keyword_search_when_model_fails():
    class Broken:
        def encode(self, texts):
            raise OSError("no network")

    fallback = Retriever(DOCS, embedder=Broken())
    assert fallback.mode == "keyword-only"
    assert fallback.search("rate limit 429 too many requests")[0].doc_id == "DOC-API-001"


def test_plan_line_kept_for_plan_checks():
    chunks = chunk_article(
        {"doc_id": "D-1", "title": "T", "applies_to": "Business and Enterprise plans",
         "content": "# T\n\n## Resolution\n\n1. Do it."}
    )
    assert chunks[0].applies_to == "Business and Enterprise plans"


# TC-05-01 (keyword part): recall@5 on answerable development tickets
def test_tc_05_01_keyword_recall_at_5_on_dev():
    dev = DOCS.parent / "development_tickets.json"
    if not dev.exists():
        pytest.skip("development_tickets.json not in data/")
    from cloudserve_support.pipeline.ingest import ingest

    keyword_only = Retriever(DOCS, use_embeddings=False)
    tickets = [t for t in json.loads(dev.read_text(encoding="utf-8")) if t["labels"]["answerable_from_docs"]]
    hits = sum(
        bool(set(t["labels"]["expected_doc_ids"]) & {p.doc_id for p in keyword_only.search(ingest(t).text)})
        for t in tickets
    )
    assert hits / len(tickets) >= 0.90