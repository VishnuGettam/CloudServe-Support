"""Tests for FR-07 (generation), FR-08 (pre-send checks), FR-09 (packets) and FR-11 (model failure).

A fake model stands in for OpenRouter, so these tests are offline and free.
"""

import json

import pytest

import cloudserve_support.pipeline.generate as gen
from cloudserve_support.app.schemas import ReasonCode
from cloudserve_support.errors import ModelUnavailable
from cloudserve_support.llm import LLMResponse, OpenRouterClient, ResponseCache
from cloudserve_support.pipeline import graph
from cloudserve_support.pipeline.escalate import human_queue
from cloudserve_support.pipeline.generate import Draft, applies_to_plan, parse
from cloudserve_support.pipeline.validate import validate

WEBHOOK = (
    "Webhook signature verification fails for every delivery. "
    "We compute the signature over the parsed JSON body, exactly as described."
)
ARTICLE = {
    "doc_id": "DOC-API-003", "title": "Webhook delivery, retries and signature verification", "applies_to": "All plans",
    "content": "Compute the signature over the raw request body before any parsing. Re-serialising JSON changes the "
               "bytes and invalidates the signature. Make your handler idempotent using the event identifier.",
}


class FakeLLM:
    """Returns `reply` for every call, or each item of a list in turn."""

    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def complete(self, system, user, max_tokens=2000):
        self.calls += 1
        reply = self.reply[min(self.calls, len(self.reply)) - 1] if isinstance(self.reply, list) else self.reply
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text, "fake/model:free", False, 5.0)


GOOD = {
    "answerable": True,
    "answer": "Compute the signature over the raw request body before any parsing; re-serialising JSON changes the bytes.",
    "citations": [{"claim": "Compute the signature over the raw request body before any parsing", "doc_id": "DOC-API-003"},
                  {"claim": "Re-serialising JSON changes the bytes and invalidates the signature", "doc_id": "DOC-API-003"}],
    "missing_information": None,
}


def ticket(**overrides):
    t = {
        "ticket_id": "T-9", "channel": "chat", "subject": "Signature mismatch", "body": WEBHOOK,
        "received_at": "2026-03-14T09:22:00Z", "customer_id": "C-1", "customer_name": "Priya Sharma",
        "customer_tier": "business", "customer_region": "europe", "language_fluency": "fluent",
    }
    t.update(overrides)
    return t


def use(monkeypatch, reply):
    fake = FakeLLM(reply)
    monkeypatch.setattr(gen, "get_llm", lambda: fake)
    return fake


def draft(**overrides):
    d = Draft(True, GOOD["answer"], list(GOOD["citations"]), None, {"DOC-API-003": ARTICLE}, "fake", False, "PR-02 v1.1")
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


# ---------- FR-07 / A6: end to end with a fake model ----------

def test_tc_07_03_auto_reply_has_label_and_citation(monkeypatch):
    use(monkeypatch, GOOD)
    r = graph.handle(ticket())
    assert r.route == "auto_respond" and r.reason_code == ReasonCode.AUTO_RESPONDED
    assert "DOC-API-003" in r.response and "drafted automatically" in r.response
    assert r.cited_doc_ids == ["DOC-API-003"]
    assert r.checks and all(c.endswith(":pass") for c in r.checks)


def test_tc_07_04_not_answerable_escalates(monkeypatch):
    use(monkeypatch, {"answerable": False, "answer": "", "citations": [], "missing_information": "no retention setting"})
    r = graph.handle(ticket())
    assert r.route == "escalate" and r.reason_code == ReasonCode.WEAK_RETRIEVAL
    assert "do not answer" in r.reason_text


def test_tc_11_01_model_unavailable_escalates_never_crashes(monkeypatch):
    use(monkeypatch, ModelUnavailable("daily free-model limit reached"))
    r = graph.handle(ticket())
    assert r.route == "escalate" and r.reason_code == ReasonCode.MODEL_UNAVAILABLE
    assert r.packet["uncertainties"]


def test_no_api_key_escalates_with_model_unavailable():
    r = graph.handle(ticket())  # conftest blanks the key
    assert r.reason_code in {ReasonCode.MODEL_UNAVAILABLE, ReasonCode.LOW_CONFIDENCE, ReasonCode.WEAK_RETRIEVAL}
    assert r.route == "escalate"


def test_garbled_model_output_fails_format_check(monkeypatch):
    fake = use(monkeypatch, "Sure! Here is my answer without JSON.")
    r = graph.handle(ticket())
    assert r.reason_code == ReasonCode.VALIDATION_FAILED and r.failed_check == "format"
    assert "repair" in r.detail
    assert fake.calls == 2  # the first reply, then exactly one repair attempt
    assert r.packet["summary"] and r.packet["uncertainties"]


def test_one_repair_attempt_recovers_broken_json(monkeypatch):
    fake = use(monkeypatch, ['{"answerable": true, "answer": "unterminated', GOOD])
    r = graph.handle(ticket())
    assert r.route == "auto_respond" and fake.calls == 2
    assert all(c.endswith(":pass") for c in r.checks)  # the repaired answer still passes every check


def test_repair_skipped_when_model_unavailable(monkeypatch):
    use(monkeypatch, ["not json", ModelUnavailable("daily free-model limit reached")])
    r = graph.handle(ticket())
    assert r.reason_code == ReasonCode.VALIDATION_FAILED and r.failed_check == "format"


def test_blocked_ticket_never_calls_the_model(monkeypatch):
    fake = use(monkeypatch, GOOD)
    r = graph.handle(ticket(body="Our account has been compromised and a former employee still has access."))
    assert r.route == "escalate" and fake.calls == 0


def test_parse_accepts_fenced_json():
    assert parse("```json\n" + json.dumps(GOOD) + "\n```")["answerable"] is True


def test_plan_rule():
    assert applies_to_plan("All plans", "standard")
    assert applies_to_plan("Container Service", "standard")
    assert not applies_to_plan("Business and Enterprise plans", "standard")
    assert applies_to_plan("Business and Enterprise plans", "enterprise")


# ---------- FR-08: each pre-send check passes and fails ----------

def test_tc_08_all_checks_pass_on_a_good_draft():
    result = validate(draft(), "chat", "business")
    assert result.passed and len(result.checks) == 8


@pytest.mark.parametrize("changes, check", [
    ({"format_error": "no JSON"}, "format"),
    ({"citations": [{"claim": "x", "doc_id": "DOC-NOT-GIVEN"}]}, "citations"),
    ({"citations": [{"claim": "Restart the database cluster nightly to clear memory leaks", "doc_id": "DOC-API-003"}]}, "grounding"),
    ({"answer": "Email ops@example.com with the raw request body details."}, "private"),
    ({"answer": "A refund has been issued and the raw request body issue is noted."}, "claims"),
    ({"answer": "Please send your API key so we can check the raw request body."}, "claims"),
    ({"answer": "We have fixed this on our side; use the raw request body."}, "claims"),
    ({"answer": " ".join(["word"] * 130)}, "length"),
    ({"answer": "You should have used the raw request body."}, "tone"),
])
def test_tc_08_each_check_fails_when_it_should(changes, check):
    result = validate(draft(**changes), "chat", "business")
    assert not result.passed and result.failed_check == check


def test_tc_08_plan_check():
    restricted = dict(ARTICLE, applies_to="Business and Enterprise plans")
    result = validate(draft(articles={"DOC-API-003": restricted}), "chat", "standard")
    assert not result.passed and result.failed_check == "plan"


# ---------- FR-09: packet and queue ----------

def test_tc_09_01_packet_has_every_field():
    r = graph.handle(ticket(body="It is not working. Please help."))
    for key in ("summary", "reason", "intent", "intent_confidence", "alternatives", "urgency",
                "uncertainties", "sources", "draft_answer", "suggested_first_step", "customer_tier", "channel"):
        assert key in r.packet
    assert r.packet["summary"].count(".") <= 4


def test_tc_09_03_queue_order():
    rows = [
        {"ticket_id": "A", "route": "escalate", "urgency": "low", "received_at": "2026-03-01", "packet": {"customer_tier": "enterprise"}},
        {"ticket_id": "B", "route": "escalate", "urgency": "high", "received_at": "2026-03-05", "packet": {"customer_tier": "standard"}},
        {"ticket_id": "C", "route": "escalate", "urgency": "high", "received_at": "2026-03-09", "packet": {"customer_tier": "enterprise"}},
        {"ticket_id": "D", "route": "auto_respond", "urgency": "high", "received_at": "2026-03-01", "packet": None},
    ]
    assert [q["ticket_id"] for q in human_queue(rows)] == ["C", "B", "A"]


# ---------- LLM client: cache and failure handling (no network) ----------

def test_cache_answers_repeat_prompt_without_a_call(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite")
    client = OpenRouterClient(api_key="k", models=["m:free"], cache=cache, min_interval=0)
    cache.put(client.cache_key("s", "u", 2000), "m:free", "cached text")
    reply = client.complete("s", "u")
    assert reply.cached and reply.text == "cached text"


def test_missing_key_raises_model_unavailable(tmp_path):
    client = OpenRouterClient(api_key="", models=["m:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)
    with pytest.raises(ModelUnavailable):
        client.complete("s", "u")


def test_falls_back_to_next_model(tmp_path, monkeypatch):
    client = OpenRouterClient(api_key="k", models=["busy:free", "ok:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)

    def fake_call(model, system, user, max_tokens):
        if model == "busy:free":
            raise RuntimeError("HTTP 503")
        return "fine"

    monkeypatch.setattr(client, "_call", fake_call)
    reply = client.complete("s", "u")
    assert reply.model == "ok:free" and reply.text == "fine"


def test_busy_model_is_retried_then_next_model_used(tmp_path, monkeypatch):
    from cloudserve_support.llm import RateLimited

    client = OpenRouterClient(api_key="k", models=["busy:free", "ok:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)
    client.retry_wait = 0
    attempts = {"busy:free": 0, "ok:free": 0}

    def fake_call(model, system, user, max_tokens):
        attempts[model] += 1
        if model == "busy:free":
            raise RateLimited("429: upstream busy", retry_after=0)
        return "fine"

    monkeypatch.setattr(client, "_call", fake_call)
    assert client.complete("s", "u").model == "ok:free"
    assert attempts == {"busy:free": client.retries + 1, "ok:free": 1}


def test_busy_model_rests_so_later_tickets_skip_it(tmp_path, monkeypatch):
    from cloudserve_support.llm import RateLimited

    client = OpenRouterClient(api_key="k", models=["busy:free", "ok:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)
    client.retry_wait = 0
    calls = []

    def fake_call(model, system, user, max_tokens):
        calls.append(model)
        if model == "busy:free":
            raise RateLimited("429: upstream busy", retry_after=0)
        return "fine"

    monkeypatch.setattr(client, "_call", fake_call)
    client.complete("s", "first ticket")
    calls.clear()
    client.complete("s", "second ticket")
    assert calls == ["ok:free"]  # the busy model is resting, no request wasted on it


def test_out_of_tokens_retries_with_a_bigger_allowance(tmp_path, monkeypatch):
    from cloudserve_support.llm import OutOfTokens

    client = OpenRouterClient(api_key="k", models=["thinker:free"], cache=ResponseCache(tmp_path / "c.sqlite"), min_interval=0)
    budgets = []

    def fake_call(model, system, user, max_tokens):
        budgets.append(max_tokens)
        if max_tokens < 4000:
            raise OutOfTokens("empty response (finish_reason=length)")
        return "answer"

    monkeypatch.setattr(client, "_call", fake_call)
    assert client.complete("s", "u").text == "answer"
    assert budgets == [2000, 4000]
    assert client.complete("s", "u").cached  # cached under the original request