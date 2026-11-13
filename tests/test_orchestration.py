"""Tests for the pipeline graph: LangGraph orchestration, the sequential
fallback, and the LangChain retriever. Both engines must make the same
decision for every ticket."""

import json
import re
from pathlib import Path

import pytest

import cloudserve_support.pipeline.generate as gen
import cloudserve_support.pipeline.orchestrator as orch
from cloudserve_support.llm import LLMResponse
from cloudserve_support.pipeline import graph
from cloudserve_support.pipeline.lc_retriever import LANGCHAIN_AVAILABLE, search_passages
from cloudserve_support.pipeline.retrieve import get_retriever

DATA = Path(__file__).resolve().parents[1] / "data"


class EchoLLM:
    """Answers with the first resolution step of the first article it is given (always grounded)."""

    def complete(self, system, user, max_tokens=2000):
        docs = re.findall(r'<article doc_id="([^"]+)">\n(.*?)</article>', user, re.S)
        doc_id, body = docs[0]
        step = re.search(r"\n1\. ([^\n]+)", body)
        sentence = step.group(1) if step else body.strip().splitlines()[0]
        reply = {"answerable": True, "answer": sentence, "citations": [{"claim": sentence, "doc_id": doc_id}],
                 "missing_information": None}
        return LLMResponse(json.dumps(reply), "echo/model", False, 1.0)


@pytest.fixture
def engines(monkeypatch):
    monkeypatch.setattr(gen, "get_llm", lambda: EchoLLM())
    yield
    orch.get_orchestrator.cache_clear()


def decisions(tickets, engine, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR", engine)
    orch.get_orchestrator.cache_clear()
    out = []
    for t in tickets:
        r = graph.handle(t).model_dump(mode="json")
        out.append({k: v for k, v in r.items() if k not in {"responded_at", "latency_ms"}})
    return out


def test_langgraph_engine_is_used_by_default(engines, monkeypatch):
    pytest.importorskip("langgraph")
    monkeypatch.delenv("ORCHESTRATOR", raising=False)
    orch.get_orchestrator.cache_clear()
    assert orch.get_orchestrator().engine == "langgraph"


def test_both_engines_make_identical_decisions(engines, monkeypatch):
    pytest.importorskip("langgraph")
    source = DATA / "validation_tickets.json"
    if not source.exists():
        pytest.skip("validation_tickets.json not in data/")
    tickets = json.loads(source.read_text())[:40] + [{"ticket_id": "BAD", "channel": "fax"}]
    with_langgraph = decisions(tickets, "langgraph", monkeypatch)
    sequential = decisions(tickets, "sequential", monkeypatch)
    assert with_langgraph == sequential
    assert {r["reason_code"] for r in with_langgraph} >= {"AUTO_RESPONDED", "POLICY_BLOCKED", "INVALID_INPUT"}


def test_langgraph_unavailable_falls_back_to_sequential(engines, monkeypatch):
    def broken():
        raise ImportError("DLL load failed: blocked by Application Control")

    monkeypatch.setattr(orch, "build_langgraph", broken)
    monkeypatch.delenv("ORCHESTRATOR", raising=False)
    orch.get_orchestrator.cache_clear()
    assert orch.get_orchestrator().engine == "sequential"
    assert graph.handle({
        "ticket_id": "T-1", "channel": "chat", "subject": "", "received_at": "2026-03-14T09:22:00Z",
        "body": "How long do pagination cursors stay valid when iterating large result sets?",
        "customer_id": "C-1", "customer_tier": "business", "customer_region": "europe", "language_fluency": "fluent",
    }).route in {"auto_respond", "escalate"}


@pytest.mark.parametrize("engine", ["langgraph", "sequential"])
def test_kill_switch_branch(engine, engines, monkeypatch):
    if engine == "langgraph":
        pytest.importorskip("langgraph")
    monkeypatch.setenv("KILL_SWITCH", "true")
    [r] = decisions([{
        "ticket_id": "T-K", "channel": "email", "subject": "Invoice", "received_at": "2026-03-14T09:22:00Z",
        "body": "Why is our invoice higher this month?", "customer_id": "C-1", "customer_tier": "standard",
        "customer_region": "europe", "language_fluency": "fluent",
    }], engine, monkeypatch)
    assert r["reason_code"] == "KILL_SWITCH" and r["route"] == "escalate"


def test_graph_tables_are_complete():
    targets = set(orch.EDGES.values()) | {o for _, opts in orch.BRANCHES.values() for o in opts}
    assert targets - {orch.END} <= set(orch.NODES)  # every edge leads to a real node
    assert set(orch.NODES) <= set(orch.EDGES) | set(orch.BRANCHES)  # every node has a way out
    assert "respond" in orch.mermaid() and "escalate" in orch.mermaid()


def test_langchain_retriever_matches_direct_search():
    query = "Our deployment keeps rolling back after the health check."
    assert search_passages(query) == get_retriever().search(query)
    if LANGCHAIN_AVAILABLE:
        from cloudserve_support.pipeline.lc_retriever import CloudServeRetriever

        docs = CloudServeRetriever().invoke(query)
        assert docs and docs[0].metadata["doc_id"].startswith("DOC-")
