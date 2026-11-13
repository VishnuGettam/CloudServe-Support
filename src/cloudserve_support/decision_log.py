"""Decision log: one permanent row per ticket decision (FR-10).

Every result from the API or the harness is written here, so any decision can
be traced later: what the system decided, why, from which articles, with
which model, prompt and policy version, and how long it took.

- Insert-only. The application never updates or deletes rows, and database
  triggers refuse UPDATE and DELETE, so the log can't be quietly edited.
- SQLite, through Python's built-in sqlite3 module (no extra package, nothing
  for Windows Application Control to block). DATABASE_URL takes the form
  sqlite:///./storage/decisions.db. Moving to PostgreSQL for production means
  the same table with a database user that has INSERT and SELECT only; the
  write path is one function (record) to point at it.
- No customer names or ids are stored: only the ticket id, a hash of the
  cleaned input, and the decision itself.
- Writing the log never stops a ticket. If the database can't be written, the
  failure is logged and the result is still returned (A11).
- Set DATABASE_URL to an empty value to turn the log off.

Look at the log:
    uv run python -m cloudserve_support.decision_log --summary
    uv run python -m cloudserve_support.decision_log --ticket VAL-0004
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_URL = "sqlite:///./storage/decisions.db"

COLUMNS = [
    "logged_at", "source", "ticket_id", "input_hash", "route", "reason_code", "reason_text",
    "intent", "intent_confidence", "urgency", "policy_rule", "policy_version", "sensitive",
    "doc_ids", "cited_doc_ids", "failed_check", "model", "cached", "prompt_version",
    "latency_ms", "received_at", "responded_at", "result",
]
JSON_COLUMNS = {"doc_ids", "cited_doc_ids", "result"}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS decisions (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        logged_at         TEXT NOT NULL,
        source            TEXT NOT NULL,      -- "api" or the harness run id
        ticket_id         TEXT NOT NULL,
        input_hash        TEXT,
        route             TEXT NOT NULL,
        reason_code       TEXT NOT NULL,
        reason_text       TEXT,
        intent            TEXT,
        intent_confidence REAL,
        urgency           TEXT,
        policy_rule       TEXT,
        policy_version    TEXT,
        sensitive         TEXT,
        doc_ids           TEXT,               -- JSON list
        cited_doc_ids     TEXT,               -- JSON list
        failed_check      TEXT,
        model             TEXT,
        cached            INTEGER,
        prompt_version    TEXT,
        latency_ms        REAL,
        received_at       TEXT,
        responded_at      TEXT,
        result            TEXT NOT NULL       -- the full result, exactly as returned (JSON)
    )""",
    "CREATE INDEX IF NOT EXISTS decisions_ticket ON decisions (ticket_id)",
    "CREATE INDEX IF NOT EXISTS decisions_reason ON decisions (reason_code)",
    # The database itself refuses edits and deletes.
    "CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions "
    "BEGIN SELECT RAISE(ABORT, 'the decision log is insert-only'); END",
    "CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions "
    "BEGIN SELECT RAISE(ABORT, 'the decision log is insert-only'); END",
]


def sqlite_path(url: str) -> str:
    """sqlite:///./storage/decisions.db -> ./storage/decisions.db (also sqlite:////abs/path, sqlite:///C:/path)."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"DATABASE_URL must start with {prefix} (got {url.split(':', 1)[0]}:...)")
    return url[len(prefix):] or ":memory:"


class DecisionLog:
    def __init__(self, url: str) -> None:
        self.url = url
        self.path = sqlite_path(url)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")  # the API and the harness can write at the same time
            for statement in SCHEMA:
                db.execute(statement)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, result: Any, source: str) -> None:
        row = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        values = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "source": source[:64],
            "ticket_id": str(row.get("ticket_id"))[:64],
            "input_hash": row.get("input_hash"),
            "route": row["route"],
            "reason_code": row["reason_code"],
            "reason_text": row.get("reason_text"),
            "intent": row.get("intent"),
            "intent_confidence": row.get("intent_confidence"),
            "urgency": row.get("urgency"),
            "policy_rule": row.get("policy_rule"),
            "policy_version": row.get("policy_version"),
            "sensitive": row.get("sensitive"),
            "doc_ids": json.dumps(row.get("doc_ids") or []),
            "cited_doc_ids": json.dumps(row.get("cited_doc_ids") or []),
            "failed_check": row.get("failed_check"),
            "model": row.get("model"),
            "cached": None if row.get("cached") is None else int(bool(row["cached"])),
            "prompt_version": row.get("prompt_version"),
            "latency_ms": row.get("latency_ms"),
            "received_at": row.get("received_at"),
            "responded_at": row.get("responded_at"),
            "result": json.dumps(row, ensure_ascii=False),
        }
        sql = f"INSERT INTO decisions ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})"
        with self._lock, self.connect() as db:
            db.execute(sql, [values[c] for c in COLUMNS])

    def for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM decisions WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            for column in JSON_COLUMNS:
                if item.get(column):
                    item[column] = json.loads(item[column])
            if item.get("cached") is not None:
                item["cached"] = bool(item["cached"])
            out.append(item)
        return out

    def summary(self) -> dict[str, Any]:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            by_reason = db.execute("SELECT reason_code, COUNT(*) FROM decisions GROUP BY reason_code").fetchall()
            by_source = db.execute(
                "SELECT source, COUNT(*), MAX(logged_at) FROM decisions GROUP BY source ORDER BY MAX(logged_at)"
            ).fetchall()
        return {
            "total": total,
            "reason_codes": {r: n for r, n in by_reason},
            "sources": {s: {"rows": n, "last": last} for s, n, last in by_source},
        }


_log_instance: DecisionLog | None = None
_log_url: str | None = None
_init_lock = threading.Lock()


def get_decision_log() -> DecisionLog | None:
    """The decision log for the current DATABASE_URL, or None when the log is turned off."""
    global _log_instance, _log_url
    url = os.getenv("DATABASE_URL", DEFAULT_URL).strip()
    if not url:
        return None
    with _init_lock:
        if _log_instance is None or _log_url != url:
            _log_instance, _log_url = DecisionLog(url), url
        return _log_instance


def record_decision(result: Any, source: str = "api") -> bool:
    """Write one decision. Returns False (and logs why) instead of raising."""
    try:
        decision_log = get_decision_log()
        if decision_log is None:
            return False
        decision_log.record(result, source)
        return True
    except Exception as exc:  # noqa: BLE001 - the ticket's result matters more than its log row
        from cloudserve_support.observability import count_log_failure

        count_log_failure()
        log.error("decision log write failed for ticket %s: %s",
                  getattr(result, "ticket_id", "?"), str(exc)[:200])
        return False


def reset() -> None:
    """Forget the open log (tests use this after changing DATABASE_URL)."""
    global _log_instance, _log_url
    with _init_lock:
        _log_instance, _log_url = None, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m cloudserve_support.decision_log")
    parser.add_argument("--summary", action="store_true", help="row counts by reason code and run")
    parser.add_argument("--ticket", help="every logged decision for one ticket id")
    args = parser.parse_args()
    decision_log = get_decision_log()
    if decision_log is None:
        raise SystemExit("DATABASE_URL is empty: the decision log is turned off")
    if args.summary or not args.ticket:
        print(json.dumps(decision_log.summary(), indent=2))
    if args.ticket:
        for row in decision_log.for_ticket(args.ticket):
            row.pop("result", None)
            print(json.dumps(row, indent=2, default=str))
