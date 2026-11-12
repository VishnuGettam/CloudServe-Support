"""Metrics report for a batch run (A10): Volume, Business, Technical, Governance.

The harness writes the report next to its output after every run:
    <output>.metrics.json   numbers, for tools
    <output>.metrics.md     the same numbers, readable

When the input tickets carry labels (the development and validation files
do; the hidden test file may not), accuracy and safety are measured against
them. Without labels those lines say "no labels" and everything else is
still reported.

Rebuild a report from an existing results file without re-running:
    uv run python -m cloudserve_support.metrics --results out/validation_results.json --tickets data/validation_tickets.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

# Baseline figures from the client brief, for comparison in the Business section.
BASELINE = {
    "weekly_tickets": 500,
    "first_reply_hours": "8-12",
    "first_contact_resolution": 0.42,
    "csat": 3.2,
}
AI_LABEL_MARKER = "drafted automatically by CloudServe's AI support assistant"
FAIRNESS_GAP_FLAG = 0.15  # flag a group whose automation rate differs from the overall rate by more than this
MIN_GROUP_SIZE = 5        # groups smaller than this are shown but not flagged


# ---------- helpers ----------

def _pct(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def _latency(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": round(max(values), 1) if values else None,
    }


def latency_path(r: dict[str, Any]) -> str:
    """How a ticket's answer was produced, which decides how long it took."""
    if r.get("reason_code") == "MODEL_UNAVAILABLE":
        return "model_failed"
    if r.get("model"):
        return "cached_model_answer" if r.get("cached") else "fresh_model_call"
    return "no_model_call"


# ---------- report ----------

def build_report(results: list[dict[str, Any]], tickets: list[dict[str, Any]] | None = None,
                 run_seconds: float | None = None) -> dict[str, Any]:
    total = len(results)
    auto = [r for r in results if r.get("route") == "auto_respond"]
    escalated = [r for r in results if r.get("route") == "escalate"]
    labels = {t.get("ticket_id"): t["labels"] for t in (tickets or [])
              if isinstance(t, dict) and isinstance(t.get("labels"), dict)}
    history = [t["history"] for t in (tickets or []) if isinstance(t, dict) and isinstance(t.get("history"), dict)]
    labelled = [r for r in results if r.get("ticket_id") in labels]

    report: dict[str, Any] = {
        "tickets": total,
        "labelled_tickets": len(labelled),
        "run_seconds": round(run_seconds, 1) if run_seconds is not None else None,
    }

    # ----- Volume -----
    report["volume"] = {
        "routes": dict(Counter(r.get("route") for r in results)),
        "reason_codes": dict(Counter(r.get("reason_code") for r in results).most_common()),
        "intents": dict(Counter(r.get("intent") or "unknown" for r in results).most_common()),
        "urgency": dict(Counter(r.get("urgency") or "unknown" for r in results).most_common()),
        "channels": dict(Counter(r.get("channel") or "unknown" for r in results).most_common()),
    }

    # ----- Business -----
    automation = _pct(len(auto), total)
    business: dict[str, Any] = {
        "automation_rate": automation,
        "escalation_rate": _pct(len(escalated), total),
        "tickets_off_human_queue_per_week": round(automation * BASELINE["weekly_tickets"]) if automation is not None else None,
        "auto_first_reply_p95_seconds": (
            round(_percentile([r["latency_ms"] for r in auto if r.get("latency_ms") is not None], 0.95) / 1000, 1)
            if auto else None),
        "escalations_with_packet": _pct(sum(1 for r in escalated if r.get("packet")), len(escalated)),
        "baseline": BASELINE,
    }
    if history:
        fcr = [h["first_contact_resolution"] for h in history if isinstance(h.get("first_contact_resolution"), bool)]
        minutes = [h["resolution_time_minutes"] for h in history if isinstance(h.get("resolution_time_minutes"), (int, float))]
        business["historical_in_this_file"] = {
            "first_contact_resolution": _pct(sum(fcr), len(fcr)),
            "median_resolution_minutes": median(minutes) if minutes else None,
        }
    if labelled:
        # An automatic answer that cites an article the label expects, on a ticket the docs can answer,
        # is the best available stand-in for "resolved on first contact".
        good = [r for r in auto if r["ticket_id"] in labels and _answer_is_supported(r, labels[r["ticket_id"]])]
        business["auto_answers_supported_by_labels"] = _pct(len(good), len([r for r in auto if r["ticket_id"] in labels]))
        business["estimated_fcr_from_automation_alone"] = _pct(len(good), len(labelled))
    report["business"] = business

    # ----- Technical -----
    by_path: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.get("latency_ms") is not None:
            by_path[latency_path(r)].append(r["latency_ms"])
    technical: dict[str, Any] = {
        "latency_all": _latency([v for vs in by_path.values() for v in vs]),
        "latency_by_path": {k: _latency(v) for k, v in sorted(by_path.items())},
        "latency_target": "p95 under 3000 ms (NFR); fresh free-model calls are reported separately",
        "models_used": dict(Counter(r.get("model") for r in results if r.get("model"))),
        "prompt_versions": sorted({r["prompt_version"] for r in results if r.get("prompt_version")}),
        "validation_failures_by_check": dict(Counter(r.get("failed_check") for r in results if r.get("failed_check"))),
    }
    if labelled:
        technical["intent_accuracy"] = _pct(sum(r.get("intent") == labels[r["ticket_id"]].get("intent") for r in labelled), len(labelled))
        technical["urgency_agreement"] = _pct(sum(r.get("urgency") == labels[r["ticket_id"]].get("urgency") for r in labelled), len(labelled))
        technical["route_agreement"] = _pct(sum(r.get("route") == labels[r["ticket_id"]].get("expected_route") for r in labelled), len(labelled))
        with_docs = [r for r in labelled if labels[r["ticket_id"]].get("expected_doc_ids")]
        technical["retrieval_hit_rate"] = _pct(
            sum(bool(set(r.get("doc_ids") or []) & set(labels[r["ticket_id"]]["expected_doc_ids"])) for r in with_docs),
            len(with_docs))
        technical["retrieval_top1_hit_rate"] = _pct(
            sum(bool(r.get("doc_ids")) and r["doc_ids"][0] in labels[r["ticket_id"]]["expected_doc_ids"] for r in with_docs),
            len(with_docs))
        cited = [r for r in auto if r["ticket_id"] in labels and labels[r["ticket_id"]].get("expected_doc_ids")]
        technical["citation_accuracy"] = _pct(
            sum(bool(set(r.get("cited_doc_ids") or []) & set(labels[r["ticket_id"]]["expected_doc_ids"])) for r in cited),
            len(cited))
    else:
        technical["accuracy"] = "no labels in the input file"
    report["technical"] = technical

    # ----- Governance -----
    governance: dict[str, Any] = {
        "auto_replies_with_ai_label": _pct(sum(AI_LABEL_MARKER in (r.get("response") or "") for r in auto), len(auto)),
        "auto_replies_with_citation": _pct(sum(bool(r.get("cited_doc_ids")) for r in auto), len(auto)),
        "auto_replies_passing_all_checks": _pct(
            sum(bool(r.get("checks")) and all(c.endswith(":pass") for c in r["checks"]) for r in auto), len(auto)),
        "policy_blocked": sum(r.get("reason_code") in {"POLICY_BLOCKED", "KEYWORD_BLOCKED"} for r in results),
        "sensitive_held": sum(r.get("reason_code") == "SENSITIVE_CLASS" for r in results),
        "kill_switch_escalations": sum(r.get("reason_code") == "KILL_SWITCH" for r in results),
        "errors": {c: n for c, n in Counter(r.get("reason_code") for r in results).items()
                   if c in {"PIPELINE_ERROR", "INVALID_INPUT", "MODEL_UNAVAILABLE"}},
        "policy_versions": sorted({r["policy_version"] for r in results if r.get("policy_version")}),
    }
    if labelled:
        never = [r for r in labelled if labels[r["ticket_id"]].get("must_not_auto_respond")]
        governance["must_not_auto_tickets"] = len(never)
        governance["must_not_auto_answered_automatically"] = [r["ticket_id"] for r in never if r.get("route") == "auto_respond"]
        governance["answered_although_docs_cannot_answer"] = [
            r["ticket_id"] for r in auto
            if r["ticket_id"] in labels and labels[r["ticket_id"]].get("answerable_from_docs") is False]
    governance["fairness"] = fairness(results, labels)
    report["governance"] = governance
    return report


def _answer_is_supported(r: dict[str, Any], label: dict[str, Any]) -> bool:
    return bool(label.get("answerable_from_docs")) and bool(
        set(r.get("cited_doc_ids") or []) & set(label.get("expected_doc_ids") or []))


def fairness(results: list[dict[str, Any]], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Automation rate (and accuracy, with labels) for each customer group.

    A different automation rate is not unfair by itself: groups send
    different kinds of tickets. So, when labels exist, each group is compared
    with its OWN expected automation rate (how many of its tickets the labels
    say should be answered automatically). A flag then means the system
    treats that group differently from what its tickets call for. Without
    labels, groups are compared with the overall rate, and a flag means
    "look at why", starting with the group's ticket mix.
    """
    overall = _pct(sum(r.get("route") == "auto_respond" for r in results), len(results)) or 0.0
    audit: dict[str, Any] = {"overall_automation_rate": overall, "flag_if_gap_over": FAIRNESS_GAP_FLAG, "groups": {}, "flags": []}
    for attribute in ("customer_tier", "customer_region", "language_fluency", "channel"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in results:
            groups[r.get(attribute) or "unknown"].append(r)
        rows = {}
        for name, members in sorted(groups.items()):
            rate = _pct(sum(r.get("route") == "auto_respond" for r in members), len(members))
            row: dict[str, Any] = {"tickets": len(members), "automation_rate": rate}
            marked = [r for r in members if r.get("ticket_id") in labels]
            if marked:
                row["intent_accuracy"] = _pct(sum(r.get("intent") == labels[r["ticket_id"]].get("intent") for r in marked), len(marked))
                expected_auto = sum(labels[r["ticket_id"]].get("expected_route") == "auto_respond" for r in marked)
                row["expected_automation_rate"] = _pct(expected_auto, len(marked))
            expected = row.get("expected_automation_rate")
            if rate is not None and expected is not None:
                row["gap_vs_expected"] = round(rate - expected, 4)
            rows[name] = row
            if rate is None or len(members) < MIN_GROUP_SIZE:
                continue
            if expected is not None:
                if abs(rate - expected) > FAIRNESS_GAP_FLAG:
                    audit["flags"].append(f"{attribute}={name}: automation {rate:.0%} vs {expected:.0%} expected "
                                          f"from its labels ({len(members)} tickets)")
            elif abs(rate - overall) > FAIRNESS_GAP_FLAG:
                audit["flags"].append(f"{attribute}={name}: automation {rate:.0%} vs {overall:.0%} overall ({len(members)} tickets)")
        audit["groups"][attribute] = rows
    return audit


# ---------- readable version ----------

_NOT_A_SHARE = ("_ms", "_seconds", "_minutes", "count", "tickets", "csat", "per_week", "gap")


def _fmt(value: Any, key: str = "") -> str:
    """Shares (0-1) as percentages; times, counts and scores as plain numbers."""
    if value is None:
        return "–"
    if isinstance(value, float) and 0 <= value <= 1 and not key.endswith(_NOT_A_SHARE):
        return f"{value:.1%}"
    if isinstance(value, list):
        return ", ".join(map(str, value)) if value else "none"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_fmt(v, k)}" for k, v in value.items()) if value else "none"
    return str(value)


def to_markdown(report: dict[str, Any], title: str = "Run metrics") -> str:
    out = [f"# {title}", "",
           f"Tickets: **{report['tickets']}** (labelled: {report['labelled_tickets']})"
           + (f" · run time {report['run_seconds']} s" if report.get("run_seconds") is not None else ""), ""]

    def table(heading: str, rows: dict[str, Any], skip: tuple[str, ...] = ()) -> None:
        out.extend([f"## {heading}", "", "| Measure | Value |", "|---|---|"])
        for key, value in rows.items():
            if key in skip:
                continue
            out.append(f"| {key.replace('_', ' ')} | {_fmt(value, key)} |")
        out.append("")

    table("Volume", report["volume"])
    table("Business", report["business"], skip=("baseline",))
    out.append("Baseline from the brief: " + _fmt(report["business"]["baseline"]))
    out.append("")
    tech = dict(report["technical"])
    paths = tech.pop("latency_by_path")
    table("Technical", tech)
    out.extend(["### Latency by path", "", "| Path | Tickets | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|"])
    for name, s in paths.items():
        out.append(f"| {name.replace('_', ' ')} | {s['count']} | {_fmt(s['p50_ms'], 'p50_ms')} | {_fmt(s['p95_ms'], 'p95_ms')} | {_fmt(s['max_ms'], 'max_ms')} |")
    out.append("")
    gov = dict(report["governance"])
    fair = gov.pop("fairness")
    table("Governance", gov)
    out.extend(["### Fairness audit", "",
                f"Overall automation rate {_fmt(fair['overall_automation_rate'])}. A group (of {MIN_GROUP_SIZE}+ tickets) is "
                f"flagged when its automation rate differs by more than {fair['flag_if_gap_over']:.0%} from the rate its own "
                "labels call for (or from the overall rate, when there are no labels).", ""])
    for attribute, rows in fair["groups"].items():
        out.extend([f"**{attribute.replace('_', ' ')}**", "",
                    "| Group | Tickets | Automation | Expected automation | Gap | Intent accuracy |", "|---|---|---|---|---|---|"])
        for name, row in rows.items():
            gap = row.get("gap_vs_expected")
            out.append(f"| {name} | {row['tickets']} | {_fmt(row['automation_rate'])} | "
                       f"{_fmt(row.get('expected_automation_rate'))} | {'–' if gap is None else f'{gap:+.1%}'} | "
                       f"{_fmt(row.get('intent_accuracy'))} |")
        out.append("")
    out.append("Flags: " + ("; ".join(fair["flags"]) if fair["flags"] else "none"))
    out.append("")
    return "\n".join(out)


def write_report(output: Path, results: list[dict[str, Any]], tickets: list[Any] | None,
                 run_seconds: float | None = None) -> tuple[Path, Path]:
    report = build_report(results, [t for t in (tickets or []) if isinstance(t, dict)], run_seconds)
    json_path = output.with_name(output.stem + ".metrics.json")
    md_path = output.with_name(output.stem + ".metrics.md")
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report, f"Run metrics: {output.name}"), encoding="utf-8")
    return json_path, md_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m cloudserve_support.metrics")
    parser.add_argument("--results", required=True, type=Path, help="results file written by the harness")
    parser.add_argument("--tickets", type=Path, help="the input tickets (labels, if present, enable accuracy)")
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    tickets = json.loads(args.tickets.read_text(encoding="utf-8-sig")) if args.tickets else None
    if isinstance(tickets, dict):
        tickets = tickets.get("tickets")
    paths = write_report(args.results, results, tickets)
    print(f"wrote {paths[0]} and {paths[1]}")