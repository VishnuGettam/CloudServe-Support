"""Batch harness: process a whole ticket file unattended (FR-02).

Usage:
    uv run python -m cloudserve_support.harness --input data/validation_tickets.json --output out/results.json

Guarantees:
- Paths come only from the command line; nothing is hardcoded.
- Exactly one result per input ticket, in input order.
- One failing ticket never stops the run (handle() never raises).
- Progress is saved after every ticket, so an interrupted run continues with
  --resume. Progress from a different input file is never reused.
- The output file is written atomically: it is either complete or absent.

Exit codes:
    0    finished; every ticket has a result
    2    bad arguments or unreadable input file
    3    output could not be written (progress is kept for --resume)
    130  interrupted (Ctrl+C); rerun with --resume
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from cloudserve_support.logging_config import setup_logging
from cloudserve_support.pipeline.graph import handle, warm_up

log = logging.getLogger("cloudserve_support.harness")

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_WRITE_FAILED = 3
EXIT_INTERRUPTED = 130


class HarnessError(Exception):
    """A problem with the run as a whole (not with one ticket)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------- input ----------

def load_tickets(path: Path) -> tuple[list[Any], str]:
    """Read the input file. Returns the tickets and a fingerprint of the file.

    Accepts a JSON list, or an object with a "tickets" list.
    """
    if not path.is_file():
        raise HarnessError(f"input file not found: {path}", EXIT_BAD_INPUT)
    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except OSError as exc:
        raise HarnessError(f"cannot read input file {path}: {exc.strerror}", EXIT_BAD_INPUT) from exc
    except UnicodeDecodeError as exc:
        raise HarnessError(f"input file is not UTF-8 text: {path}", EXIT_BAD_INPUT) from exc
    except json.JSONDecodeError as exc:
        raise HarnessError(f"input file is not valid JSON: {path} (line {exc.lineno})", EXIT_BAD_INPUT) from exc

    if isinstance(data, dict) and isinstance(data.get("tickets"), list):
        data = data["tickets"]
    if not isinstance(data, list):
        raise HarnessError("input must be a JSON list of tickets", EXIT_BAD_INPUT)
    if not data:
        log.warning("input file contains no tickets")
    return data, hashlib.sha256(raw_bytes).hexdigest()


# ---------- progress (for --resume) ----------

def progress_path(output: Path) -> Path:
    return output.with_name(output.name + ".progress.jsonl")


def load_progress(path: Path, fingerprint: str, count: int) -> dict[int, dict[str, Any]]:
    """Results from an earlier interrupted run of the SAME input, keyed by position.

    The first line records which input file and ticket count the progress
    belongs to; if either differs, the old progress is discarded.
    """
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        log.warning("progress file unreadable, starting fresh: %s", path)
        return {}

    try:
        meta = json.loads(lines[0])["meta"] if lines else {}
    except (json.JSONDecodeError, KeyError, TypeError):
        meta = {}
    if meta.get("input_sha256") != fingerprint or meta.get("count") != count:
        log.warning("progress file belongs to a different input or limit; starting fresh")
        return {}

    done: dict[int, dict[str, Any]] = {}
    for line in lines[1:]:
        try:
            row = json.loads(line)
            index = int(row["index"])
            if 0 <= index < count and isinstance(row["result"], dict):
                done[index] = row["result"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # a half-written last line from a crash is redone
    return done


def start_progress(path: Path, fingerprint: str, count: int, keep: dict[int, dict[str, Any]]) -> None:
    """(Re)write the progress file with its header and any results being kept."""
    lines = [json.dumps({"meta": {"input_sha256": fingerprint, "count": count}})]
    lines += [json.dumps({"index": i, "result": r}) for i, r in sorted(keep.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- output ----------

def write_atomically(path: Path, results: list[dict[str, Any]]) -> None:
    """Write to a temporary file, then rename, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(results, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------- run ----------

def run(input_path: Path, output_path: Path, resume: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is not None and limit < 0:
        raise HarnessError("--limit must be zero or more", EXIT_BAD_INPUT)
    if output_path.resolve() == input_path.resolve():
        raise HarnessError("--output must not be the same file as --input", EXIT_BAD_INPUT)
    if output_path.is_dir():
        raise HarnessError(f"--output is a folder, not a file: {output_path}", EXIT_BAD_INPUT)

    tickets, fingerprint = load_tickets(input_path)
    if limit is not None:
        tickets = tickets[:limit]
    count = len(tickets)

    progress = progress_path(output_path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        done = load_progress(progress, fingerprint, count) if resume else {}
        start_progress(progress, fingerprint, count, done)
    except OSError as exc:
        raise HarnessError(f"cannot write to {output_path.parent}: {exc.strerror}", EXIT_WRITE_FAILED) from exc

    if done:
        log.info("resuming: %d of %d tickets already processed", len(done), count)

    try:
        warm_up()  # load models once, so the first ticket's latency is not inflated
    except Exception as exc:  # noqa: BLE001 - each ticket will then escalate with PIPELINE_ERROR
        log.error("pipeline could not start (%s); tickets will be escalated", exc)

    started = time.perf_counter()
    try:
        with progress.open("a", encoding="utf-8") as sink:
            for index, raw in enumerate(tickets):
                if index in done:
                    continue
                result = handle(raw).model_dump(mode="json")
                done[index] = result
                sink.write(json.dumps({"index": index, "result": result}) + "\n")
                sink.flush()
                if (index + 1) % 25 == 0:
                    log.info("processed %d / %d", index + 1, count)

        results = [done[i] for i in range(count)]
        write_atomically(output_path, results)
    except OSError as exc:
        raise HarnessError(
            f"failed writing results ({exc.strerror}); progress kept, rerun with --resume", EXIT_WRITE_FAILED
        ) from exc

    progress.unlink(missing_ok=True)

    elapsed = time.perf_counter() - started
    reasons = Counter(r["reason_code"] for r in results)
    log.info("done: %d tickets in %.1fs -> %s", count, elapsed, output_path)
    log.info("routes: %s", dict(Counter(r["route"] for r in results)))
    log.info("reason codes: %s", dict(reasons))
    for code in ("PIPELINE_ERROR", "INVALID_INPUT"):
        if reasons.get(code):
            log.warning("%d tickets escalated with %s; see the log above for details", reasons[code], code)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cloudserve_support.harness",
        description="Process a file of support tickets unattended.",
    )
    parser.add_argument("--input", required=True, type=Path, help="JSON file of tickets")
    parser.add_argument("--output", required=True, type=Path, help="where to write results")
    parser.add_argument("--resume", action="store_true", help="continue an interrupted run")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N tickets")
    args = parser.parse_args(argv)  # bad arguments exit with code 2 automatically

    setup_logging()
    try:
        run(args.input, args.output, resume=args.resume, limit=args.limit)
    except HarnessError as exc:
        log.error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        log.error("interrupted; progress saved. Rerun the same command with --resume to continue.")
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())