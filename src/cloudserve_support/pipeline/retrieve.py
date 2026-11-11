"""Hybrid retrieval over the 29 help articles (FR-05, acceptance criterion A4).

Chunking decision: each article is split at its "## " headings into
Symptoms, Common causes, Resolution and Notes. These sections are short and
self-contained, and splitting at headings never breaks a numbered list of
resolution steps. The title and plan line are repeated in every chunk so a
chunk still makes sense on its own.

Search: meaning-based search (MiniLM embeddings in Chroma) and keyword search
(BM25) each rank every chunk; the two rankings are merged by reciprocal rank
fusion. Results are reduced to one passage per article, best first.

Relevance threshold: a passage is returned only if its meaning similarity is
at least `min_score`. If nothing clears it, the result is an empty list:
returning nothing is a valid answer, and is better than returning something
irrelevant (Build Specification, Retrieve).

If the embedding model cannot be loaded (for example no network on first
run), retrieval falls back to keyword search only and logs a warning, rather
than stopping the system (A11).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from cloudserve_support.errors import PipelineError

log = logging.getLogger(__name__)

DEFAULT_DOCS_PATH = "data/documentation.json"
DEFAULT_MODEL = "all-MiniLM-L6-v2"
DEFAULT_MIN_SCORE = 0.30  # starting point; set from data by eval/retrieval_check.py
DEFAULT_BM25_MIN = 2.0  # used only when the embedding model is unavailable
RRF_K = 60
CANDIDATES = 20

_APPLIES = re.compile(r"\*\*Applies to:\*\*\s*(.+)")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be but by can do does for from has have how i if in is it its "
    "my no not of on or our so that the their them then there this to us was we what "
    "when which why will with you your".split()
)


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


class MiniLMEmbedder:
    """Wraps sentence-transformers; returns unit-length vectors."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, load once

        self.model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True), dtype=np.float32)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    applies_to: str
    text: str


@dataclass(frozen=True)
class Passage:
    """One retrieved passage. `doc_id` and `chunk_id` resolve to the real corpus."""

    doc_id: str
    chunk_id: str
    title: str
    section: str
    applies_to: str
    text: str
    score: float  # meaning similarity, 0..1 (0 when running keyword-only)
    keyword_score: float

    def to_dict(self) -> dict:
        return asdict(self)


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def chunk_article(article: dict) -> list[Chunk]:
    """Split one article at its '## ' headings."""
    try:
        doc_id, title, content = article["doc_id"], article["title"], article["content"]
    except KeyError as exc:
        raise PipelineError(f"article is missing field {exc}") from exc

    applies = article.get("applies_to") or ""
    if not applies:
        match = _APPLIES.search(content)
        applies = match.group(1).strip() if match else ""

    chunks: list[Chunk] = []
    for part in re.split(r"\n(?=## )", content):
        heading, _, body = part.partition("\n")
        if not heading.startswith("## "):
            continue  # the title and plan line; repeated in every chunk instead
        section = heading[3:].strip()
        body = body.strip()
        if not body:
            continue
        text = f"{title}\nApplies to: {applies}\n{section}:\n{body}"
        chunks.append(Chunk(f"{doc_id}#{section.lower().replace(' ', '-')}", doc_id, title, section, applies, text))

    if not chunks:  # an article without sections is kept whole
        chunks.append(Chunk(f"{doc_id}#full", doc_id, title, "Full article", applies, f"{title}\n{content}"))
    return chunks


class Retriever:
    def __init__(
        self,
        docs_path: str | Path = DEFAULT_DOCS_PATH,
        embedder: Embedder | None = None,
        min_score: float | None = None,
        use_embeddings: bool = True,
    ) -> None:
        path = Path(docs_path)
        try:
            articles = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(f"cannot load documentation from {path}: {exc}") from exc
        if not isinstance(articles, list) or not articles:
            raise PipelineError(f"documentation file {path} contains no articles")

        self.articles = {a["doc_id"]: a for a in articles}
        self.chunks = [c for a in articles for c in chunk_article(a)]
        self.min_score = float(os.getenv("RETRIEVAL_MIN_SCORE", DEFAULT_MIN_SCORE)) if min_score is None else min_score
        self.bm25 = BM25Okapi([tokens(c.text) for c in self.chunks])

        self.embedder: Embedder | None = None
        self.collection = None
        if use_embeddings:
            try:
                self.embedder = embedder or MiniLMEmbedder(os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL))
                self._build_vector_store()
            except Exception as exc:  # noqa: BLE001 - degrade to keyword search, never stop
                log.warning("embedding search unavailable, using keyword search only: %s", exc)
                self.embedder, self.collection = None, None

        log.info(
            "retriever ready: %d articles, %d chunks, mode=%s, min_score=%.2f",
            len(self.articles), len(self.chunks), self.mode, self.min_score,
        )

    @property
    def mode(self) -> str:
        return "hybrid" if self.collection is not None else "keyword-only"

    def _build_vector_store(self) -> None:
        import chromadb
        from chromadb.config import Settings

        vectors = self.embedder.encode([c.text for c in self.chunks])
        client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
        # Rebuilt on every start from documentation.json, so the index can never be stale.
        self.collection = client.create_collection(f"docs-{uuid.uuid4().hex[:8]}", metadata={"hnsw:space": "cosine"})
        self.collection.add(
            ids=[c.chunk_id for c in self.chunks],
            embeddings=vectors.tolist(),
            metadatas=[{"doc_id": c.doc_id} for c in self.chunks],
        )

    def article(self, doc_id: str) -> dict | None:
        """The full article for a doc_id, for the generator and citation checks."""
        return self.articles.get(doc_id)

    def search(self, query: str, k: int | None = None) -> list[Passage]:
        """Return up to k relevant passages, one per article, best first. May be empty."""
        k = k or int(os.getenv("RETRIEVAL_TOP_K", 5))
        if not query or not query.strip():
            return []

        index = {c.chunk_id: i for i, c in enumerate(self.chunks)}
        keyword = self.bm25.get_scores(tokens(query))
        keyword_rank = sorted(range(len(self.chunks)), key=lambda i: -keyword[i])[:CANDIDATES]

        similarity: dict[int, float] = {}
        dense_rank: list[int] = []
        if self.collection is not None:
            vector = self.embedder.encode([query])
            hits = self.collection.query(query_embeddings=vector.tolist(), n_results=min(CANDIDATES, len(self.chunks)))
            for chunk_id, distance in zip(hits["ids"][0], hits["distances"][0]):
                i = index[chunk_id]
                dense_rank.append(i)
                similarity[i] = max(0.0, 1.0 - float(distance))

        fused: dict[int, float] = {}
        for ranking in (dense_rank, keyword_rank):
            for rank, i in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)

        results: list[Passage] = []
        seen_docs: set[str] = set()
        for i in sorted(fused, key=lambda i: (-fused[i], self.chunks[i].chunk_id)):  # tie-break keeps it deterministic
            chunk = self.chunks[i]
            if chunk.doc_id in seen_docs:
                continue
            if not self._relevant(similarity.get(i, 0.0), float(keyword[i])):
                continue
            seen_docs.add(chunk.doc_id)
            results.append(
                Passage(
                    chunk.doc_id, chunk.chunk_id, chunk.title, chunk.section, chunk.applies_to,
                    chunk.text, round(similarity.get(i, 0.0), 4), round(float(keyword[i]), 4),
                )
            )
            if len(results) == k:
                break
        return results

    def _relevant(self, similarity: float, keyword: float) -> bool:
        if self.collection is not None:
            return similarity >= self.min_score
        return keyword >= DEFAULT_BM25_MIN


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    """One shared retriever per process; the model loads once."""
    return Retriever(os.getenv("DOCS_PATH", DEFAULT_DOCS_PATH))