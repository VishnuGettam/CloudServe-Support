"""Error boundary tests: handle() and the API always return one result, never crash."""

from fastapi.testclient import TestClient

import cloudserve_support.pipeline.graph as graph
from cloudserve_support.app.main import app
from cloudserve_support.errors import ModelUnavailable, safe_ticket_id


def ticket(**overrides):
    t = {
        "ticket_id": "T-1", "channel": "chat", "subject": "",
        "body": "Webhooks stopped arriving since last night.",
        "received_at": "2026-03-14T09:22:00Z", "customer_id": "C-1",
        "customer_name": "Priya Sharma", "customer_tier": "standard",
        "customer_region": "asia_pacific", "language_fluency": "fluent",
    }
    t.update(overrides)
    return t


def test_numeric_ticket_id_on_bad_ticket_does_not_crash():
    result = graph.handle({"ticket_id": 123, "channel": "fax"})
    assert result.ticket_id == "123"
    assert result.reason_code == "INVALID_INPUT"


def test_unexpected_error_becomes_pipeline_error(monkeypatch):
    monkeypatch.setattr(graph, "process", lambda t: 1 / 0)
    result = graph.handle(ticket())
    assert result.reason_code == "PIPELINE_ERROR"
    assert "ZeroDivisionError" in result.detail
    assert result.ticket_id == "T-1"


def test_unexpected_error_inside_ingest_is_caught(monkeypatch):
    def broken(raw):
        raise RuntimeError("cleaner bug")
    monkeypatch.setattr(graph, "ingest", broken)
    assert graph.handle(ticket()).reason_code == "PIPELINE_ERROR"


def test_known_error_keeps_its_reason_code(monkeypatch):
    def unavailable(t):
        raise ModelUnavailable("provider timed out", t.ticket_id)
    monkeypatch.setattr(graph, "process", unavailable)
    assert graph.handle(ticket()).reason_code == "MODEL_UNAVAILABLE"


def test_validation_detail_never_contains_ticket_text():
    secret = "my private stack trace with token abc123"
    result = graph.handle(ticket(channel="fax", body=secret))
    assert "abc123" not in result.detail
    assert "channel" in result.detail


def test_safe_ticket_id():
    assert safe_ticket_id({"ticket_id": " A-1 "}) == "A-1"
    assert safe_ticket_id({"ticket_id": None}) is None
    assert safe_ticket_id("junk") is None


def test_api_invalid_json_body_returns_invalid_input():
    client = TestClient(app)
    response = client.post("/tickets", content="{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json()["reason_code"] == "INVALID_INPUT"


def test_api_empty_body_returns_invalid_input():
    response = TestClient(app).post("/tickets")
    assert response.json()["reason_code"] == "INVALID_INPUT"


def test_api_unexpected_server_error_returns_500_with_same_shape(monkeypatch):
    import cloudserve_support.app.main as main

    def explode(raw):
        raise RuntimeError("boom")
    monkeypatch.setattr(main, "handle", explode)
    response = TestClient(app, raise_server_exceptions=False).post("/tickets", json=ticket())
    assert response.status_code == 500
    assert response.json()["reason_code"] == "PIPELINE_ERROR"