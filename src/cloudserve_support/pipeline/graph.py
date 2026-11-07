"""The single entry point that both the API and the harness call.

`handle` is the error boundary for one ticket: whatever goes wrong, it returns
exactly one TicketResult and never raises.

    known error (CloudServeError) -> escalation with that error's reason code
    anything else                 -> escalation with PIPELINE_ERROR, full
                                     traceback in the log

For now `process` is a placeholder that escalates every valid ticket. It is
replaced by the LangGraph flow once classification (FR-03) onwards exist.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode, TicketResult
from cloudserve_support.errors import CloudServeError, describe, safe_ticket_id
from cloudserve_support.pipeline.ingest import ingest

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def process(ticket: NormalisedTicket) -> TicketResult:
    """Placeholder pipeline: escalate everything until FR-03 onwards exist."""
    return TicketResult(
        ticket_id=ticket.ticket_id,
        route="escalate",
        reason_code=ReasonCode.LOW_CONFIDENCE,
        detail="pipeline not built yet",
        received_at=ticket.received_at,
        responded_at=_now(),
    )


def escalation(
    ticket_id: str | None,
    reason: ReasonCode,
    detail: str,
    received_at: datetime | None = None,
) -> TicketResult:
    """An escalation that carries the reason it happened. Cannot fail on valid input types."""
    return TicketResult(
        ticket_id=ticket_id or "UNKNOWN",
        route="escalate",
        reason_code=reason,
        detail=detail,
        received_at=received_at,
        responded_at=_now(),
    )


def handle(raw: Any) -> TicketResult:
    """Turn any raw input into exactly one result. Never raises."""
    ticket_id = safe_ticket_id(raw)
    received_at: datetime | None = None
    try:
        ticket = ingest(raw)
        ticket_id, received_at = ticket.ticket_id, ticket.received_at
        return process(ticket)

    except CloudServeError as exc:
        log.warning("ticket %s escalated: %s (%s)", exc.ticket_id or ticket_id, exc.reason_code.value, exc.detail)
        return escalation(exc.ticket_id or ticket_id, exc.reason_code, exc.detail, received_at)

    except Exception as exc:  # noqa: BLE001 - this is the error boundary
        log.exception("unexpected error for ticket %s", ticket_id)
        return escalation(ticket_id, ReasonCode.PIPELINE_ERROR, describe(exc), received_at)