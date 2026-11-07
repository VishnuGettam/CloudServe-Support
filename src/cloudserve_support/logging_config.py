"""Shared logging setup for the API and the harness.

Level comes from the LOG_LEVEL environment variable (default INFO). Logs go
to stderr, so the harness's stdout and output file stay clean.
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_cloudserve_configured", False):
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    root._cloudserve_configured = True  # type: ignore[attr-defined]