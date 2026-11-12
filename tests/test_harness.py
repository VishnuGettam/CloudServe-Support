"""Tests for FR-02 (harness) and TC-01-05 (API). IDs match the Stage 3 table."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cloudserve_support.pipeline.graph as graph
from cloudserve_support.app.main import app
from cloudserve_support.harness.__main__ import HarnessError, main, progress_path, run

DATA = Path(__file__).resolve().parents[1] / "data"


def make_ticket(i: int, **overrides):
    ticket = {
        "ticket_id": f"T-{i}",
        "channel": "chat",
        "subject": "",
        "body": f"Our deployment {i} keeps rolling back after the health check.",
        "received_at": "2026-03-14T09:22:00Z",
        "customer_id": "CUST-1",
        "customer_name": "Priya Sharma",
        "customer_tier": "business",
        "customer_region": "europe",
        "language_fluency": "fluent",
    }
    ticket.update(overrides)
    return ticket


def write(path: Path, tickets) -> Path:
    path.write_text(json.dumps(tickets), encoding="utf-8")
    return path


# TC-02-01: one result per ticket, nothing dropped (on the real dev file)
def test_tc_02_01_output_count_equals_input(tmp_path):
    source = DATA / "development_tickets.json"
    if not source.exists():
        pytest.skip("development_tickets.json not in data/")
    out = tmp_path / "results.json"
    run(source, out)
    results = json.loads(out.read_text())
    tickets = json.loads(source.read_text())
    assert len(results) == len(tickets)
    assert [r["ticket_id"] for r in results] == [t["ticket_id"] for t in tickets]


# TC-02-02: works on any path given on the command line (no hardcoding)
def test_tc_02_02_runs_on_temporary_paths(tmp_path):
    src = write(tmp_path / "in.json", [make_ticket(1), make_ticket(2)])
    out = tmp_path / "nested" / "dir" / "out.json"
    completed = subprocess.run(
        [sys.executable, "-m", "cloudserve_support.harness", "--input", str(src), "--output", str(out)],
        capture_output=True,
        text=True,
        env={**os.environ, "OPENROUTER_API_KEY": ""},  # never call the real model from tests
    )
    assert completed.returncode == 0, completed.stderr
    assert len(json.loads(out.read_text())) == 2


# TC-02-03: a malformed ticket and a crashing ticket do not stop the run
def test_tc_02_03_bad_tickets_do_not_stop_run(tmp_path, monkeypatch):
    real_process = graph.process

    def flaky(ticket):
        if ticket.ticket_id == "T-3":
            raise RuntimeError("boom")
        return real_process(ticket)

    monkeypatch.setattr(graph, "process", flaky)
    tickets = [make_ticket(1), make_ticket(2, channel="fax"), make_ticket(3), "junk", make_ticket(5)]
    out = tmp_path / "out.json"
    results = run(write(tmp_path / "in.json", tickets), out)

    assert len(results) == 5
    codes = [r["reason_code"] for r in results]
    assert codes[1] == "INVALID_INPUT"
    assert codes[2] == "PIPELINE_ERROR"
    assert codes[3] == "INVALID_INPUT"
    assert results[4]["ticket_id"] == "T-5"


# TC-02-04: an interrupted run resumes without redoing finished tickets
def test_tc_02_04_resume_after_interruption(tmp_path, monkeypatch):
    tickets = [make_ticket(i) for i in range(5)]
    src = write(tmp_path / "in.json", tickets)
    out = tmp_path / "out.json"

    real_handle = graph.handle
    calls = {"n": 0}

    def crash_on_fourth(raw, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt  # simulate the process being stopped
        return real_handle(raw, **kwargs)

    import cloudserve_support.harness.__main__ as harness

    monkeypatch.setattr(harness, "handle", crash_on_fourth)
    with pytest.raises(KeyboardInterrupt):
        run(src, out)
    assert not out.exists()
    assert progress_path(out).exists()

    seen = []

    def counting(raw, **kwargs):
        seen.append(raw["ticket_id"])
        return real_handle(raw, **kwargs)

    monkeypatch.setattr(harness, "handle", counting)
    results = run(src, out, resume=True)

    assert seen == ["T-3", "T-4"]  # only the unfinished tickets were processed
    assert [r["ticket_id"] for r in results] == [f"T-{i}" for i in range(5)]
    assert not progress_path(out).exists()


# ---------- run-level errors: clear message and exit code, no traceback ----------

def test_missing_input_file_exit_code_2(tmp_path):
    assert main(["--input", str(tmp_path / "missing.json"), "--output", str(tmp_path / "o.json")]) == 2


def test_invalid_json_input_exit_code_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(HarnessError) as err:
        run(bad, tmp_path / "out.json")
    assert err.value.exit_code == 2


def test_output_same_as_input_is_refused(tmp_path):
    src = write(tmp_path / "in.json", [make_ticket(1)])
    with pytest.raises(HarnessError):
        run(src, src)
    assert json.loads(src.read_text())[0]["ticket_id"] == "T-1"  # input untouched


def test_output_is_a_folder_is_refused(tmp_path):
    src = write(tmp_path / "in.json", [make_ticket(1)])
    with pytest.raises(HarnessError):
        run(src, tmp_path)


def test_progress_from_a_different_input_is_not_reused(tmp_path):
    out = tmp_path / "out.json"
    first = write(tmp_path / "a.json", [make_ticket(1), make_ticket(2)])
    progress_path(out).write_text(
        json.dumps({"meta": {"input_sha256": "other-file", "count": 2}}) + "\n"
        + json.dumps({"index": 0, "result": {"ticket_id": "WRONG", "route": "escalate", "reason_code": "X"}}) + "\n",
        encoding="utf-8",
    )
    results = run(first, out, resume=True)
    assert [r["ticket_id"] for r in results] == ["T-1", "T-2"]


def test_ctrl_c_returns_130_and_keeps_progress(tmp_path, monkeypatch):
    import cloudserve_support.harness.__main__ as harness

    src = write(tmp_path / "in.json", [make_ticket(1), make_ticket(2)])
    out = tmp_path / "out.json"
    monkeypatch.setattr(harness, "handle", lambda raw, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
    assert main(["--input", str(src), "--output", str(out)]) == 130
    assert progress_path(out).exists()
    assert not out.exists()


# TC-01-05: POST /tickets returns a decision; a bad ticket gets INVALID_INPUT, not a crash
def test_tc_01_05_api_returns_decision():
    client = TestClient(app)
    ok = client.post("/tickets", json=make_ticket(1))
    assert ok.status_code == 200
    assert ok.json()["ticket_id"] == "T-1"
    assert ok.json()["route"] in {"auto_respond", "escalate"}

    bad = client.post("/tickets", json={"ticket_id": "X", "channel": "fax"})
    assert bad.status_code == 200
    assert bad.json()["reason_code"] == "INVALID_INPUT"

    assert client.get("/health").json() == {"status": "ok"}
