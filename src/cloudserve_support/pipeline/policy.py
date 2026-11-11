"""Policy gate: tickets that must never be answered automatically (FR-04).

Pure code, no model. Three independent rules, checked in this order; any one
of them blocks the ticket:

1. Intent rule: the classifier's intent is one of the blocked intents.
2. Keyword rule: the ticket text matches the keyword safety net. This does
   not depend on the classifier, so a security incident that was classified
   as an "API key issue" is still caught.
3. Unclear rule: the ticket is short and names no product, feature or error.

The gate also flags sensitive tickets (data residency, billing disputes),
which are not blocked but must clear a stricter threshold at routing.

Every decision records which rules were checked and what matched, whether
or not it blocked, so the decision log can explain it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from cloudserve_support.app.schemas import ReasonCode
from cloudserve_support.errors import PipelineError

log = logging.getLogger(__name__)

DEFAULT_CONFIG = "config/never_auto.yaml"


@dataclass(frozen=True)
class PolicyDecision:
    blocked: bool
    reason_code: ReasonCode | None  # POLICY_BLOCKED or KEYWORD_BLOCKED when blocked
    rule: str | None  # "intent", "keyword:<group>", "unclear"
    matched: str | None  # the intent or the text fragment that triggered it
    sensitive: str | None  # "data_residency" or "billing_dispute" when flagged
    checks: list[str] = field(default_factory=list)  # everything checked, for the log
    policy_version: str = ""

    @property
    def reason_text(self) -> str:
        """Plain-language reason a support manager can read."""
        if not self.blocked:
            return "Allowed by policy." + (f" Flagged as {self.sensitive.replace('_', ' ')}: stricter threshold applies." if self.sensitive else "")
        if self.rule == "intent":
            return f"Never answered automatically: classified as {self.matched.replace('_', ' ')}."
        if self.rule and self.rule.startswith("keyword:"):
            return f"Sent to a human: the ticket mentions '{self.matched}', which suggests a {self.rule.split(':')[1]} issue."
        return "Sent to a human: the ticket does not say which product or error it is about."


class PolicyGate:
    def __init__(self, config: dict) -> None:
        try:
            self.version = str(config.get("version", "unversioned"))
            self.blocked_intents = frozenset(config["blocked_intents"])
            self.keyword_groups = {
                name: [re.compile(p, re.IGNORECASE) for p in patterns]
                for name, patterns in config["keyword_groups"].items()
            }
            self.sensitive_intents = frozenset(config.get("sensitive_intents", []))
            self.billing_dispute = [re.compile(p, re.IGNORECASE) for p in config.get("billing_dispute_patterns", [])]
            self.unclear_max_words = int(config.get("unclear_max_words", 20))
            self.product_terms = [re.compile(p if p.startswith("\\") else re.escape(p), re.IGNORECASE)
                                  for p in config.get("product_terms", [])]
        except (KeyError, TypeError, re.error) as exc:
            raise PipelineError(f"invalid policy configuration: {exc}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyGate":
        try:
            config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PipelineError(f"cannot load policy configuration {path}: {exc}") from exc
        if not isinstance(config, dict):
            raise PipelineError(f"policy configuration {path} is empty or not a mapping")
        return cls(config)

    def names_product_or_error(self, text: str) -> bool:
        return any(p.search(text) for p in self.product_terms)

    def check(self, text: str, intent: str | None) -> PolicyDecision:
        checks: list[str] = []

        # 1. intent rule
        checks.append(f"intent:{intent}")
        if intent in self.blocked_intents:
            return PolicyDecision(True, ReasonCode.POLICY_BLOCKED, "intent", intent, None, checks, self.version)

        # 2. keyword safety net
        for group, patterns in self.keyword_groups.items():
            checks.append(f"keyword:{group}")
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return PolicyDecision(
                        True, ReasonCode.KEYWORD_BLOCKED, f"keyword:{group}", match.group(0).lower(), None, checks, self.version
                    )

        # 3. unclear rule
        words = len(text.split())
        checks.append(f"unclear:words={words}")
        if words <= self.unclear_max_words and not self.names_product_or_error(text):
            return PolicyDecision(True, ReasonCode.POLICY_BLOCKED, "unclear", f"{words} words, no product named", None, checks, self.version)

        # sensitive classes: allowed, but flagged for the stricter threshold
        sensitive = None
        if intent in self.sensitive_intents:
            sensitive = intent
        elif intent in {"billing_query", "quota_or_overage"} and any(p.search(text) for p in self.billing_dispute):
            sensitive = "billing_dispute"
        checks.append(f"sensitive:{sensitive}")
        return PolicyDecision(False, None, None, None, sensitive, checks, self.version)


@lru_cache(maxsize=1)
def get_policy_gate() -> PolicyGate:
    gate = PolicyGate.from_file(os.getenv("POLICY_CONFIG", DEFAULT_CONFIG))
    log.info("policy gate ready: version %s, %d blocked intents", gate.version, len(gate.blocked_intents))
    return gate