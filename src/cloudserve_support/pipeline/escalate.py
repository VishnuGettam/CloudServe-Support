"""Escalation packets and the human queue (FR-09).

The packet is built in code, without a model call, so escalations cost
nothing against the free tier and are fully reproducible. It gives the
engineer what they need without re-reading the thread or re-asking the
customer: a short summary, the classification and its alternatives, the
sources found, any draft, and a specific list of what the system was unsure
about.

Queue order: urgency (high, medium, low), then customer tier (enterprise,
business, standard), then the oldest ticket first.
"""

from __future__ import annotations

import re
from typing import Any

from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode
from cloudserve_support.pipeline.classify import Classification
from cloudserve_support.pipeline.policy import PolicyDecision
from cloudserve_support.pipeline.retrieve import Passage

URGENCY_RANK = {"high": 0, "medium": 1, "low": 2}
TIER_RANK = {"enterprise": 0, "business": 1, "standard": 2}
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def summarise(ticket: NormalisedTicket, max_sentences: int = 2) -> str:
    """Up to three sentences: who and where, then the customer's first sentences (name already masked)."""
    sentences = [s.strip() for s in _SENTENCE.split(ticket.body.replace("\n", " ")) if s.strip()]
    quoted = " ".join(sentences[:max_sentences])
    if len(quoted) > 400:
        quoted = quoted[:397].rstrip() + "..."
    return f"{ticket.customer_tier.capitalize()} customer, {ticket.channel.replace('_', ' ')}. {quoted}"


def uncertainties(
    reason: ReasonCode,
    c: Classification,
    policy: PolicyDecision,
    passages: list[Passage],
    thresholds: tuple[float, float],
    failed_check: str | None = None,
    detail: str | None = None,
) -> list[str]:
    general, sensitive = thresholds
    notes: list[str] = []
    runner = next((a for a, _ in c.alternatives if a != c.intent), None)
    split = f", runner-up {runner}" if runner else ""
    notes.append(f"Intent {c.intent} at confidence {c.intent_confidence:.2f}{split}.")

    if reason in (ReasonCode.POLICY_BLOCKED, ReasonCode.KEYWORD_BLOCKED):
        notes.append(f"Policy rule {policy.rule} matched '{policy.matched}'.")
    elif reason == ReasonCode.LOW_CONFIDENCE:
        notes.append(f"Confidence below the {general:.2f} threshold.")
    elif reason == ReasonCode.SENSITIVE_CLASS:
        notes.append(f"Flagged {policy.sensitive}; needs {sensitive:.2f}.")
    elif reason == ReasonCode.WEAK_RETRIEVAL:
        notes.append(detail or "No help article passed the relevance threshold.")
    elif reason == ReasonCode.VALIDATION_FAILED:
        notes.append(f"Draft failed the '{failed_check}' check: {detail}.")
    elif reason == ReasonCode.MODEL_UNAVAILABLE:
        notes.append(f"No answer drafted: {detail}.")

    if passages:
        best = passages[0]
        notes.append(f"Best article match {best.doc_id} ({best.title}), similarity {best.score:.2f}.")
    else:
        notes.append("No help article matched.")
    if c.urgency_confidence < 0.6:
        notes.append(f"Urgency '{c.urgency}' is uncertain (vote share {c.urgency_confidence:.2f}).")
    return notes


def build_packet(
    ticket: NormalisedTicket,
    reason: ReasonCode,
    reason_text: str,
    c: Classification,
    policy: PolicyDecision,
    passages: list[Passage],
    thresholds: tuple[float, float],
    draft_answer: str | None = None,
    failed_check: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "summary": summarise(ticket),
        "reason_code": reason.value,
        "reason": reason_text,
        "intent": c.intent,
        "intent_confidence": c.intent_confidence,
        "alternatives": [{"intent": i, "share": s} for i, s in c.alternatives],
        "urgency": c.urgency,
        "customer_tier": ticket.customer_tier,
        "channel": ticket.channel,
        "uncertainties": uncertainties(reason, c, policy, passages, thresholds, failed_check, detail),
        "sources": [{"doc_id": p.doc_id, "title": p.title, "section": p.section, "score": p.score} for p in passages],
        "draft_answer": draft_answer,
        "suggested_first_step": (
            f"Start from {passages[0].doc_id}: {passages[0].title}." if passages else "No article matched; read the ticket."
        ),
        "policy_checks": policy.checks,
        "policy_version": policy.policy_version,
    }


def queue_key(result: dict[str, Any]) -> tuple:
    packet = result.get("packet") or {}
    return (
        URGENCY_RANK.get(result.get("urgency") or "medium", 1),
        TIER_RANK.get(packet.get("customer_tier") or "standard", 2),
        result.get("received_at") or "",
        result.get("ticket_id") or "",
    )


def human_queue(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Escalated tickets in the order a human should take them."""
    escalated = [r for r in results if r.get("route") == "escalate"]
    return [
        {
            "position": i + 1,
            "ticket_id": r["ticket_id"],
            "urgency": r.get("urgency"),
            "customer_tier": (r.get("packet") or {}).get("customer_tier"),
            "received_at": r.get("received_at"),
            "reason_code": r.get("reason_code"),
            "summary": (r.get("packet") or {}).get("summary"),
        }
        for i, r in enumerate(sorted(escalated, key=queue_key))
    ]