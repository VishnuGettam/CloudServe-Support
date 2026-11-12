"""The single entry point that both the API and the harness call.

`handle` is the error boundary for one ticket: whatever goes wrong, it returns
exactly one TicketResult and never raises.

    known error (CloudServeError) -> escalation with that error's reason code
    anything else                 -> escalation with PIPELINE_ERROR, full
                                     traceback in the log

`process` runs the pipeline for one valid ticket:

    kill switch -> classify -> policy gate -> retrieve -> route
        -> (automatic route only) generate -> validate -> reply
        -> (any other outcome) escalation packet

A ticket is answered automatically only if routing allows it, the model
drafts an answer from the articles, and the draft passes every pre-send
check. Anything else escalates with a packet (FR-09). If the model can't be
reached, the ticket escalates with MODEL_UNAVAILABLE (FR-11, A11).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode, TicketResult
from cloudserve_support.decision_log import record_decision
from cloudserve_support.errors import CloudServeError, ModelUnavailable, describe, safe_ticket_id
from cloudserve_support.pipeline.caveats import get_caveats
from cloudserve_support.pipeline.classify import get_classifier
from cloudserve_support.pipeline.escalate import build_packet
from cloudserve_support.pipeline.generate import Draft, generate
from cloudserve_support.pipeline.ingest import ingest
from cloudserve_support.pipeline.policy import get_policy_gate
from cloudserve_support.pipeline.retrieve import get_retriever
from cloudserve_support.pipeline.route import decide, thresholds
from cloudserve_support.pipeline.validate import validate

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def kill_switch_on() -> bool:
    return os.getenv("KILL_SWITCH", "false").strip().lower() in {"1", "true", "yes", "on"}


def warm_up() -> None:
    """Load the classifier, policy gate, caveats and retriever once, before the first ticket."""
    get_classifier()
    get_policy_gate()
    get_caveats()
    get_retriever()


AI_LABEL = (
    "This reply was drafted automatically by CloudServe's AI support assistant from our help articles. "
    "If it doesn't solve the problem, reply to this message and a member of the support team will pick it up."
)


def format_reply(draft: Draft, caveats: list[tuple[str, str]] | None = None) -> str:
    """The customer-facing reply, assembled by code:

    answer, then any mandatory caveats for the cited articles (quoted from the
    articles), then the sources, then the AI-drafted label.
    """
    cited = list(dict.fromkeys(c["doc_id"] for c in draft.citations))
    sources = "; ".join(f"{d} ({draft.articles[d]['title']})" for d in cited)
    parts = [draft.answer]
    if caveats:
        parts.append("Important to know:\n" + "\n".join(f"- {text} ({doc_id})" for doc_id, text in caveats))
    parts += [f"Sources: {sources}", AI_LABEL]
    return "\n\n".join(parts)


def process(ticket: NormalisedTicket) -> TicketResult:
    """Run the pipeline for one valid ticket."""
    if kill_switch_on():
        return escalation(
            ticket.ticket_id, ReasonCode.KILL_SWITCH, "kill switch on", ticket.received_at,
            reason_text="Kill switch is on: every ticket is sent to a human.",
        )

    c = get_classifier().classify(ticket.text)
    policy = get_policy_gate().check(ticket.text, c.intent)
    passages = get_retriever().search(ticket.text)  # always run, so escalations carry sources
    decision = decide(c, policy, passages)

    common: dict[str, Any] = dict(
        ticket_id=ticket.ticket_id,
        intent=c.intent,
        urgency=c.urgency,
        intent_confidence=c.intent_confidence,
        alternatives=[{"intent": i, "share": s} for i, s in c.alternatives],
        doc_ids=[p.doc_id for p in passages],
        retrieval_scores=[p.score for p in passages],
        policy_rule=policy.rule,
        sensitive=policy.sensitive,
        policy_version=policy.policy_version,
        received_at=ticket.received_at,
    )

    def escalate(reason: ReasonCode, reason_text: str, draft: Draft | None = None,
                 failed_check: str | None = None, detail: str | None = None,
                 checks: list[str] | None = None) -> TicketResult:
        packet = build_packet(ticket, reason, reason_text, c, policy, passages, thresholds(),
                              draft_answer=draft.answer if draft and draft.answer else None,
                              failed_check=failed_check, detail=detail)
        return TicketResult(
            **common, route="escalate", reason_code=reason, reason_text=reason_text, packet=packet,
            failed_check=failed_check, detail=detail, checks=checks or [],
            model=draft.model if draft else None, prompt_version=draft.prompt_version if draft else None,
            cached=draft.cached if draft else None, responded_at=_now(),
        )

    if decision.route != "auto_respond":
        return escalate(decision.reason_code, decision.reason_text)

    # Routed to an automatic answer: draft it, then check it before it can be sent.
    try:
        draft = generate(ticket, passages)
    except ModelUnavailable as exc:
        return escalate(ReasonCode.MODEL_UNAVAILABLE,
                        "Sent to a human: the answer-writing model was unavailable.", detail=exc.detail)

    if not draft.answerable and not draft.format_error:
        missing = draft.missing_information or "the articles found do not answer this question"
        return escalate(ReasonCode.WEAK_RETRIEVAL,
                        "Sent to a human: the help articles found do not answer this question.",
                        draft=draft, detail=f"generator: {missing}"[:200])

    check = validate(draft, ticket.channel, ticket.customer_tier)
    if not check.passed:
        return escalate(ReasonCode.VALIDATION_FAILED,
                        f"Sent to a human: the drafted answer failed the '{check.failed_check}' check.",
                        draft=draft, failed_check=check.failed_check, detail=check.detail, checks=check.checks)

    cited = list(dict.fromkeys(c_["doc_id"] for c_ in draft.citations))
    caveats = get_caveats().for_docs(cited)  # mandatory facts the answer must carry, added by code
    checks = [*check.checks, "caveats:pass"]  # which caveats were added is in caveat_doc_ids
    return TicketResult(
        **common, route="auto_respond", reason_code=ReasonCode.AUTO_RESPONDED,
        reason_text=f"{decision.reason_text} Passed all {len(check.checks)} pre-send checks"
                    + (f"; {len(caveats)} mandatory caveat(s) added." if caveats else "."),
        response=format_reply(draft, caveats), cited_doc_ids=cited, checks=checks,
        caveat_doc_ids=list(dict.fromkeys(d for d, _ in caveats)),
        model=draft.model, cached=draft.cached, prompt_version=draft.prompt_version, responded_at=_now(),
    )


def escalation(
    ticket_id: str | None,
    reason: ReasonCode,
    detail: str,
    received_at: datetime | None = None,
    reason_text: str | None = None,
) -> TicketResult:
    """An escalation that carries the reason it happened. Cannot fail on valid input types."""
    return TicketResult(
        ticket_id=ticket_id or "UNKNOWN",
        route="escalate",
        reason_code=reason,
        reason_text=reason_text or _default_reason_text(reason),
        detail=detail,
        received_at=received_at,
        responded_at=_now(),
    )


def _default_reason_text(reason: ReasonCode) -> str:
    return {
        ReasonCode.INVALID_INPUT: "Sent to a human: the ticket could not be read (missing or invalid fields).",
        ReasonCode.MODEL_UNAVAILABLE: "Sent to a human: the language model was unavailable.",
        ReasonCode.PIPELINE_ERROR: "Sent to a human: an internal error occurred while processing the ticket.",
        ReasonCode.KILL_SWITCH: "Kill switch is on: every ticket is sent to a human.",
    }.get(reason, "Sent to a human.")


def handle(raw: Any, source: str = "api") -> TicketResult:
    """Turn any raw input into exactly one result, and log the decision (FR-10). Never raises.

    `source` names where the ticket came from in the decision log: "api", or
    the harness run id.
    """
    started = time.perf_counter()
    ticket_id = safe_ticket_id(raw)
    received_at: datetime | None = None
    ticket: NormalisedTicket | None = None
    try:
        ticket = ingest(raw)
        ticket_id, received_at = ticket.ticket_id, ticket.received_at
        result = process(ticket)

    except CloudServeError as exc:
        log.warning("ticket %s escalated: %s (%s)", exc.ticket_id or ticket_id, exc.reason_code.value, exc.detail)
        result = escalation(exc.ticket_id or ticket_id, exc.reason_code, exc.detail, received_at)

    except Exception as exc:  # noqa: BLE001 - this is the error boundary
        log.exception("unexpected error for ticket %s", ticket_id)
        result = escalation(ticket_id, ReasonCode.PIPELINE_ERROR, describe(exc), received_at)

    if ticket is not None:  # group fields for the fairness audit; filled even when a later step failed
        result.channel = ticket.channel
        result.customer_tier = ticket.customer_tier
        result.customer_region = ticket.customer_region
        result.language_fluency = ticket.language_fluency
        result.input_hash = ticket.input_hash
    result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    record_decision(result, source)  # never raises: a logging failure must not lose the ticket
    return result
