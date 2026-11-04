from typing import Any
from fastapi import Body, FastAPI
from cloudserve_support.app.schemas import TicketResult
from cloudserve_support.pipeline.graph import handle

app = FastAPI(title="CloudServe Support Triage", version="0.1.0")


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Welcome to the CloudServe Support Triage API"}

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets", response_model=TicketResult)
def submit_ticket(raw: Any = Body(...)) -> TicketResult:
    return handle(raw if isinstance(raw, dict) else {"_raw": raw})