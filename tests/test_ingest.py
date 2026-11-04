"""Tests for FR-01. Test IDs match the Stage 3 traceability table."""

import json
from pathlib import Path

import pytest

from cloudserve_support.pipeline.ingest import InvalidTicket, clean_email, ingest

DATA = Path(__file__).resolve().parents[1] / "data"


def make_ticket(**overrides):
    ticket = {
        "ticket_id": "T-1",
        "channel": "email",
        "subject": "Cannot log in",
        "body": "I cannot sign in to the console since this morning.",
        "received_at": "2026-03-14T09:22:00Z",
        "customer_id": "CUST-1",
        "customer_name": "Priya Sharma",
        "customer_tier": "business",
        "customer_region": "europe",
        "language_fluency": "fluent",
    }
    ticket.update(overrides)
    return ticket


# TC-01-01: every channel converts
@pytest.mark.parametrize("channel", ["email", "chat", "docs_comment", "forum"])
def test_tc_01_01_each_channel_converts(channel):
    result = ingest(make_ticket(channel=channel))
    assert result.channel == channel
    assert result.text


# TC-01-02: chat with an empty or missing subject
@pytest.mark.parametrize("subject", ["", None])
def test_tc_01_02_empty_chat_subject(subject):
    result = ingest(make_ticket(channel="chat", subject=subject))
    assert result.subject == ""
    assert result.text == result.body


# TC-01-03: malformed tickets raise InvalidTicket, never crash
@pytest.mark.parametrize(
    "bad",
    [
        make_ticket(channel="fax"),
        make_ticket(body="   "),
        make_ticket(customer_tier="gold"),
        {k: v for k, v in make_ticket().items() if k != "received_at"},
        "not a ticket",
    ],
)
def test_tc_01_03_malformed_ticket_is_rejected(bad):
    with pytest.raises(InvalidTicket):
        ingest(bad)


# TC-01-04: the customer's name never reaches the text sent to a model
def test_tc_01_04_name_is_masked():
    result = ingest(
        make_ticket(
            subject="Help for Priya Sharma",
            body="Hi, this is priya sharma. Priya cannot log in. Sharma team.",
        )
    )
    assert "priya" not in result.text.lower()
    assert "sharma" not in result.text.lower()
    assert "[CUSTOMER]" in result.text


def test_lowercase_common_word_is_not_masked():
    result = ingest(make_ticket(customer_name="Will May", body="It will fail in may."))
    assert result.body == "It will fail in may."


def test_email_quoted_reply_and_signature_removed():
    body = (
        "My deployment keeps rolling back.\n\nRegards,\nPriya\n\n"
        "On Mon, 3 Mar 2026, Support wrote:\n> Please send logs"
    )
    assert clean_email(body) == "My deployment keeps rolling back."


def test_email_starting_with_thanks_is_not_emptied():
    assert clean_email("Thanks,\nThe export is still queued.") == (
        "Thanks,\nThe export is still queued."
    )


def test_customer_name_not_stored():
    result = ingest(make_ticket())
    assert "customer_name" not in result.model_dump()


# Every development ticket converts (run once the data file is in data/)
def test_all_development_tickets_convert():
    path = DATA / "development_tickets.json"
    if not path.exists():
        pytest.skip("development_tickets.json not in data/")
    tickets = json.loads(path.read_text(encoding="utf-8"))
    results = [ingest(t) for t in tickets]
    assert len(results) == len(tickets) == 500