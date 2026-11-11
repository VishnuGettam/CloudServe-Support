"""Data models shared by the API, the harness and the pipeline.

Serves FR-01 (one internal format for all four channels) and defines the
reason codes used by FR-06, FR-09, FR-10 and FR-11.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Channel = Literal["email", "chat", "docs_comment", "forum"]
Tier = Literal["enterprise", "business", "standard"]
Region = Literal["north_america", "europe", "asia_pacific", "latin_america"]
Fluency = Literal["fluent", "non_fluent"]
Route = Literal["auto_respond", "escalate"]


class ReasonCode(str, Enum):
    """Exactly one of these is attached to every ticket's outcome."""

    AUTO_RESPONDED = "AUTO_RESPONDED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    KEYWORD_BLOCKED = "KEYWORD_BLOCKED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    WEAK_RETRIEVAL = "WEAK_RETRIEVAL"
    SENSITIVE_CLASS = "SENSITIVE_CLASS"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    KILL_SWITCH = "KILL_SWITCH"
    INVALID_INPUT = "INVALID_INPUT"  # added in Stage 3; revision log entry 1
    PIPELINE_ERROR = "PIPELINE_ERROR"  # added in build; revision log entry 2


class TicketIn(BaseModel):
    """A ticket exactly as it arrives, in the dataset schema.

    Extra fields such as `labels` and `history` are ignored here, so the same
    model accepts live tickets and labelled evaluation files.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    ticket_id: str = Field(min_length=1)
    channel: Channel
    subject: str = ""  # chat tickets arrive with an empty subject
    body: str = Field(min_length=1)
    received_at: datetime
    customer_id: str = Field(min_length=1)
    customer_name: str = ""
    customer_tier: Tier
    customer_region: Region
    language_fluency: Fluency

    @field_validator("subject", mode="before")
    @classmethod
    def none_subject_is_empty(cls, value: Any) -> Any:
        return "" if value is None else value


class NormalisedTicket(BaseModel):
    """The single internal format every later step works from.

    `text` has been cleaned and has the customer's name masked, so it is safe
    to send to an external model. The original name is never stored here.
    """

    ticket_id: str
    channel: Channel
    subject: str
    body: str
    text: str
    received_at: datetime
    customer_id: str
    customer_tier: Tier
    customer_region: Region
    language_fluency: Fluency
    input_hash: str


class TicketResult(BaseModel):
    """One output row per ticket, written by the harness and returned by the API."""

    ticket_id: str
    route: Route
    reason_code: ReasonCode
    reason_text: str | None = None  # plain language, for a support manager
    intent: str | None = None
    urgency: str | None = None
    intent_confidence: float | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)  # intents considered, with vote shares
    doc_ids: list[str] = Field(default_factory=list)
    retrieval_scores: list[float] = Field(default_factory=list)
    policy_rule: str | None = None
    sensitive: str | None = None
    response: str | None = None
    packet: dict[str, Any] | None = None
    failed_check: str | None = None
    detail: str | None = None
    received_at: datetime | None = None
    responded_at: datetime
    latency_ms: float | None = None  # system processing time; used for time to first reply