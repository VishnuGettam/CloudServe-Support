"""LangChain retriever over the 29 help articles.

Wraps the hybrid retriever (pipeline/retrieve.py) as a standard LangChain
`BaseRetriever`, so the knowledge base can be used by any LangChain or
LangGraph component (`retriever.invoke("question")` returns `Document`s with
the doc_id, section and scores in their metadata).

The search itself is unchanged: same chunks, same hybrid scoring, same
relevance threshold. `search_passages()` is what the pipeline calls; it goes
through LangChain when langchain-core is installed and loadable, and calls the
retriever directly when it isn't, with identical results.
"""

from __future__ import annotations

import logging
from dataclasses import fields

from cloudserve_support.pipeline.retrieve import Passage, get_retriever

log = logging.getLogger(__name__)

_PASSAGE_FIELDS = [f.name for f in fields(Passage)]

try:  # langchain-core ships compiled dependencies; degrade if they can't load
    from langchain_core.callbacks import CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever

    LANGCHAIN_AVAILABLE = True
    LANGCHAIN_ERROR = ""
except Exception as exc:  # noqa: BLE001 - ImportError or a blocked DLL (OSError)
    LANGCHAIN_AVAILABLE = False
    LANGCHAIN_ERROR = str(exc)[:200]


def to_document(passage: Passage) -> "Document":
    return Document(page_content=passage.text, metadata=passage.to_dict())


def to_passage(document: "Document") -> Passage:
    return Passage(**{name: document.metadata[name] for name in _PASSAGE_FIELDS})


if LANGCHAIN_AVAILABLE:

    class CloudServeRetriever(BaseRetriever):
        """LangChain retriever: hybrid search over the 29 CloudServe help articles only."""

        k: int | None = None

        def _get_relevant_documents(
            self, query: str, *, run_manager: CallbackManagerForRetrieverRun
        ) -> list[Document]:
            return [to_document(p) for p in get_retriever().search(query, self.k)]


_logged = False


def search_passages(query: str) -> list[Passage]:
    """Retrieve passages for the pipeline, through LangChain when available."""
    global _logged
    if not LANGCHAIN_AVAILABLE:
        if not _logged:
            log.warning("langchain-core unavailable (%s); calling the retriever directly", LANGCHAIN_ERROR)
            _logged = True
        return get_retriever().search(query)
    return [to_passage(d) for d in CloudServeRetriever().invoke(query)]
