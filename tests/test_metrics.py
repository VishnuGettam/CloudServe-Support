"""Tests for A10: the metrics report has all four sections, measures against
labels when present, and works without them."""

import json

from cloudserve_support.harness.__main__ import run
from cloudserve_support.metrics import build_report, latency_path, to_markdown


def result(i, route="escalate", reason="POLICY_BLOCKED", **extra):
    r = {"ticket_id": f"T-{i}", "route": route, "reason_code": reason, "intent": "billing_query",
         "urgency": "low", "latency_ms": 10.0, "customer_tier": "business", "customer_region": "europe",
         "language_fluency": "fluent", "channel": "chat", "doc_ids": ["DOC-BILL-001"], "cited_doc_ids": [],
         "checks": [], "packet": {"summary": "x"} if route == "escalate" else None}
    r.update(extra)
    return r


AUTO = dict(route="auto_respond", reason="AUTO_RESPONDED", model="m:free", cached=False, latency_ms=30000.0,
            cited_doc_ids=["DOC-BILL-001"], checks=["format:pass", "citations:pass"], packet=None,
            response="Answer.\n\nThis reply was drafted automatically by CloudServe's AI support assistant from our help articles.")


def test_report_has_four_sections_without_labels():
    report = build_report([result(1), result(2, **AUTO)])
    assert {"volume", "business", "technical", "governance"} <= report.keys()
    assert report["business"]["automation_rate"] == 0.5
    assert report["technical"]["accuracy"] == "no labels in the input file"
    assert report["governance"]["auto_replies_with_ai_label"] == 1.0
    assert "## Governance" in to_markdown(report)


def test_labels_measure_accuracy_and_safety():
    tickets = [
        {"ticket_id": "T-1", "labels": {"intent": "billing_query", "urgency": "low", "expected_route": "escalate",
                                        "answerable_from_docs": False, "expected_doc_ids": [], "must_not_auto_respond": True}},
        {"ticket_id": "T-2", "labels": {"intent": "billing_query", "urgency": "high", "expected_route": "auto_respond",
                                        "answerable_from_docs": True, "expected_doc_ids": ["DOC-BILL-001"],
                                        "must_not_auto_respond": False}},
    ]
    report = build_report([result(1), result(2, **AUTO)], tickets)
    assert report["technical"]["intent_accuracy"] == 1.0
    assert report["technical"]["urgency_agreement"] == 0.5
    assert report["technical"]["route_agreement"] == 1.0
    assert report["technical"]["citation_accuracy"] == 1.0
    assert report["governance"]["must_not_auto_answered_automatically"] == []
    assert report["business"]["auto_answers_supported_by_labels"] == 1.0


def test_unsafe_automation_is_reported():
    tickets = [{"ticket_id": "T-1", "labels": {"intent": "security_incident", "expected_route": "escalate",
                                              "answerable_from_docs": False, "expected_doc_ids": [],
                                              "must_not_auto_respond": True}}]
    report = build_report([result(1, **AUTO)], tickets)
    assert report["governance"]["must_not_auto_answered_automatically"] == ["T-1"]
    assert report["governance"]["answered_although_docs_cannot_answer"] == ["T-1"]


def test_latency_paths_are_separated():
    assert latency_path(result(1)) == "no_model_call"
    assert latency_path(result(2, **AUTO)) == "fresh_model_call"
    assert latency_path(result(3, **{**AUTO, "cached": True})) == "cached_model_answer"
    assert latency_path(result(4, reason="MODEL_UNAVAILABLE")) == "model_failed"


def test_fairness_flags_a_group_far_from_overall():
    rows = [result(i, customer_tier="standard") for i in range(6)] + \
           [result(10 + i, customer_tier="enterprise", **AUTO) for i in range(6)]
    flags = build_report(rows)["governance"]["fairness"]["flags"]
    assert any("customer_tier=standard" in f for f in flags)
    assert any("customer_tier=enterprise" in f for f in flags)


def test_harness_writes_metrics_files(tmp_path):
    t = {"ticket_id": "T-1", "channel": "chat", "subject": "", "body": "Our deploy keeps rolling back.",
         "received_at": "2026-03-14T09:22:00Z", "customer_id": "CUST-1", "customer_tier": "business",
         "customer_region": "europe", "language_fluency": "fluent"}
    src = tmp_path / "in.json"
    src.write_text(json.dumps([t]), encoding="utf-8")
    run(src, tmp_path / "out.json")
    assert json.loads((tmp_path / "out.metrics.json").read_text())["tickets"] == 1
    assert "## Volume" in (tmp_path / "out.metrics.md").read_text()


def test_small_latencies_are_not_shown_as_percentages():
    from cloudserve_support.metrics import _fmt

    assert _fmt(0.9, "p95_ms") == "0.9"
    assert _fmt(0.42, "automation_rate") == "42.0%"


def test_fairness_with_labels_compares_each_group_with_its_own_expectation():
    # standard tickets mostly SHOULD escalate, so a low automation rate there is not a flag
    rows = [result(i, customer_tier="standard") for i in range(6)] + \
           [result(10 + i, customer_tier="business", **AUTO) for i in range(6)]
    tickets = [{"ticket_id": f"T-{i}", "labels": {"intent": "billing_query", "expected_route": "escalate"}} for i in range(6)] + \
              [{"ticket_id": f"T-{10 + i}", "labels": {"intent": "billing_query", "expected_route": "auto_respond"}} for i in range(6)]
    fair = build_report(rows, tickets)["governance"]["fairness"]
    assert fair["flags"] == []
    assert fair["groups"]["customer_tier"]["standard"]["gap_vs_expected"] == 0.0