"""The ticket pipeline as a LangGraph state graph.

    START ──(kill switch on)──────────────────────────────► kill_switch ─► END
      │
      ▼
    classify ─► policy ─► retrieve ─► route ──(not automatic)──► escalate ─► END
                                        │                            ▲
                                        ▼                            │
                                    generate ──(model down / not answerable)
                                        │                            │
                                        ▼                            │
                                    validate ──(a check failed)──────┘
                                        │
                                        ▼
                                     respond ─► END

Each node is a plain function that reads the shared state and returns the
fields it adds. Each decision between nodes is a plain function of the state.
Both are defined once, in NODES, EDGES and BRANCHES below, and used to build:

- the LangGraph `StateGraph` (the default engine), and
- a sequential runner that walks the same tables, used when LangGraph can't
  be imported (for example, a blocked DLL under Windows Application Control)
  or when ORCHESTRATOR=sequential.

The two engines therefore make the same decision for every ticket, which is
tested. All safety logic stays in the node functions, in code; the graph
only decides which node runs next.

Draw the graph:
    uv run python -m cloudserve_support.pipeline.orchestrator --mermaid
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, TypedDict

from cloudserve_support.app.schemas import NormalisedTicket, ReasonCode, TicketResult
from cloudserve_support.errors import ModelUnavailable
from cloudserve_support.pipeline.caveats import get_caveats
from cloudserve_support.pipeline.classify import Classification, get_classifier
from cloudserve_support.pipeline.escalate import build_packet
from cloudserve_support.pipeline.generate import Draft, generate
from cloudserve_support.pipeline.lc_retriever import search_passages
from cloudserve_support.pipeline.policy import PolicyDecision, get_policy_gate
from cloudserve_support.pipeline.retrieve import Passage
from cloudserve_support.pipeline.route import RouteDecision, decide, thresholds
from cloudserve_support.pipeline.validate import ValidationResult, validate

log = logging.getLogger(__name__)

AI_LABEL = (
    "This reply was drafted automatically by CloudServe's AI support assistant from our help articles. "
    "If it doesn't solve the problem, reply to this message and a member of the support team will pick it up."
)


class TicketState(TypedDict, total=False):
    """Everything the nodes know about one ticket, filled in as it moves through the graph."""

    ticket: NormalisedTicket
    classification: Classification
    policy: PolicyDecision
    passages: list[Passage]
    decision: RouteDecision
    draft: Draft
    check: ValidationResult
    escalation: dict[str, Any]  # why the ticket goes to a human: reason, reason_text, detail, ...
    result: TicketResult


def _now() -> datetime:
    return datetime.now(timezone.utc)


def kill_switch_on() -> bool:
    return os.getenv("KILL_SWITCH", "false").strip().lower() in {"1", "true", "yes", "on"}


# ---------- nodes: each returns only the fields it adds ----------

def classify_node(state: TicketState) -> dict[str, Any]:
    return {"classification": get_classifier().classify(state["ticket"].text)}


def policy_node(state: TicketState) -> dict[str, Any]:
    return {"policy": get_policy_gate().check(state["ticket"].text, state["classification"].intent)}


def retrieve_node(state: TicketState) -> dict[str, Any]:
    # Always runs, even for blocked tickets, so escalations carry sources.
    return {"passages": search_passages(state["ticket"].text)}


def route_node(state: TicketState) -> dict[str, Any]:
    decision = decide(state["classification"], state["policy"], state["passages"])
    update: dict[str, Any] = {"decision": decision}
    if decision.route != "auto_respond":
        update["escalation"] = {"reason": decision.reason_code, "reason_text": decision.reason_text}
    return update


def generate_node(state: TicketState) -> dict[str, Any]:
    try:
        draft = generate(state["ticket"], state["passages"])
    except ModelUnavailable as exc:
        return {"escalation": {"reason": ReasonCode.MODEL_UNAVAILABLE,
                               "reason_text": "Sent to a human: the answer-writing model was unavailable.",
                               "detail": exc.detail}}
    update: dict[str, Any] = {"draft": draft}
    if not draft.answerable and not draft.format_error:
        missing = draft.missing_information or "the articles found do not answer this question"
        update["escalation"] = {"reason": ReasonCode.WEAK_RETRIEVAL,
                                "reason_text": "Sent to a human: the help articles found do not answer this question.",
                                "detail": f"generator: {missing}"[:200]}
    return update


def validate_node(state: TicketState) -> dict[str, Any]:
    ticket = state["ticket"]
    check = validate(state["draft"], ticket.channel, ticket.customer_tier)
    update: dict[str, Any] = {"check": check}
    if not check.passed:
        update["escalation"] = {"reason": ReasonCode.VALIDATION_FAILED,
                                "reason_text": f"Sent to a human: the drafted answer failed the '{check.failed_check}' check.",
                                "failed_check": check.failed_check, "detail": check.detail, "checks": check.checks}
    return update


def format_reply(draft: Draft, caveats: list[tuple[str, str]] | None = None) -> str:
    """The customer-facing reply, assembled by code: answer, caveats, sources, AI label."""
    cited = list(dict.fromkeys(c["doc_id"] for c in draft.citations))
    sources = "; ".join(f"{d} ({draft.articles[d]['title']})" for d in cited)
    parts = [draft.answer]
    if caveats:
        parts.append("Important to know:\n" + "\n".join(f"- {text} ({doc_id})" for doc_id, text in caveats))
    parts += [f"Sources: {sources}", AI_LABEL]
    return "\n\n".join(parts)


def _common(state: TicketState) -> dict[str, Any]:
    c, policy, passages = state["classification"], state["policy"], state["passages"]
    return dict(
        ticket_id=state["ticket"].ticket_id,
        intent=c.intent,
        urgency=c.urgency,
        intent_confidence=c.intent_confidence,
        alternatives=[{"intent": i, "share": s} for i, s in c.alternatives],
        doc_ids=[p.doc_id for p in passages],
        retrieval_scores=[p.score for p in passages],
        policy_rule=policy.rule,
        sensitive=policy.sensitive,
        policy_version=policy.policy_version,
        received_at=state["ticket"].received_at,
    )


def respond_node(state: TicketState) -> dict[str, Any]:
    draft, check, decision = state["draft"], state["check"], state["decision"]
    cited = list(dict.fromkeys(c["doc_id"] for c in draft.citations))
    caveats = get_caveats().for_docs(cited)  # mandatory facts the answer must carry, added by code
    result = TicketResult(
        **_common(state), route="auto_respond", reason_code=ReasonCode.AUTO_RESPONDED,
        reason_text=f"{decision.reason_text} Passed all {len(check.checks)} pre-send checks"
                    + (f"; {len(caveats)} mandatory caveat(s) added." if caveats else "."),
        response=format_reply(draft, caveats), cited_doc_ids=cited, checks=[*check.checks, "caveats:pass"],
        caveat_doc_ids=list(dict.fromkeys(d for d, _ in caveats)),
        model=draft.model, cached=draft.cached, prompt_version=draft.prompt_version, responded_at=_now(),
    )
    return {"result": result}


def escalate_node(state: TicketState) -> dict[str, Any]:
    why, draft = state["escalation"], state.get("draft")
    packet = build_packet(state["ticket"], why["reason"], why["reason_text"], state["classification"],
                          state["policy"], state["passages"], thresholds(),
                          draft_answer=draft.answer if draft and draft.answer else None,
                          failed_check=why.get("failed_check"), detail=why.get("detail"))
    result = TicketResult(
        **_common(state), route="escalate", reason_code=why["reason"], reason_text=why["reason_text"],
        packet=packet, failed_check=why.get("failed_check"), detail=why.get("detail"),
        checks=why.get("checks") or [],
        model=draft.model if draft else None, prompt_version=draft.prompt_version if draft else None,
        cached=draft.cached if draft else None, responded_at=_now(),
    )
    return {"result": result}


def kill_switch_node(state: TicketState) -> dict[str, Any]:
    ticket = state["ticket"]
    return {"result": TicketResult(
        ticket_id=ticket.ticket_id, route="escalate", reason_code=ReasonCode.KILL_SWITCH,
        reason_text="Kill switch is on: every ticket is sent to a human.", detail="kill switch on",
        received_at=ticket.received_at, responded_at=_now(),
    )}


# ---------- branches: which node runs next ----------

def after_start(state: TicketState) -> str:
    return "kill_switch" if kill_switch_on() else "classify"


def after_route(state: TicketState) -> str:
    return "escalate" if "escalation" in state else "generate"


def after_generate(state: TicketState) -> str:
    return "escalate" if "escalation" in state else "validate"


def after_validate(state: TicketState) -> str:
    return "escalate" if "escalation" in state else "respond"


# ---------- the graph, defined once ----------

START, END = "__start__", "__end__"

NODES: dict[str, Callable[[TicketState], dict[str, Any]]] = {
    "kill_switch": kill_switch_node,
    "classify": classify_node,
    "policy": policy_node,
    "retrieve": retrieve_node,
    "route": route_node,
    "generate": generate_node,
    "validate": validate_node,
    "respond": respond_node,
    "escalate": escalate_node,
}
EDGES: dict[str, str] = {  # fixed next step
    "classify": "policy",
    "policy": "retrieve",
    "retrieve": "route",
    "respond": END,
    "escalate": END,
    "kill_switch": END,
}
BRANCHES: dict[str, tuple[Callable[[TicketState], str], list[str]]] = {  # next step decided by the state
    START: (after_start, ["kill_switch", "classify"]),
    "route": (after_route, ["generate", "escalate"]),
    "generate": (after_generate, ["validate", "escalate"]),
    "validate": (after_validate, ["respond", "escalate"]),
}


class SequentialGraph:
    """Walks NODES, EDGES and BRANCHES in order: the same graph without LangGraph."""

    engine = "sequential"

    def invoke(self, state: TicketState) -> TicketState:
        state = dict(state)  # type: ignore[assignment]
        step = BRANCHES[START][0](state)
        for _ in range(len(NODES) + 1):  # a ticket visits each node at most once
            state.update(NODES[step](state))
            if step in BRANCHES:
                step = BRANCHES[step][0](state)
            else:
                step = EDGES[step]
            if step == END:
                return state
        raise RuntimeError("pipeline graph did not reach END")


def build_langgraph():
    """Compile the LangGraph StateGraph from the same tables. Raises if LangGraph can't load."""
    from langgraph.graph import END as LG_END, START as LG_START, StateGraph

    def target(name: str) -> str:
        return LG_END if name == END else name

    graph = StateGraph(TicketState)
    for name, fn in NODES.items():
        graph.add_node(name, fn)
    for source, (choose, options) in BRANCHES.items():
        graph.add_conditional_edges(LG_START if source == START else source, choose,
                                    {option: target(option) for option in options})
    for source, dest in EDGES.items():
        graph.add_edge(source, target(dest))
    compiled = graph.compile()
    compiled.engine = "langgraph"  # type: ignore[attr-defined]
    return compiled


@lru_cache(maxsize=1)
def get_orchestrator():
    """The LangGraph graph, or the sequential runner if LangGraph is unavailable or ORCHESTRATOR=sequential."""
    wanted = os.getenv("ORCHESTRATOR", "langgraph").strip().lower()
    if wanted == "langgraph":
        try:
            graph = build_langgraph()
            log.info("orchestrator ready: LangGraph state graph, %d nodes", len(NODES))
            return graph
        except Exception as exc:  # noqa: BLE001 - ImportError or a blocked DLL; degrade, don't stop (A11)
            log.warning("LangGraph unavailable (%s); running the same graph sequentially", str(exc)[:200])
    log.info("orchestrator ready: sequential runner, %d nodes", len(NODES))
    return SequentialGraph()


def run_pipeline(ticket: NormalisedTicket) -> TicketResult:
    """Run one valid ticket through the graph and return its result."""
    final = get_orchestrator().invoke({"ticket": ticket})
    return final["result"]


def mermaid() -> str:
    """The graph as a Mermaid diagram, drawn from the same tables the engines use."""
    lines = ["flowchart TD", "    __start__([START])", "    __end__([END])"]
    for source, dest in EDGES.items():
        lines.append(f"    {source} --> {dest}")
    for source, (choose, options) in BRANCHES.items():
        for option in options:
            lines.append(f"    {source} -. {choose.__name__} .-> {option}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m cloudserve_support.pipeline.orchestrator")
    parser.add_argument("--mermaid", action="store_true", help="print the pipeline graph as Mermaid")
    parser.parse_args()
    graph = get_orchestrator()
    if getattr(graph, "engine", "") == "langgraph":
        print(graph.get_graph().draw_mermaid())  # drawn by LangGraph from the compiled graph
    else:
        print(mermaid())
