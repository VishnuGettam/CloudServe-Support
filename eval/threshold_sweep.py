"""Choose the routing confidence thresholds from development data (FR-06).

Run from the project root:
    uv run python eval/threshold_sweep.py --tickets data/development_tickets.json

Method: every development ticket is classified by a model that has NOT seen
any ticket with the same body (5 folds, grouped by body), so the confidences
are honest. Each ticket then goes through the real policy gate and retrieval.
For each candidate threshold the script counts:

- auto:        tickets that would be answered automatically
- intent_prec: of those, how many have the right intent (what the threshold controls)
- route_dis:   of those, how many the client labelled "escalate"
- unsafe:      must-not-automate tickets answered (must be zero)
- missed:      tickets labelled "auto_respond" that would be escalated anyway
- fcr:         automatic answers that agree with the client's route label, as a share of all tickets

Rule used to choose: the lowest threshold where intent precision among
automatic answers is at least 99% and no must-not-automate ticket is answered.
Sensitive classes (data residency, billing disputes) are reported separately.
Their threshold is the general one plus 0.10: a policy choice, because the
development data has too few of them (36) to set it on its own.

Why not choose by route_dis: about a quarter of non-blocked tickets are
labelled "escalate" because the client marked them not answerable from the
docs, and that label is not predictable from the text (identical texts carry
different labels; the best possible agreement by text is about 86%). Raising
the confidence threshold does not reduce it. Whether the docs actually answer
a ticket is decided later, by the generator's "not answerable" check.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cloudserve_support.pipeline.classify import NeighbourClassifier
from cloudserve_support.pipeline.ingest import ingest
from cloudserve_support.pipeline.policy import get_policy_gate
from cloudserve_support.pipeline.retrieve import get_retriever

FOLDS = 5
THRESHOLDS = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
MIN_INTENT_PRECISION = 0.99  # a wrong intent means a wrong article and a wrong answer
SENSITIVE_MARGIN = 0.10  # policy choice: sensitive classes need more certainty


def held_out_rows(tickets: list[dict]) -> list[dict]:
    bodies = sorted({t["body"].strip().lower() for t in tickets})
    gate, retriever = get_policy_gate(), get_retriever()
    rows = []
    for f in range(FOLDS):
        held = set(bodies[f::FOLDS])
        train = [t for t in tickets if t["body"].strip().lower() not in held]
        model = NeighbourClassifier(train)
        for t in tickets:
            if t["body"].strip().lower() not in held:
                continue
            text = ingest(t).text
            c = model.classify(text)
            intent_ok = c.intent == t["labels"]["intent"]
            p = gate.check(text, c.intent)
            rows.append({
                "confidence": c.intent_confidence,
                "intent_ok": intent_ok,
                "blocked": p.blocked,
                "sensitive": p.sensitive,
                "has_passage": bool(retriever.search(text)),
                "expected": t["labels"]["expected_route"],
                "must_not": t["labels"]["must_not_auto_respond"],
            })
    return rows


def evaluate(rows: list[dict], tau: float) -> dict:
    auto = [r for r in rows if not r["blocked"] and r["has_passage"] and r["confidence"] >= tau]
    wrong = sum(r["expected"] == "escalate" for r in auto)
    right_intent = sum(r["intent_ok"] for r in auto)
    unsafe = sum(r["must_not"] for r in auto)
    auto_ids = {id(r) for r in auto}
    missed = sum(r["expected"] == "auto_respond" and id(r) not in auto_ids for r in rows)
    return {
        "tau": tau, "auto": len(auto), "auto_rate": len(auto) / len(rows),
        "wrong_auto": wrong, "wrong_share": wrong / len(auto) if auto else 0.0,
        "intent_prec": right_intent / len(auto) if auto else 1.0,
        "unsafe": unsafe, "missed": missed, "fcr": (len(auto) - wrong) / len(rows),
        "escalation_rate": 1 - len(auto) / len(rows),
    }


def table(rows: list[dict], title: str) -> list[dict]:
    print(f"\n{title} ({len(rows)} tickets)")
    print("  tau   auto  auto%  intent_prec  route_dis  unsafe  missed   fcr    escalation")
    results = [evaluate(rows, tau) for tau in THRESHOLDS]
    for r in results:
        print(f"  {r['tau']:.2f}  {r['auto']:4d}  {r['auto_rate']:5.1%}     {r['intent_prec']:6.1%}     "
              f"{r['wrong_share']:6.1%}  {r['unsafe']:5d}  {r['missed']:6d}  {r['fcr']:5.1%}  {r['escalation_rate']:6.1%}")
    return results


def choose(results: list[dict]) -> float:
    ok = [r for r in results if r["intent_prec"] >= MIN_INTENT_PRECISION and r["unsafe"] == 0 and r["auto"] > 0]
    return min(r["tau"] for r in ok) if ok else max(THRESHOLDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickets", required=True, type=Path)
    args = parser.parse_args()
    tickets = [t for t in json.loads(args.tickets.read_text(encoding="utf-8")) if t.get("labels")]

    rows = held_out_rows(tickets)
    general = [r for r in rows if not r["sensitive"]]
    sensitive = [r for r in rows if r["sensitive"]]

    tau = choose(table(general, "General tickets"))
    table(sensitive, "Sensitive tickets (data residency, billing disputes)")
    tau_s = round(min(tau + SENSITIVE_MARGIN, 0.99), 2)

    print(f"\nChosen: CONFIDENCE_THRESHOLD={tau:.2f}  CONFIDENCE_THRESHOLD_SENSITIVE={tau_s:.2f}")
    print(f"Rule: lowest threshold with intent precision >= {MIN_INTENT_PRECISION:.0%} among automatic answers "
          "and zero must-not-automate tickets answered.")
    combined = evaluate(general, tau)
    s = evaluate(sensitive, tau_s)
    n = len(rows)
    auto = combined["auto"] + s["auto"]
    wrong = combined["wrong_auto"] + s["wrong_auto"]
    print(f"Overall at these settings: auto {auto}/{n} ({auto / n:.1%}), route disagreement {wrong} "
          f"({wrong / auto if auto else 0:.1%} of auto), FCR {(auto - wrong) / n:.1%}, escalation {1 - auto / n:.1%}")


if __name__ == "__main__":
    main()