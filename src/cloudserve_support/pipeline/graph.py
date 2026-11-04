from datetime import datetime, timezone
from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode, TicketResult


def process(ticket: NormalisedTicket) -> TicketResult:
    """Placeholder pipeline: escalate everything until FR-03 onwards exist."""
    return TicketResult(
        ticket_id=ticket.ticket_id,
        route="escalate",
        reason_code=ReasonCode.LOW_CONFIDENCE,
        detail="pipeline not built yet",
        received_at=ticket.received_at,
        responded_at=datetime.now(timezone.utc),
    )


def invalid_result(ticket_id: str | None, detail: str) -> TicketResult:
    """Turn a rejected ticket into an INVALID_INPUT escalation."""
    return TicketResult(
        ticket_id=ticket_id or "UNKNOWN",
        route="escalate",
        reason_code=ReasonCode.INVALID_INPUT,
        detail=detail,
        responded_at=datetime.now(timezone.utc),
    )


def handle(raw: dict) -> TicketResult:
    """The one entry point the API and the harness both call."""
    from cloudserve_support.pipeline.ingest import InvalidTicket, ingest
    try:
        ticket = ingest(raw)
    except InvalidTicket as exc:
        return invalid_result(exc.ticket_id, exc.detail)
    return process(ticket)