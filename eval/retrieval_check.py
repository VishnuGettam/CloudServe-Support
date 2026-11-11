"""Measure retrieval quality with the real MiniLM model, and choose the relevance threshold.

Run from the project root:
    uv run python eval/retrieval_check.py --tickets data/development_tickets.json

Prints:
- recall@1, @3 and @5 for hybrid and keyword-only search, on answerable tickets
  (TC-05-01: target recall@5 >= 90%);
- the best similarity score for answerable vs not-answerable tickets, and for
  several candidate thresholds, how much recall is kept and how many
  not-answerable tickets would wrongly get passages.

Use development tickets to choose the threshold. Check on validation only.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from cloudserve_support.pipeline.ingest import ingest
from cloudserve_support.pipeline.retrieve import Retriever


def recall(retriever: Retriever, tickets: list[dict]) -> dict[int, float]:
    hits = {1: 0, 3: 0, 5: 0}
    for t in tickets:
        found = [p.doc_id for p in retriever.search(ingest(t).text, k=5)]
        expected = set(t["labels"]["expected_doc_ids"])
        for k in hits:
            hits[k] += bool(expected & set(found[:k]))
    return {k: round(v / len(tickets), 3) for k, v in hits.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", required=True, type=Path)
    parser.add_argument("--docs", default="data/documentation.json", type=Path)
    args = parser.parse_args()

    tickets = json.loads(args.tickets.read_text(encoding="utf-8"))
    answerable = [t for t in tickets if t["labels"]["answerable_from_docs"]]
    other = [t for t in tickets if not t["labels"]["answerable_from_docs"]]

    hybrid = Retriever(args.docs, min_score=0.0)
    keyword = Retriever(args.docs, use_embeddings=False)
    print(f"mode: {hybrid.mode}; {len(answerable)} answerable, {len(other)} not answerable\n")
    if hybrid.mode != "hybrid":
        print("WARNING: the embedding model did not load (see the warning above), so this run")
        print("measures keyword search only. Similarity scores and the threshold table need")
        print("the model; fix the warning and run again.\n")
    print("recall (answerable tickets)")
    print(f"  hybrid       {recall(hybrid, answerable)}")
    print(f"  keyword-only {recall(keyword, answerable)}\n")

    if hybrid.mode != "hybrid":
        return

    def top_score(t: dict) -> float:
        results = hybrid.search(ingest(t).text, k=1)
        return results[0].score if results else 0.0

    def best_correct_score(t: dict) -> float:
        expected = set(t["labels"]["expected_doc_ids"])
        scores = [p.score for p in hybrid.search(ingest(t).text, k=5) if p.doc_id in expected]
        return max(scores, default=0.0)

    a_scores = [best_correct_score(t) for t in answerable]
    o_scores = [top_score(t) for t in other]
    for name, s in (("answerable (correct article)", a_scores), ("not answerable (top article)", o_scores)):
        q = statistics.quantiles(s, n=10)
        print(f"{name:30s} median {statistics.median(s):.3f}  p10 {q[0]:.3f}  p90 {q[-1]:.3f}")

    print("\nthreshold  recall_kept  not_answerable_still_matched")
    for threshold in (0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50):
        kept = sum(s >= threshold for s in a_scores) / len(a_scores)
        leak = sum(s >= threshold for s in o_scores) / max(len(o_scores), 1)
        print(f"  {threshold:.2f}      {kept:6.1%}        {leak:6.1%}")
    print("\nSet RETRIEVAL_MIN_SCORE to the highest threshold that keeps recall_kept at about 95%.")


if __name__ == "__main__":
    main()