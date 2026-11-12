"""Mandatory caveats: facts an automatic reply must never leave out (FR-08 follow-up).

The pre-send checks catch claims the articles don't support. They can't catch
important facts the model left out. Validation ticket VAL-0026 ("our data
must stay in the EU for a contract") got a grounded reply that omitted that
metadata is stored centrally and that moving region needs a migration.

So for a short list of articles (config/caveats.yaml), code adds the
critical sentence to every automatic reply that cites the article, under the
answer. The model is not asked to remember it, and the prompt does not change.

Safety of the caveats themselves:
- each caveat must appear word for word in its article; this is checked when
  the caveats are loaded, and a mismatch stops the system from starting
  (every ticket then escalates), so caveats can't drift from the docs;
- caveats come from a file owned by Support, not from the model.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from cloudserve_support.errors import PipelineError

log = logging.getLogger(__name__)

DEFAULT_CAVEATS = "config/caveats.yaml"
DEFAULT_DOCS = "data/documentation.json"


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass(frozen=True)
class Caveats:
    by_doc: dict[str, tuple[str, ...]]
    version: str

    def for_docs(self, doc_ids: list[str]) -> list[tuple[str, str]]:
        """(doc_id, caveat) pairs for the cited articles, in citation order, without repeats."""
        out: list[tuple[str, str]] = []
        for doc_id in dict.fromkeys(doc_ids):
            out.extend((doc_id, text) for text in self.by_doc.get(doc_id, ()))
        return out


def _load_articles(path: str) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("articles") or data.get("documents") or list(data.values())
    return {a["doc_id"]: a.get("content", "") for a in data if isinstance(a, dict) and "doc_id" in a}


def load_caveats(path: str, docs_path: str) -> Caveats:
    """Read the caveats file and check every caveat against its article. Raises PipelineError on any problem."""
    if not Path(path).exists():
        log.warning("no caveats file at %s: automatic replies get no mandatory caveats", path)
        return Caveats({}, "none")
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        articles = _load_articles(docs_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PipelineError(f"cannot load caveats: {exc}") from exc

    by_doc: dict[str, tuple[str, ...]] = {}
    problems: list[str] = []
    for doc_id, texts in (config.get("caveats") or {}).items():
        if doc_id not in articles:
            problems.append(f"{doc_id}: no such article")
            continue
        body = _normalise(articles[doc_id])
        texts = [texts] if isinstance(texts, str) else list(texts or [])
        for text in texts:
            if _normalise(str(text)) not in body:
                problems.append(f"{doc_id}: caveat not found word for word in the article: {str(text)[:80]}")
        by_doc[doc_id] = tuple(str(t).strip() for t in texts)
    if problems:
        raise PipelineError("caveats file does not match the documentation: " + "; ".join(problems))
    caveats = Caveats(by_doc, str(config.get("version", "unversioned")))
    log.info("caveats ready: version %s, %d articles", caveats.version, len(by_doc))
    return caveats


@lru_cache(maxsize=1)
def get_caveats() -> Caveats:
    return load_caveats(os.getenv("CAVEATS_CONFIG", DEFAULT_CAVEATS), os.getenv("DOCS_PATH", DEFAULT_DOCS))
