"""Tests for the Prometheus metrics: the API exposes them, tickets and model
requests are counted, and the harness writes them after a run."""

import json

import pytest
from fastapi.testclient import TestClient

prometheus_client = pytest.importorskip("prometheus_client")
from prometheus_client import REGISTRY  # noqa: E402

from cloudserve_support.app.main import app  # noqa: E402
from cloudserve_support.harness.__main__ import run  # noqa: E402
from cloudserve_support.llm import OpenRouterClient, ResponseCache  # noqa: E402
from cloudserve_support.pipeline.graph import handle  # noqa: E402

TICKET = {
    "ticket_id": "M-1", "channel": "chat", "subject": "", "received_at": "2026-03-14T09:22:00Z",
    "body": "We think someone accessed our account and we need help now, there was a suspicious login.",
    "customer_id": "C-1", "customer_tier": "enterprise", "customer_region": "europe", "language_fluency": "fluent",
}


def value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_tickets_are_counted_by_route_reason_and_group():
    labels = dict(route="escalate", reason_code="POLICY_BLOCKED", customer_tier="enterprise", language_fluency="fluent")
    before = value("cloudserve_tickets_total", **labels)
    result = handle(TICKET)  # a security incident: always blocked before any model call
    assert result.route == "escalate" and result.reason_code.value == "POLICY_BLOCKED"
    assert value("cloudserve_tickets_total", **labels) == before + 1
    assert value("cloudserve_ticket_latency_seconds_count", path="no_model_call") >= 1


def test_api_exposes_metrics():
    client = TestClient(app)
    client.post("/tickets", json=TICKET)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "cloudserve_tickets_total" in response.text
    assert "cloudserve_ticket_latency_seconds_bucket" in response.text


def test_model_requests_are_counted(tmp_path, monkeypatch):
    client = OpenRouterClient(api_key="k", models=["m:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)
    monkeypatch.setattr(client, "_call", lambda *a: "fine")
    ok_before = value("cloudserve_model_requests_total", model="m:free", outcome="ok")
    hit_before = value("cloudserve_model_requests_total", model="m:free", outcome="cache_hit")
    client.complete("s", "u")
    client.complete("s", "u")
    assert value("cloudserve_model_requests_total", model="m:free", outcome="ok") == ok_before + 1
    assert value("cloudserve_model_requests_total", model="m:free", outcome="cache_hit") == hit_before + 1


def test_harness_writes_prometheus_file_and_survives_bad_pushgateway(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps([TICKET]), encoding="utf-8")
    run(src, tmp_path / "out.json", pushgateway="http://127.0.0.1:9")  # nothing listens there: must not fail
    text = (tmp_path / "out.prom").read_text()
    assert "cloudserve_tickets_total" in text
