"""Shared test settings.

Tests must never call the real model provider or use the real response
cache: the key is blanked and the cache and the decision log point at temporary files. A real
key in .env is therefore never used by the test suite.
"""

import pytest

import cloudserve_support.decision_log as decision_log
import cloudserve_support.llm as llm


@pytest.fixture(autouse=True)
def offline_model(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "llm_cache.sqlite"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'decisions.db').as_posix()}")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    llm.get_llm.cache_clear()
    decision_log.reset()
    yield
    llm.get_llm.cache_clear()
    decision_log.reset()
