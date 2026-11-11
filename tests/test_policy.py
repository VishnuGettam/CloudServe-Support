"""Tests for FR-04 (policy gate). IDs match the Stage 3 traceability table."""

import json
from pathlib import Path

import pytest

from cloudserve_support.errors import PipelineError
from cloudserve_support.pipeline.ingest import ingest
from cloudserve_support.pipeline.policy import PolicyGate

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "never_auto.yaml"
DEV = ROOT / "data" / "development_tickets.json"


@pytest.fixture(scope="module")
def gate():
    return PolicyGate.from_file(CONFIG)


# TC-04-01: no must-not-auto-respond ticket passes the gate (using the true intent)
def test_tc_04_01_no_blocked_ticket_passes(gate):
    if not DEV.exists():
        pytest.skip("development_tickets.json not in data/")
    for t in json.loads(DEV.read_text(encoding="utf-8")):
        if t["labels"]["must_not_auto_respond"]:
            assert gate.check(ingest(t).text, t["labels"]["intent"]).blocked, t["ticket_id"]


# TC-04-02: the keyword and unclear rules alone catch at least 90%, with no intent at all
def test_tc_04_02_safety_net_without_classifier(gate):
    if not DEV.exists():
        pytest.skip("development_tickets.json not in data/")
    tickets = [t for t in json.loads(DEV.read_text(encoding="utf-8")) if t["labels"]["must_not_auto_respond"]]
    caught = sum(gate.check(ingest(t).text, None).blocked for t in tickets)
    assert caught / len(tickets) >= 0.90


# TC-04-03: a security ticket misclassified as an API key issue is still caught
def test_tc_04_03_misclassified_security_ticket_caught(gate):
    text = "Key posted on GitHub\nOne of our engineers pushed a key to a public repository and we see calls we don't recognise."
    d = gate.check(text, "api_key_issue")
    assert d.blocked
    assert d.reason_code == "KEYWORD_BLOCKED"
    assert d.rule == "keyword:security"


# TC-04-04: a short ticket that names no product is blocked as unclear
@pytest.mark.parametrize("text", ["It is not working. Please help.", "Following up on my previous message. Any update?"])
def test_tc_04_04_vague_ticket_blocked(gate, text):
    d = gate.check(text, "deployment_failure")
    assert d.blocked and d.rule == "unclear"


def test_blocked_intent_wins_regardless_of_text(gate):
    d = gate.check("How do I export my data?", "feature_request")
    assert d.blocked and d.reason_code == "POLICY_BLOCKED" and d.rule == "intent"


def test_ordinary_ticket_allowed(gate):
    d = gate.check("Webhook signature verification fails on every delivery", "webhook_issue")
    assert not d.blocked
    assert d.sensitive is None
    assert d.checks  # what was checked is recorded even when nothing blocks


def test_sensitive_classes_flagged_not_blocked(gate):
    assert gate.check("Which region is our data stored in?", "data_residency").sensitive == "data_residency"
    d = gate.check("We were charged for a service we do not believe we use. Please refund.", "billing_query")
    assert not d.blocked and d.sensitive == "billing_dispute"


def test_reason_text_is_plain_language(gate):
    d = gate.check("Our account may be compromised", "account_access")
    assert "compromis" in d.reason_text and "human" in d.reason_text


def test_bad_config_raises_pipeline_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(PipelineError):
        PolicyGate.from_file(bad)