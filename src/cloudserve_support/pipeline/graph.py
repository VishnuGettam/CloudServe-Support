"""The single entry point that both the API and the harness call.

`handle` is the error boundary for one ticket: whatever goes wrong, it returns
exactly one TicketResult and never raises.

    known error (CloudServeError) -> escalation with that error's reason code
    anything else                 -> escalation with PIPELINE_ERROR, full
                                     traceback in the log

`process` runs one valid ticket through the pipeline graph
(pipeline/orchestrator.py, a LangGraph state graph):

    kill switch -> classify -> policy gate -> retrieve -> route
        -> (automatic route only) generate -> validate -> respond
        -> (any other outcome) escalate, with a packet

After every ticket, `handle` writes the decision log (FR-10) and updates the
Prometheus metrics. Neither can stop a ticket.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode, TicketResult
from cloudserve_support.decision_log import record_decision
from cloudserve_support.errors import CloudServeError, describe, safe_ticket_id
from cloudserve_support.observability import observe_result, set_orchestrator
from cloudserve_support.pipeline.caveats import get_caveats
from cloudserve_support.pipeline.classify import get_classifier
from cloudserve_support.pipeline.ingest import ingest
from cloudserve_support.pipeline.orchestrator import (  # noqa: F401 - re-exported for callers and tests
    AI_LABEL, format_reply, get_orchestrator, kill_switch_on, run_pipeline,
)
from cloudserve_support.pipeline.policy import get_policy_gate
from cloudserve_support.pipeline.retrieve import get_retriever

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def warm_up() -> None:
    """Load the classifier, policy gate, caveats, retriever and pipeline graph once, before the first ticket."""
    get_classifier()
    get_policy_gate()
    get_caveats()
    get_retriever()
    set_orchestrator(getattr(get_orchestrator(), "engine", "unknown"))


def process(ticket: NormalisedTicket) -> TicketResult:
    """Run the pipeline graph for one valid ticket."""
    return run_pipeline(ticket)


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
    observe_result(result)           # Prometheus metrics; never raises
    return result
