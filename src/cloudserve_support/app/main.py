"""FastAPI front door for live tickets (FR-01).

Error handling:
- A ticket that fails validation, or a body that is not valid JSON, returns
  200 with an INVALID_INPUT escalation: the ticket is recorded, never dropped.
- Any unexpected server error returns 500 with a PIPELINE_ERROR escalation
  body, so callers always receive the same response shape.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cloudserve_support.app.schemas import ReasonCode, TicketResult
from cloudserve_support.errors import describe
from cloudserve_support.logging_config import setup_logging
from cloudserve_support.pipeline.graph import escalation, handle

setup_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="CloudServe Support Triage", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def bad_request_body(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Body missing or not valid JSON: escalate as INVALID_INPUT instead of a bare 422."""
    kinds = sorted({err.get("type", "invalid") for err in exc.errors()})
    log.warning("rejected request body on %s: %s", request.url.path, kinds)
    result = escalation(None, ReasonCode.INVALID_INPUT, f"request body rejected: {', '.join(kinds)}")
    return JSONResponse(status_code=200, content=jsonable_encoder(result))


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence. handle() should already have caught everything."""
    log.exception("unhandled error on %s", request.url.path)
    result = escalation(None, ReasonCode.PIPELINE_ERROR, describe(exc))
    return JSONResponse(status_code=500, content=jsonable_encoder(result))


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "CloudServe Support Triage API is running."}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets", response_model=TicketResult)
def submit_ticket(raw: Any = Body(...)) -> TicketResult:
    """Accept one ticket and return its decision."""
    return handle(raw)