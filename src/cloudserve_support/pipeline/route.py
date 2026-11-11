"""Routing: answer automatically or escalate (FR-06, A5).

Deterministic: the decision depends only on the classifier output, the policy
gate result and the retrieved passages, all of which are deterministic, and
on two thresholds read from configuration.

Thresholds were chosen from development data by eval/threshold_sweep.py:
- CONFIDENCE_THRESHOLD (0.80): lowest value where intent precision among
  automatic answers reaches 99%.
- CONFIDENCE_THRESHOLD_SENSITIVE (0.90): data residency and billing disputes
  need 0.10 more certainty. A policy choice; too few examples to fit it.

Checks run in a fixed order, and the first that fails gives the reason code:
policy gate -> relevant passage found -> sensitive threshold -> general threshold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cloudserve_support.app.schemas import ReasonCode
from cloudserve_support.pipeline.classify import Classification
from cloudserve_support.pipeline.policy import PolicyDecision
from cloudserve_support.pipeline.retrieve import Passage

DEFAULT_THRESHOLD = 0.80
DEFAULT_THRESHOLD_SENSITIVE = 0.90


@dataclass(frozen=True)
class RouteDecision:
    route: str  # "auto_respond" or "escalate"
    reason_code: ReasonCode
    reason_text: str  # plain language, for a support manager
    threshold: float


def thresholds() -> tuple[float, float]:
    general = float(os.getenv("CONFIDENCE_THRESHOLD", DEFAULT_THRESHOLD))
    sensitive = float(os.getenv("CONFIDENCE_THRESHOLD_SENSITIVE", DEFAULT_THRESHOLD_SENSITIVE))
    return general, max(sensitive, general)


def decide(c: Classification, policy: PolicyDecision, passages: list[Passage]) -> RouteDecision:
    general, sensitive = thresholds()
    tau = sensitive if policy.sensitive else general
    intent = c.intent.replace("_", " ")

    if policy.blocked:
        return RouteDecision("escalate", policy.reason_code, policy.reason_text, tau)

    if not passages:
        return RouteDecision(
            "escalate", ReasonCode.WEAK_RETRIEVAL,
            "Sent to a human: no help article was relevant enough to answer from.", tau,
        )

    if policy.sensitive and c.intent_confidence < sensitive:
        return RouteDecision(
            "escalate", ReasonCode.SENSITIVE_CLASS,
            f"Sent to a human: this looks like a {policy.sensitive.replace('_', ' ')} question, which needs "
            f"{sensitive:.0%} certainty; the system was {c.intent_confidence:.0%} sure.", tau,
        )

    if c.intent_confidence < general:
        runner_up = next((a for a, _ in c.alternatives if a != c.intent), None)
        alternative = f" (it may also be {runner_up.replace('_', ' ')})" if runner_up else ""
        return RouteDecision(
            "escalate", ReasonCode.LOW_CONFIDENCE,
            f"Sent to a human: the system was only {c.intent_confidence:.0%} sure this is {intent}{alternative}; "
            f"it needs {general:.0%}.", tau,
        )

    return RouteDecision(
        "auto_respond", ReasonCode.AUTO_RESPONDED,
        f"Answered automatically: {c.intent_confidence:.0%} sure this is {intent}, "
        f"answered from {passages[0].doc_id}.", tau,
    )