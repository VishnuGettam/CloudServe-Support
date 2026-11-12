"""Tests for mandatory caveats: facts an automatic reply must never leave out."""

import json

import pytest

import cloudserve_support.pipeline.generate as gen
from cloudserve_support.errors import PipelineError
from cloudserve_support.llm import LLMResponse
from cloudserve_support.pipeline import graph
from cloudserve_support.pipeline.caveats import get_caveats, load_caveats

DOCS = "data/documentation.json"


def test_every_configured_caveat_is_in_its_article():
    caveats = load_caveats("config/caveats.yaml", DOCS)  # raises if any caveat drifted from the docs
    assert "DOC-DATA-003" in caveats.by_doc
    assert any("stored centrally" in c for c in caveats.by_doc["DOC-DATA-003"])


def test_caveat_not_in_article_stops_startup(tmp_path):
    bad = tmp_path / "caveats.yaml"
    bad.write_text("caveats:\n  DOC-DATA-003:\n    - Your data never leaves the EU.\n", encoding="utf-8")
    with pytest.raises(PipelineError, match="word for word"):
        load_caveats(str(bad), DOCS)


def test_unknown_article_stops_startup(tmp_path):
    bad = tmp_path / "caveats.yaml"
    bad.write_text("caveats:\n  DOC-NOPE-999:\n    - anything\n", encoding="utf-8")
    with pytest.raises(PipelineError, match="no such article"):
        load_caveats(str(bad), DOCS)


def test_missing_file_means_no_caveats(tmp_path):
    assert load_caveats(str(tmp_path / "absent.yaml"), DOCS).by_doc == {}


def test_for_docs_keeps_citation_order_without_repeats():
    pairs = get_caveats().for_docs(["DOC-DEPLOY-002", "DOC-DATA-003", "DOC-DEPLOY-002", "DOC-API-002"])
    assert [d for d, _ in pairs] == ["DOC-DEPLOY-002", "DOC-DATA-003", "DOC-DATA-003"]


# The VAL-0026 case: a grounded residency answer must also carry the facts it left out.
def test_residency_reply_carries_mandatory_caveats(monkeypatch):
    answer = {
        "answerable": True,
        "answer": "Check the region shown on the organisation settings page; it is fixed at creation and determines "
                  "where primary data is stored.",
        "citations": [{"claim": "Check the region shown on the organisation settings page. This is fixed at creation "
                                "and determines where primary data is stored.", "doc_id": "DOC-DATA-003"}],
        "missing_information": None,
    }

    class FakeLLM:
        def complete(self, system, user, max_tokens=2000):
            return LLMResponse(json.dumps(answer), "fake/model:free", False, 5.0)

    monkeypatch.setattr(gen, "get_llm", lambda: FakeLLM())
    r = graph.handle({
        "ticket_id": "T-EU", "channel": "email", "subject": "Need data to stay in the EU",
        "body": "We need our data to remain within the EU for a contract we are signing. Which region is our organisation in?",
        "received_at": "2026-03-14T09:22:00Z", "customer_id": "C-1", "customer_tier": "business",
        "customer_region": "europe", "language_fluency": "fluent",
    })
    if r.route != "auto_respond":
        pytest.skip(f"routing sent this ticket to a human ({r.reason_code.value}); caveats apply to automatic replies only")
    assert r.caveat_doc_ids == ["DOC-DATA-003"]
    assert "stored centrally regardless of the data region" in r.response
    assert "planned migration with support" in r.response
    assert r.response.index("Important to know") < r.response.index("Sources:")  # under the answer, before sources
    assert "caveats:pass" in r.checks
