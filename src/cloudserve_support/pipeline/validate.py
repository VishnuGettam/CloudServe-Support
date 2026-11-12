"""Pre-send checks on every drafted answer (FR-08, A6).

All checks are code, run in a fixed order. The first failure stops the reply
from being sent; the ticket escalates with VALIDATION_FAILED and the name of
the failed check. Every check that ran is recorded, whether it passed or not.

1. format      the model returned the expected JSON, with an answer and citations
2. citations   every cited article is one of the articles the model was given
3. grounding   every cited claim shares most of its content words with its article
4. private     no email addresses, phone numbers, keys, tokens or customer IDs
5. claims      no promised refunds, fixes or dates; never asks for credentials
6. plan        no cited article is for a plan the customer is not on
7. length      within the channel's word limit
8. tone        no blaming the customer

The grounding check is a word-overlap test, not a second model call: it is
deterministic and costs nothing against the free-tier limit. The model-based
claim checker (prompt PR-04) is kept for evaluation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cloudserve_support.pipeline.generate import Draft, applies_to_plan

WORD_LIMITS = {"chat": 120, "docs_comment": 150, "forum": 150, "email": 250}
GROUNDING_MIN = 0.5

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be been but by can could do does for from has have how i if in into is it its "
    "may might must my no not of on or our should so than that the their them then there these this "
    "those to use using was we were what when where which while who will with would you your".split()
)

PRIVATE_DATA = {
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone number": re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)"),
    "API key or token": re.compile(r"\b(sk|pk|rk|ghp|xox[abp])[-_][A-Za-z0-9_-]{10,}|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9]{40,}\b"),
    "customer ID": re.compile(r"\bCUST-\d+\b", re.IGNORECASE),
    "masked name placeholder": re.compile(r"\[CUSTOMER\]"),
}

BANNED_CLAIMS = {
    "refund or credit issued": re.compile(
        r"\b(refund|credit)s?\b[^.]{0,40}\b(has|have|was|were|been)\b[^.]{0,20}\b(issued|processed|applied|approved|granted)\b"
        r"|\bwe(\s+have|'ve)?\s+(issued|processed|applied|approved)\b[^.]{0,30}\b(refund|credit)", re.IGNORECASE),
    "fixed on our side": re.compile(
        r"\b(has|have|was|were|been)\s+(now\s+)?(fixed|resolved|patched)\s+(on our (side|end)|by (us|our team))"
        r"|\bwe(\s+have|'ve)\s+(fixed|resolved|patched)\b", re.IGNORECASE),
    "date for a fix": re.compile(
        r"\b(fix|patch|resolution|release)\b[^.]{0,40}\b(by|on|within|before)\s+(monday|tuesday|wednesday|thursday|friday"
        r"|saturday|sunday|tomorrow|next week|end of (the )?(day|week|month)|\d{1,2}(st|nd|rd|th)?\s+[a-z]{3,9}|[a-z]{3,9}\s+\d{1,2})",
        re.IGNORECASE),
    "asks for credentials": re.compile(
        r"\b(send|share|provide|give|email|paste)\b[^.]{0,30}\b(your\s+)?(password|api key|secret|token|credentials?)\b",
        re.IGNORECASE),
}

BLAME = re.compile(
    r"\byou should have\b|\byour (own )?fault\b|\byou failed to\b|\bas (we|i) (already )?(told|said)\b|\bclearly you\b",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    passed: bool
    failed_check: str | None = None
    detail: str | None = None
    checks: list[str] = field(default_factory=list)  # every check that ran, with its result


def content_words(text: str) -> set[str]:
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words}  # light stemming


def grounding_score(claim: str, article_text: str) -> float:
    claim_words = content_words(claim)
    if not claim_words:
        return 1.0
    return len(claim_words & content_words(article_text)) / len(claim_words)


def validate(draft: Draft, channel: str, tier: str) -> ValidationResult:
    result = ValidationResult(passed=True)

    def fail(check: str, detail: str) -> ValidationResult:
        result.passed, result.failed_check, result.detail = False, check, detail[:200]
        result.checks.append(f"{check}:fail")
        return result

    # 1. format
    if draft.format_error:
        return fail("format", f"model output not in the expected format: {draft.format_error}")
    if not draft.answer or not draft.citations:
        return fail("format", "answer or citations missing")
    result.checks.append("format:pass")

    # 2. citations
    unknown = sorted({c["doc_id"] for c in draft.citations} - set(draft.articles))
    if unknown:
        return fail("citations", f"cites articles it was not given: {', '.join(unknown)}")
    result.checks.append("citations:pass")

    # 3. grounding
    for c in draft.citations:
        score = grounding_score(c["claim"], draft.articles[c["doc_id"]]["content"])
        if score < GROUNDING_MIN:
            return fail("grounding", f"claim not supported by {c['doc_id']} (overlap {score:.2f})")
    result.checks.append("grounding:pass")

    # 4. private data
    for name, pattern in PRIVATE_DATA.items():
        if pattern.search(draft.answer):
            return fail("private", f"reply contains a {name}")
    result.checks.append("private:pass")

    # 5. banned claims
    for name, pattern in BANNED_CLAIMS.items():
        if pattern.search(draft.answer):
            return fail("claims", f"reply makes a banned claim: {name}")
    result.checks.append("claims:pass")

    # 6. plan
    for doc_id in {c["doc_id"] for c in draft.citations}:
        applies = draft.articles[doc_id].get("applies_to", "")
        if not applies_to_plan(applies, tier):
            return fail("plan", f"{doc_id} applies to '{applies}', not the {tier} plan")
    result.checks.append("plan:pass")

    # 7. length
    words, limit = len(draft.answer.split()), WORD_LIMITS.get(channel, 120)
    if words > limit:
        return fail("length", f"{words} words, limit for {channel} is {limit}")
    result.checks.append("length:pass")

    # 8. tone
    match = BLAME.search(draft.answer)
    if match:
        return fail("tone", f"blaming phrase: '{match.group(0)}'")
    result.checks.append("tone:pass")

    return result