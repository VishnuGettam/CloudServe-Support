"""Grounded answer generation (FR-07), using prompt PR-02.

One model call per ticket routed to an automatic answer, and none for any
other ticket. To make the most of the free tier (50 calls a day) and keep
answers reproducible:

- the question sent is the ticket body only (the subject sometimes disagrees
  with the body, and identical bodies then share one cached answer);
- the customer's plan is applied here, in code: articles that don't apply to
  the customer's plan are removed before the prompt is built, so the plan is
  not part of the prompt;
- the top three remaining articles are sent whole, so the model sees the
  resolution steps and not just the section that matched.

The model's output is parsed strictly. If it is not the expected JSON, the
model is asked once to send the same answer again as valid JSON (one extra
call, cached like any other). If that also fails, it is reported as a format
failure and the validator escalates it. The repair request can't change what
the answer says: the reply still goes through every pre-send check.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from cloudserve_support.app.schemas import NormalisedTicket
from cloudserve_support.errors import ModelUnavailable, PipelineError
from cloudserve_support.llm import get_llm
from cloudserve_support.pipeline.retrieve import Passage, get_retriever

log = logging.getLogger(__name__)

DEFAULT_PROMPT = "prompts/pr02_generator_v1.md"
MAX_ARTICLES = 3
REPAIR_NOTE = (
    "\n\nYour previous reply could not be read as JSON ({error}). Reply again with only the JSON object "
    "described above: no text before or after it, double quotes escaped inside strings, no code fences."
)
PLAN_WORDS = {"standard": "standard", "business": "business", "enterprise": "enterprise"}


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    user: str


@dataclass
class Draft:
    answerable: bool
    answer: str
    citations: list[dict] = field(default_factory=list)
    missing_information: str | None = None
    articles: dict[str, dict] = field(default_factory=dict)  # doc_id -> article given to the model
    model: str = ""
    cached: bool = False
    prompt_version: str = ""
    format_error: str | None = None
    latency_ms: float = 0.0


@lru_cache(maxsize=4)
def load_prompt(path: str = DEFAULT_PROMPT) -> Prompt:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"cannot read prompt file {path}: {exc}") from exc
    version_match = re.search(r"version\s+([\d.]+)", text)
    try:
        system = text.split("### SYSTEM", 1)[1].split("### USER", 1)[0].strip()
        user = text.split("### USER", 1)[1].strip()
    except IndexError as exc:
        raise PipelineError(f"prompt file {path} needs '### SYSTEM' and '### USER' sections") from exc
    return Prompt(f"PR-02 v{version_match.group(1) if version_match else '?'}", system, user)


def applies_to_plan(applies_to: str, tier: str) -> bool:
    """True unless the article names plans and the customer's plan is not among them."""
    text = applies_to.lower()
    if "plan" not in text or "all plans" in text:
        return True  # product scope like "Container Service", or every plan
    return PLAN_WORDS.get(tier, tier) in text


def eligible_articles(passages: list[Passage], tier: str) -> dict[str, dict]:
    retriever = get_retriever()
    chosen: dict[str, dict] = {}
    for p in passages:
        article = retriever.article(p.doc_id)
        if article and applies_to_plan(article.get("applies_to", ""), tier):
            chosen[p.doc_id] = article
        if len(chosen) == MAX_ARTICLES:
            break
    return chosen


def render_articles(articles: dict[str, dict]) -> str:
    return "\n\n".join(
        f'<article doc_id="{doc_id}">\n{a["content"].strip()}\n</article>' for doc_id, a in articles.items()
    )


def parse(text: str) -> dict:
    """Extract the JSON object from the model's reply. Raises ValueError if there isn't a valid one."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("answerable"), bool):
        raise ValueError("'answerable' missing or not true/false")
    citations = data.get("citations") or []
    if not isinstance(citations, list) or not all(isinstance(c, dict) and "doc_id" in c for c in citations):
        raise ValueError("'citations' is not a list of {claim, doc_id}")
    return data


def generate(ticket: NormalisedTicket, passages: list[Passage]) -> Draft:
    """Write an answer from the articles, or say the articles don't answer it.

    Raises ModelUnavailable if no model could be reached (the caller escalates).
    """
    articles = eligible_articles(passages, ticket.customer_tier)
    if not articles:
        return Draft(False, "", missing_information="no retrieved article applies to the customer's plan")

    prompt = load_prompt(os.getenv("GENERATOR_PROMPT", DEFAULT_PROMPT))
    user = prompt.user.replace("{question}", ticket.body).replace("{articles}", render_articles(articles))
    reply = get_llm().complete(prompt.system, user)

    draft = Draft(False, "", articles=articles, model=reply.model, cached=reply.cached,
                  prompt_version=prompt.version, latency_ms=reply.latency_ms)
    try:
        data = parse(reply.text)
    except (ValueError, json.JSONDecodeError) as first_error:
        # One repair attempt: same prompt, plus a note asking for valid JSON only.
        log.info("ticket %s: model reply was not valid JSON (%s); asking once for a corrected reply",
                 ticket.ticket_id, str(first_error)[:120])
        try:
            reply = get_llm().complete(prompt.system, user + REPAIR_NOTE.format(error=str(first_error)[:120]))
            data = parse(reply.text)
            draft.model, draft.cached = reply.model, reply.cached
            draft.latency_ms += reply.latency_ms
        except ModelUnavailable:
            draft.format_error = str(first_error)[:200]
            return draft
        except (ValueError, json.JSONDecodeError) as exc:
            draft.format_error = f"{str(exc)[:160]} (after one repair attempt)"
            return draft

    draft.answerable = data["answerable"]
    draft.answer = (data.get("answer") or "").strip()
    draft.citations = [{"claim": str(c.get("claim", "")), "doc_id": str(c["doc_id"])} for c in data.get("citations") or []]
    draft.missing_information = data.get("missing_information")
    return draft