"""Prometheus metrics for live monitoring (Grafana dashboard in monitoring/).

Where the metrics come from:
- the API serves them at GET /metrics, for Prometheus to scrape;
- the harness writes them to <output>.prom after every run, and pushes them
  to a Prometheus Pushgateway when --pushgateway (or PUSHGATEWAY_URL) is set.

Metrics (all prefixed cloudserve_):
    tickets_total{route, reason_code, customer_tier, language_fluency}   counter
    ticket_latency_seconds{path}                                          histogram
    model_requests_total{model, outcome}                                  counter
    check_failures_total{check}                                           counter
    caveats_added_total                                                   counter
    decision_log_failures_total                                           counter
    kill_switch                                                           gauge (1 = on)
    orchestrator_info{engine}                                             gauge (1 for the engine in use)

The per-run metrics report (metrics.py) remains the evaluation record; these
are for watching a running system. prometheus-client is pure Python. If it
can't be imported, every function here does nothing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest, push_to_gateway,
        write_to_textfile,
    )

    PROMETHEUS_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    PROMETHEUS_AVAILABLE = False
    log.warning("prometheus-client unavailable (%s); live metrics are off", str(exc)[:200])

if PROMETHEUS_AVAILABLE:
    TICKETS = Counter(
        "cloudserve_tickets_total", "Tickets processed, by outcome and customer group",
        ["route", "reason_code", "customer_tier", "language_fluency"],
    )
    LATENCY = Histogram(
        "cloudserve_ticket_latency_seconds", "System processing time per ticket, by how the answer was produced",
        ["path"], buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 45, 60, 120),
    )
    MODEL_REQUESTS = Counter(
        "cloudserve_model_requests_total", "Requests to the language model, by result", ["model", "outcome"],
    )
    CHECK_FAILURES = Counter("cloudserve_check_failures_total", "Drafts stopped by a pre-send check", ["check"])
    CAVEATS = Counter("cloudserve_caveats_added_total", "Mandatory caveats added to automatic replies")
    LOG_FAILURES = Counter("cloudserve_decision_log_failures_total", "Decisions that could not be written to the log")
    KILL_SWITCH = Gauge("cloudserve_kill_switch", "1 when the kill switch is on")
    ORCHESTRATOR = Gauge("cloudserve_orchestrator_info", "Pipeline engine in use", ["engine"])


def _path(result: dict[str, Any]) -> str:
    from cloudserve_support.metrics import latency_path  # same paths as the metrics report

    return latency_path(result)


def observe_result(result: Any) -> None:
    """Record one ticket's outcome. Never raises."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        row = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        TICKETS.labels(
            route=row.get("route") or "unknown",
            reason_code=row.get("reason_code") or "unknown",
            customer_tier=row.get("customer_tier") or "unknown",
            language_fluency=row.get("language_fluency") or "unknown",
        ).inc()
        if row.get("latency_ms") is not None:
            LATENCY.labels(path=_path(row)).observe(row["latency_ms"] / 1000)
        if row.get("failed_check"):
            CHECK_FAILURES.labels(check=row["failed_check"]).inc()
        caveat_count = len(row.get("caveat_doc_ids") or [])
        if caveat_count:
            CAVEATS.inc(caveat_count)
        KILL_SWITCH.set(1 if row.get("reason_code") == "KILL_SWITCH" else 0)
    except Exception as exc:  # noqa: BLE001 - metrics must never affect a ticket
        log.debug("metrics update failed: %s", exc)


def count_model_request(model: str, outcome: str) -> None:
    """outcome: ok, cache_hit, rate_limited, out_of_tokens, daily_limit, no_key, error."""
    if PROMETHEUS_AVAILABLE:
        try:
            MODEL_REQUESTS.labels(model=model, outcome=outcome).inc()
        except Exception:  # noqa: BLE001
            pass


def count_log_failure() -> None:
    if PROMETHEUS_AVAILABLE:
        LOG_FAILURES.inc()


def set_orchestrator(engine: str) -> None:
    if PROMETHEUS_AVAILABLE:
        ORCHESTRATOR.labels(engine=engine).set(1)


def exposition() -> tuple[bytes, str]:
    """The current metrics in Prometheus text format, and its content type."""
    if not PROMETHEUS_AVAILABLE:
        return b"# prometheus-client not installed\n", "text/plain; charset=utf-8"
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def export_run(output: Path, job: str, pushgateway: str | None = None) -> Path | None:
    """After a harness run: write <output>.prom, and push to a Pushgateway if one is given. Never raises."""
    if not PROMETHEUS_AVAILABLE:
        return None
    prom_path = output.with_name(output.stem + ".prom")
    try:
        write_to_textfile(str(prom_path), REGISTRY)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not write %s: %s", prom_path, exc)
        prom_path = None
    url = pushgateway or os.getenv("PUSHGATEWAY_URL", "").strip()
    if url:
        try:
            push_to_gateway(url, job="cloudserve_harness", registry=REGISTRY, grouping_key={"input": job})
            log.info("metrics pushed to %s", url)
        except Exception as exc:  # noqa: BLE001 - monitoring must not fail a finished run
            log.warning("could not push metrics to %s: %s", url, str(exc)[:200])
    return prom_path
