"""Tests for FR-10: every decision is logged, the log is insert-only, and a
logging failure never loses a ticket."""

import json

import sqlite3

import pytest

import cloudserve_support.decision_log as decision_log
from cloudserve_support.harness.__main__ import run
from cloudserve_support.pipeline.graph import handle


def ticket(i=1, **overrides):
    t = {
        "ticket_id": f"T-{i}", "channel": "chat", "subject": "",
        "body": "How long do pagination cursors stay valid when iterating large result sets?",
        "received_at": "2026-03-14T09:22:00Z", "customer_id": "CUST-1", "customer_name": "Priya Sharma",
        "customer_tier": "business", "customer_region": "europe", "language_fluency": "fluent",
    }
    t.update(overrides)
    return t


# TC-10-01: one row per decision, with the fields needed to explain it
def test_tc_10_01_every_decision_is_logged():
    result = handle(ticket(1))
    handle({"ticket_id": "BAD", "channel": "fax"})  # invalid input is logged too
    rows = decision_log.get_decision_log().for_ticket("T-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["route"] == result.route and row["reason_code"] == result.reason_code.value
    assert row["source"] == "api"
    assert row["input_hash"] and row["policy_version"] == "1.0"
    assert row["latency_ms"] is not None and row["intent"]
    assert decision_log.get_decision_log().for_ticket("BAD")[0]["reason_code"] == "INVALID_INPUT"


# TC-10-02: no customer name or id is stored
def test_tc_10_02_no_personal_data_in_log():
    handle(ticket(2))
    row = decision_log.get_decision_log().for_ticket("T-2")[0]
    dumped = json.dumps(row, default=str)
    assert "Priya" not in dumped and "CUST-1" not in dumped


# TC-10-03: the log refuses updates and deletes
def test_tc_10_03_log_is_insert_only():
    handle(ticket(3))
    db = decision_log.get_decision_log().connect()
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        db.execute("UPDATE decisions SET route = 'auto_respond'")
    with pytest.raises(sqlite3.DatabaseError, match="insert-only"):
        db.execute("DELETE FROM decisions")
    db.close()
    assert len(decision_log.get_decision_log().for_ticket("T-3")) == 1


# TC-10-04: if the database can't be written, the ticket still gets its result
def test_tc_10_04_log_failure_does_not_lose_ticket(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(decision_log.DecisionLog, "record", broken)
    result = handle(ticket(4))
    assert result.ticket_id == "T-4" and result.route in {"auto_respond", "escalate"}


def test_log_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    decision_log.reset()
    assert decision_log.record_decision(handle(ticket(5))) is False


def test_harness_run_is_tagged_with_run_id(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps([ticket(6), ticket(7)]), encoding="utf-8")
    run(src, tmp_path / "out.json")
    summary = decision_log.get_decision_log().summary()
    assert summary["total"] == 2
    assert all(s.startswith("harness:in:") for s in summary["sources"])