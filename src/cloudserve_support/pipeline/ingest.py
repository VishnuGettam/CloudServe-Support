"""Ingest: turn a raw ticket from any channel into one NormalisedTicket.

Serves FR-01. Both front doors (the FastAPI endpoint and the batch harness)
call `ingest`, so live and batch tickets are always treated the same way.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from cloudserve_support.app.schemas import NormalisedTicket, TicketIn
from cloudserve_support.errors import InvalidTicket, safe_ticket_id

__all__ = ["InvalidTicket", "clean_email", "hash_input", "ingest", "mask_name"]

MASK = "[CUSTOMER]"

# Email: everything from a reply header or quoted block onwards is history,
# not the customer's new message.
_REPLY_HEADER = re.compile(
    r"^(on .+ wrote:|-----\s*original message\s*-----|from:\s.+)$",
    re.IGNORECASE,
)
# Email: a signature starts at a sign-off line.
_SIGNOFF = re.compile(
    r"^(--\s*|regards,?|kind regards,?|best regards,?|best,?|thanks,?|"
    r"thank you,?|many thanks,?|cheers,?|sent from my .+)$",
    re.IGNORECASE,
)
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_email(body: str) -> str:
    """Remove quoted replies and signatures from an email body.

    Only cuts when real content remains above the cut, so a short email that
    happens to start with "Thanks," is never emptied.
    """
    kept: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or _REPLY_HEADER.match(stripped):
            break
        if _SIGNOFF.match(stripped) and any(k.strip() for k in kept):
            break
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned or body.strip()


def normalise_whitespace(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def mask_name(text: str, customer_name: str) -> str:
    """Replace the customer's name with [CUSTOMER].

    The full name is matched regardless of case. Single name parts are matched
    only when capitalised, so ordinary words such as "will" or "may" are left
    alone if a customer happens to be called Will or May.
    """
    name = customer_name.strip()
    if not name:
        return text
    text = re.sub(rf"\b{re.escape(name)}\b", MASK, text, flags=re.IGNORECASE)
    for part in sorted(name.split(), key=len, reverse=True):
        if len(part) < 2:
            continue
        text = re.sub(rf"\b{re.escape(part)}\b", MASK, text)
    return text


def hash_input(raw: dict[str, Any]) -> str:
    """Stable fingerprint of the ticket as received, for the decision log."""
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest(raw: dict[str, Any]) -> NormalisedTicket:
    """Validate, clean and mask one raw ticket.

    Raises InvalidTicket if the ticket does not match the schema or has no
    usable text after cleaning. Any other exception is a bug; the caller
    (pipeline.graph.handle) turns it into a PIPELINE_ERROR escalation.
    """
    ticket_id = safe_ticket_id(raw)
    if not isinstance(raw, dict):
        raise InvalidTicket("ticket is not a JSON object", ticket_id)

    try:
        ticket = TicketIn.model_validate(raw)
    except ValidationError as exc:
        # Field names only, never values: values may contain customer text.
        problems = sorted(
            {f"{'.'.join(str(p) for p in err['loc']) or 'ticket'} ({err['type']})" for err in exc.errors()}
        )
        raise InvalidTicket(f"schema validation failed: {', '.join(problems)}", ticket_id) from exc

    body = clean_email(ticket.body) if ticket.channel == "email" else ticket.body
    body = normalise_whitespace(body)
    subject = normalise_whitespace(ticket.subject)

    body = mask_name(body, ticket.customer_name)
    subject = mask_name(subject, ticket.customer_name)

    if not body:
        raise InvalidTicket("ticket body is empty after cleaning", ticket.ticket_id)

    text = f"{subject}\n{body}" if subject else body

    return NormalisedTicket(
        ticket_id=ticket.ticket_id,
        channel=ticket.channel,
        subject=subject,
        body=body,
        text=text,
        received_at=ticket.received_at,
        customer_id=ticket.customer_id,
        customer_tier=ticket.customer_tier,
        customer_region=ticket.customer_region,
        language_fluency=ticket.language_fluency,
        input_hash=hash_input(raw),
    )