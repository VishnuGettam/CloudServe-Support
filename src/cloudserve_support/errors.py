"""One place that defines every error the system raises on purpose.

Each error carries the reason code it should become, so the pipeline turns
any known failure into an escalation with the right code, in one step. An
error that is NOT one of these is a bug, and becomes PIPELINE_ERROR.

Rule: error details never contain ticket text or customer names, only
ticket IDs, field names and short technical messages (NFR-04).
"""

from __future__ import annotations
from typing import Any
from cloudserve_support.app.schemas import ReasonCode

MAX_DETAIL = 300


class CloudServeError(Exception):
    """Base class. Subclasses set the reason code they map to."""

    reason_code: ReasonCode = ReasonCode.PIPELINE_ERROR

    def __init__(self, detail: str, ticket_id: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail[:MAX_DETAIL]
        self.ticket_id = ticket_id


class InvalidTicket(CloudServeError):
    """The ticket cannot be turned into the internal format (FR-01)."""

    reason_code = ReasonCode.INVALID_INPUT


class ModelUnavailable(CloudServeError):
    """The model provider timed out or failed after retries (FR-11). Used from FR-03 onwards."""

    reason_code = ReasonCode.MODEL_UNAVAILABLE


class PipelineError(CloudServeError):
    """A known internal failure inside a pipeline step."""

    reason_code = ReasonCode.PIPELINE_ERROR


def safe_ticket_id(raw: Any) -> str | None:
    """Best-effort ticket ID from raw input, always a short string or None."""
    if isinstance(raw, dict):
        value = raw.get("ticket_id")
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:100]
    return None


def describe(exc: BaseException) -> str:
    """Short, data-free description of an unexpected exception."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"