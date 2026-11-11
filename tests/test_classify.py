"""Tests for FR-03 / A3 / A5. IDs match the Stage 3 traceability table."""

from pathlib import Path

import pytest

from cloudserve_support.pipeline.classify import FALLBACK_INTENT, NeighbourClassifier, load_labelled

DEV = Path(__file__).resolve().parents[1] / "data" / "development_tickets.json"
pytestmark = pytest.mark.skipif(not DEV.exists(), reason="development_tickets.json not in data/")


@pytest.fixture(scope="module")
def classifier():
    return NeighbourClassifier(load_labelled(DEV))


# A3: class plus a confidence between 0 and 1, with alternatives recorded
def test_tc_03_01_output_has_intent_urgency_confidence(classifier):
    r = classifier.classify("My API key returns 401 unauthorized since this morning")
    assert r.intent == "api_key_issue"
    assert 0.0 <= r.intent_confidence <= 1.0
    assert r.urgency in {"low", "medium", "high"}
    assert r.alternatives and r.alternatives[0][0] == r.intent


# A5: same input, same output
def test_tc_03_02_deterministic(classifier):
    text = "Our deployment keeps rolling back after the health check"
    assert classifier.classify(text) == classifier.classify(text)


# Build spec: a defined fallback instead of an exception
@pytest.mark.parametrize("text", ["", "   ", "zzqx"])
def test_tc_03_03_fallback_not_exception(classifier, text):
    r = classifier.classify(text)
    assert r.intent == FALLBACK_INTENT
    assert r.intent_confidence == 0.0


# Held-out accuracy, with duplicate bodies kept out of training
def test_tc_03_04_held_out_accuracy():
    tickets = load_labelled(DEV)
    bodies = sorted({t["body"].strip().lower() for t in tickets})
    hold = set(bodies[::5])
    train = [t for t in tickets if t["body"].strip().lower() not in hold]
    test = [t for t in tickets if t["body"].strip().lower() in hold]
    model = NeighbourClassifier(train)
    correct = [model.classify(f"{t['subject']}\n{t['body']}").intent == t["labels"]["intent"] for t in test]
    assert sum(correct) / len(correct) >= 0.85