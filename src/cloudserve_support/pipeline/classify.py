"""Intent and urgency classification by nearest-neighbour vote (FR-03, A3, A5).

How it works: the ticket is compared with the 500 labelled development
tickets. The 15 most similar vote on intent and on urgency, each vote weighted
by similarity. The share of the vote won by the top intent is turned into a
calibrated confidence: a mapping learned from cross-validation on the
development tickets, so that "0.9" means right about 90% of the time.

Why this instead of asking the language model:
- deterministic: the same ticket always gets the same answer (A5);
- no model calls, so it fits the free tier's 50 requests per day;
- confidence is measured, not the model rating itself.

Similarity uses TF-IDF on words and word pairs. Setting
CLASSIFIER_EMBEDDINGS=true averages in MiniLM similarity as well.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.isotonic import IsotonicRegression

from cloudserve_support.errors import PipelineError

log = logging.getLogger(__name__)

DEFAULT_TRAIN_PATH = "data/development_tickets.json"
K = 15
FOLDS = 5
FALLBACK_INTENT = "unclear_request"  # routed to a human by the policy gate


@dataclass(frozen=True)
class Classification:
    intent: str
    intent_confidence: float  # calibrated, 0..1
    urgency: str
    urgency_confidence: float  # raw vote share, 0..1 (urgency labels are noisy)
    alternatives: list[tuple[str, float]] = field(default_factory=list)  # top intents and vote shares
    method: str = "knn"


def ticket_text(t: dict) -> str:
    return f"{t.get('subject') or ''}\n{t.get('body') or ''}".strip()


class NeighbourClassifier:
    def __init__(self, tickets: list[dict], embedder=None, k: int = K) -> None:
        if not tickets:
            raise PipelineError("no labelled tickets to classify against")
        self.k = k
        self.texts = [ticket_text(t) for t in tickets]
        self.bodies = [(t.get("body") or "").strip().lower() for t in tickets]
        self.intents = [t["labels"]["intent"] for t in tickets]
        self.urgencies = [t["labels"]["urgency"] for t in tickets]
        self.vectorizer = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), lowercase=True, stop_words="english")
        self.matrix = self.vectorizer.fit_transform(self.texts)
        self.embedder = embedder
        self.vectors = None
        if embedder is not None:
            try:
                self.vectors = embedder.encode(self.texts)
            except Exception as exc:  # noqa: BLE001 - degrade to TF-IDF only
                log.warning("embeddings unavailable for classifier, using TF-IDF only: %s", exc)
                self.embedder = None
        self.calibrator = self._fit_calibrator()

    @property
    def method(self) -> str:
        return "knn-tfidf+minilm" if self.vectors is not None else "knn-tfidf"

    def _similarities(self, texts: list[str]) -> np.ndarray:
        sims = (self.vectorizer.transform(texts) @ self.matrix.T).toarray()
        if self.vectors is not None:
            sims = 0.5 * sims + 0.5 * (self.embedder.encode(texts) @ self.vectors.T)
        return sims

    def _vote(self, sims: np.ndarray, labels: list[str]) -> list[tuple[str, float]]:
        top = np.argsort(-sims, kind="stable")[: self.k]
        weights: dict[str, float] = defaultdict(float)
        for j in top:
            weights[labels[j]] += max(float(sims[j]), 1e-6)
        total = sum(weights.values())
        return sorted(((lab, w / total) for lab, w in weights.items()), key=lambda p: (-p[1], p[0]))

    def _fit_calibrator(self) -> IsotonicRegression:
        """Learn vote share -> probability correct, by cross-validation.

        Tickets with the same body are kept in the same fold, so a ticket is
        never scored against its own duplicate. Only 216 of the 500 bodies are
        unique, so without this the accuracy estimate would be far too high.
        """
        groups: dict[str, int] = {}
        group = np.array([groups.setdefault(b, len(groups)) for b in self.bodies])
        fold_of_group = np.arange(len(groups)) % FOLDS
        shares, correct = [], []
        all_sims = self._similarities(self.texts)
        for f in range(FOLDS):
            test = np.where(fold_of_group[group] == f)[0]
            train = np.where(fold_of_group[group] != f)[0]
            labels = [self.intents[j] for j in train]
            for i in test:
                ranked = self._vote(all_sims[i, train], labels)
                shares.append(ranked[0][1])
                correct.append(float(ranked[0][0] == self.intents[i]))
        self.cv_accuracy = float(np.mean(correct))
        cal = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(shares, correct)
        log.info("classifier ready: %d tickets, method=%s, cross-validated accuracy %.3f",
                 len(self.texts), self.method, self.cv_accuracy)
        return cal

    def classify(self, text: str) -> Classification:
        if not text or not text.strip():
            return Classification(FALLBACK_INTENT, 0.0, "medium", 0.0, [], "fallback-empty")
        sims = self._similarities([text])[0]
        if float(sims.max()) <= 0.0:
            return Classification(FALLBACK_INTENT, 0.0, "medium", 0.0, [], "fallback-no-match")
        intents = self._vote(sims, self.intents)
        urgency = self._vote(sims, self.urgencies)
        confidence = float(self.calibrator.predict([intents[0][1]])[0])
        return Classification(
            intent=intents[0][0],
            intent_confidence=round(confidence, 4),
            urgency=urgency[0][0],
            urgency_confidence=round(urgency[0][1], 4),
            alternatives=[(lab, round(s, 4)) for lab, s in intents[:3]],
            method=self.method,
        )


def load_labelled(path: str | Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot load labelled tickets from {path}: {exc}") from exc
    return [t for t in data if isinstance(t, dict) and t.get("labels", {}).get("intent")]


@lru_cache(maxsize=1)
def get_classifier() -> NeighbourClassifier:
    """One shared classifier per process. TF-IDF by default; MiniLM optional."""
    embedder = None
    if os.getenv("CLASSIFIER_EMBEDDINGS", "false").lower() == "true":
        try:
            from cloudserve_support.pipeline.retrieve import MiniLMEmbedder
            embedder = MiniLMEmbedder()
        except Exception as exc:  # noqa: BLE001
            log.warning("MiniLM unavailable for classifier: %s", exc)
    return NeighbourClassifier(load_labelled(os.getenv("TRAIN_PATH", DEFAULT_TRAIN_PATH)), embedder)