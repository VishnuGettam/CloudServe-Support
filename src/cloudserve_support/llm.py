"""Language-model client for OpenRouter's free models (FR-07, FR-11, A11).

- Several models are tried in order (LLM_MODELS, comma-separated). If one is
  busy, missing or failing, the next is tried.
- Every response is cached on disk, keyed by the exact prompt. Re-runs cost
  nothing, and a repeated ticket gets the same answer (A5).
- Requests are spaced to stay under the free tier's 20 requests per minute.
- A temporary 429 (the provider behind a free model is busy) is retried on
  the same model after a short wait, then the next model is tried.
- A model that stays busy is rested for LLM_COOLDOWN seconds, so later tickets
  skip it instead of spending time and daily quota on requests that will fail.
- A reasoning model that runs out of tokens before answering (empty reply,
  finish_reason=length) is retried once with double the token allowance.
- Any failure (no key, daily limit reached, every model failing) raises
  ModelUnavailable, which the pipeline turns into an escalation. The system
  never stops because the model is unavailable.

List the free models currently offered:
    uv run python -m cloudserve_support.llm --list-free
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from cloudserve_support.errors import ModelUnavailable
from cloudserve_support.observability import count_model_request

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = "nvidia/nemotron-3.5-lightning:free,google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free"
MAX_TOKENS_CAP = 4000
DEFAULT_CACHE = "storage/llm_cache.sqlite"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    cached: bool
    latency_ms: float


class ResponseCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT, text TEXT, created REAL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT model, text FROM responses WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, key: str, model: str, text: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?)", (key, model, text, time.time()))


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        models: list[str] | None = None,
        cache: ResponseCache | None = None,
        timeout: float | None = None,
        min_interval: float | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "")).strip()
        self.models = models or [m.strip() for m in os.getenv("LLM_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
        self.cache = cache or ResponseCache(os.getenv("LLM_CACHE_PATH", DEFAULT_CACHE))
        self.timeout = timeout or float(os.getenv("LLM_TIMEOUT", 30))
        self.min_interval = float(os.getenv("LLM_MIN_INTERVAL", 3.2)) if min_interval is None else min_interval
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "low")
        self.retries = int(os.getenv("LLM_RETRIES", 1))  # extra attempts per model after a temporary 429
        self.retry_wait = float(os.getenv("LLM_RETRY_WAIT", 8))
        self.cooldown = float(os.getenv("LLM_COOLDOWN", 120))
        self._last_call = 0.0
        self._daily_limit_hit = False
        self._resting_until: dict[str, float] = {}

    @staticmethod
    def cache_key(system: str, user: str, max_tokens: int) -> str:
        return hashlib.sha256(json.dumps([system, user, max_tokens], ensure_ascii=False).encode()).hexdigest()

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> LLMResponse:
        started = time.perf_counter()
        key = self.cache_key(system, user, max_tokens)
        hit = self.cache.get(key)
        if hit:
            count_model_request(hit[0], "cache_hit")
            return LLMResponse(hit[1], hit[0], True, round((time.perf_counter() - started) * 1000, 1))

        if not self.api_key or self.api_key == "your_key_here":
            count_model_request("none", "no_key")
            raise ModelUnavailable("OPENROUTER_API_KEY is not set")
        if self._daily_limit_hit:
            count_model_request("none", "daily_limit")
            raise ModelUnavailable("daily free-model limit reached earlier in this run")

        problems: list[str] = []
        for model in self.models:
            rest = self._resting_until.get(model, 0) - time.monotonic()
            if rest > 0:
                problems.append(f"{model}: resting {rest:.0f}s after repeated 429s")
                continue
            budget = max_tokens
            for attempt in range(self.retries + 1):
                try:
                    text = self._call(model, system, user, budget)
                except ModelUnavailable:
                    count_model_request(model, "daily_limit")
                    raise
                except RateLimited as exc:
                    count_model_request(model, "rate_limited")
                    # Free models are shared: the provider behind a model often
                    # rejects requests for a few seconds. Wait and retry the same
                    # model before moving to the next one.
                    if attempt < self.retries:
                        wait = exc.retry_after or self.retry_wait * (attempt + 1)
                        log.info("model %s busy (%s); retrying in %.0fs", model, exc, wait)
                        time.sleep(wait)
                        continue
                    problems.append(f"{model}: {str(exc)[:160]}")
                    self._resting_until[model] = time.monotonic() + self.cooldown
                    log.warning(
                        "model %s still busy after %d attempts, resting it for %.0fs: %s",
                        model, attempt + 1, self.cooldown, str(exc)[:200],
                    )
                    break
                except OutOfTokens as exc:
                    count_model_request(model, "out_of_tokens")
                    if attempt < self.retries and budget < MAX_TOKENS_CAP:
                        budget = min(budget * 2, MAX_TOKENS_CAP)
                        log.info("model %s ran out of tokens before answering; retrying with %d", model, budget)
                        continue
                    problems.append(f"{model}: {str(exc)[:160]}")
                    log.warning("model %s failed: %s", model, str(exc)[:200])
                    break
                except Exception as exc:  # noqa: BLE001 - try the next model
                    count_model_request(model, "error")
                    problems.append(f"{model}: {str(exc)[:160]}")
                    log.warning("model %s failed: %s", model, str(exc)[:200])
                    break
                count_model_request(model, "ok")
                self.cache.put(key, model, text)
                return LLMResponse(text, model, False, round((time.perf_counter() - started) * 1000, 1))
        raise ModelUnavailable("all models failed: " + "; ".join(problems))

    def _call(self, model: str, system: str, user: str, max_tokens: int) -> str:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

        response = httpx.post(
            f"{API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-Title": "CloudServe Support Triage",
            },
            json={
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0,
                "seed": 7,
                "max_tokens": max_tokens,
                # Reasoning models spend tokens "thinking" before they answer; with too few
                # tokens the answer is empty. Keep thinking short and out of the reply.
                # Models without reasoning ignore this.
                "reasoning": {"effort": self.reasoning_effort, "exclude": True},
            },
            timeout=self.timeout,
        )
        if response.status_code == 429:
            body = response.text
            lowered = body.lower()
            if "per-day" in lowered or "per day" in lowered or "free-models-per-day" in lowered:
                self._daily_limit_hit = True
                raise ModelUnavailable("daily free-model limit reached")
            retry_after = response.headers.get("retry-after")
            raise RateLimited(
                f"429: {_error_message(body)}",
                float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None,
            )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {_error_message(response.text)}")
        data = response.json()
        if "error" in data:
            raise RuntimeError(_error_message(json.dumps(data)))
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            reason = choice.get("finish_reason") or "unknown"
            if reason == "length":
                raise OutOfTokens(f"empty response (finish_reason={reason}, max_tokens={max_tokens})")
            raise RuntimeError(f"empty response (finish_reason={reason})")
        return text


class RateLimited(Exception):
    """A temporary 429 from OpenRouter or the provider behind a free model."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OutOfTokens(Exception):
    """A reasoning model used its whole token allowance thinking and wrote no answer."""


def _error_message(body: str) -> str:
    """The readable part of an OpenRouter error body, without the noise."""
    try:
        error = json.loads(body).get("error", {})
        message = error.get("message", "")
        raw = (error.get("metadata") or {}).get("raw", "")
        provider = (error.get("metadata") or {}).get("provider_name", "")
        detail = " ".join(x for x in (message, f"[{provider}]" if provider else "", str(raw)[:120]) if x)
        return detail[:240] or body[:240]
    except (ValueError, AttributeError):
        return body[:240]


@lru_cache(maxsize=1)
def get_llm() -> OpenRouterClient:
    return OpenRouterClient()


def list_free_models() -> list[str]:
    response = httpx.get(f"{API_URL}/models", timeout=30)
    response.raise_for_status()
    return sorted(m["id"] for m in response.json().get("data", []) if m.get("id", "").endswith(":free"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m cloudserve_support.llm")
    parser.add_argument("--list-free", action="store_true", help="list the free models OpenRouter offers now")
    parser.add_argument("--ping", action="store_true", help="send one short test request with your key")
    args = parser.parse_args()
    if args.list_free:
        print("\n".join(list_free_models()))
    if args.ping:
        client = OpenRouterClient(cache=ResponseCache(os.getenv("LLM_CACHE_PATH", DEFAULT_CACHE)))
        # max_tokens is generous: some models spend tokens before answering, and too few gives an empty reply
        reply = client.complete("Reply with exactly the word: ok", f"ping {time.time()}", max_tokens=1000)
        print(f"model={reply.model} cached={reply.cached} latency_ms={reply.latency_ms} text={reply.text!r}")
