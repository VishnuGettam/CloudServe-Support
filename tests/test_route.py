"""Tests for FR-06 routing and the wired pipeline (A5, A11). IDs match the Stage 3 table."""

import pytest

import cloudserve_support.pipeline.graph as graph
from cloudserve_support.app.schemas import ReasonCode
from cloudserve_support.pipeline.classify import Classification
from cloudserve_support.pipeline.policy import PolicyDecision
from cloudserve_support.pipeline.retrieve import Passage
from cloudserve_support.pipeline.route import decide

PASSAGE = Passage("DOC-API-003", "DOC-API-003#resolution", "Webhooks", "Resolution", "All plans", "text", 0.62, 4.1)
ALLOWED = PolicyDecision(False, None, None, None, None, ["intent:webhook_issue"], "1.0")


def cls(conf, intent="webhook_issue"):
    return Classification(intent, conf, "medium", 0.6, [(intent, 0.9), ("integration_help", 0.1)])


def ticket(**overrides):
    t = {
        "ticket_id": "T-1", "channel": "chat", "subject": "",
        "body": "Webhook signature verification fails for every delivery. We compute it over the parsed body.",
        "received_at": "2026-03-14T09:22:00Z", "customer_id": "C-1", "customer_name": "Priya Sharma",
        "customer_tier": "business", "customer_region": "europe", "language_fluency": "fluent",
    }
    t.update(overrides)
    return t


# TC-06-03: every branch gives exactly one reason code and a plain-language reason
def test_tc_06_03_branches():
    blocked = PolicyDecision(True, ReasonCode.KEYWORD_BLOCKED, "keyword:security", "breach", None, [], "1.0")
    sensitive = PolicyDecision(False, None, None, None, "data_residency", [], "1.0")
    cases = [
        (decide(cls(0.99), blocked, [PASSAGE]), "escalate", ReasonCode.KEYWORD_BLOCKED),
        (decide(cls(0.99), ALLOWED, []), "escalate", ReasonCode.WEAK_RETRIEVAL),
        (decide(cls(0.85, "data_residency"), sensitive, [PASSAGE]), "escalate", ReasonCode.SENSITIVE_CLASS),
        (decide(cls(0.50), ALLOWED, [PASSAGE]), "escalate", ReasonCode.LOW_CONFIDENCE),
        (decide(cls(0.95), ALLOWED, [PASSAGE]), "auto_respond", ReasonCode.AUTO_RESPONDED),
    ]
    for d, route, code in cases:
        assert d.route == route and d.reason_code == code
        assert d.reason_text and len(d.reason_text) > 20


def test_low_confidence_reason_names_the_alternative():
    d = decide(cls(0.5), ALLOWED, [PASSAGE])
    assert "integration help" in d.reason_text


def test_thresholds_come_from_configuration(monkeypatch):
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.40")
    assert decide(cls(0.5), ALLOWED, [PASSAGE]).route == "auto_respond"


# A5: the same ticket twice gives the same decision
def test_tc_06_05_same_ticket_same_decision():
    first, second = graph.handle(ticket()), graph.handle(ticket())
    keys = ["route", "reason_code", "intent", "intent_confidence", "doc_ids", "policy_rule"]
    assert {k: getattr(first, k) for k in keys} == {k: getattr(second, k) for k in keys}


def test_pipeline_result_is_filled_in():
    r = graph.handle(ticket())
    assert r.intent and r.intent_confidence is not None and r.reason_text
    assert r.latency_ms is not None and r.latency_ms >= 0
    if r.route == "escalate":
        assert r.packet and "sources" in r.packet


def test_security_ticket_escalates_end_to_end():
    r = graph.handle(ticket(body="One of our engineers pushed a key to a public repository and we see calls we don't recognise."))
    assert r.route == "escalate"
    assert r.reason_code in {ReasonCode.KEYWORD_BLOCKED, ReasonCode.POLICY_BLOCKED}
    assert r.packet["policy_checks"]


# TC-11-02: kill switch sends everything to a human
def test_tc_11_02_kill_switch(monkeypatch):
    monkeypatch.setenv("KILL_SWITCH", "true")
    r = graph.handle(ticket())
    assert r.route == "escalate" and r.reason_code == ReasonCode.KILL_SWITCH


@pytest.mark.parametrize("value", ["false", "", "0"])
def test_kill_switch_off_values(monkeypatch, value):
    monkeypatch.setenv("KILL_SWITCH", value)
    assert not graph.kill_switch_on()