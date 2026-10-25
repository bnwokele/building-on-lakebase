"""Custom operational UI mounted onto the agent server's FastAPI app.

The agent server already exposes the MLflow Responses API (`/responses` + `/invocations`) — we do
NOT touch that. Here we ADD the operational dashboard: `/api/*` routes (tickets/customers/notes
over the `db.py` layer) and serve a single page (dashboard + chat panel) at `/`.

start_server.py includes `router` + `ui` on its `app`. This module does NOT import the agent server,
so tests can mount the routers on a bare FastAPI app (see tests/conftest.py).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_server import db

router = APIRouter(prefix="/api", tags=["dashboard"])
ui = APIRouter()

_STATIC = Path(__file__).parent / "static"


@router.get("/customers")
def list_customers():
    return db.query("SELECT id, name, tier, email FROM customers ORDER BY name")


@router.get("/tickets")
def list_tickets(customer_id: int):
    return db.query(
        "SELECT id, subject, status, priority, sla_hours, escalated_at FROM tickets "
        "WHERE customer_id = %s "
        "ORDER BY created_at DESC",
        (customer_id,),
    )


@router.get("/notes")
def list_notes(ticket_id: int):
    return db.query(
        "SELECT author, note, created_at FROM support_interactions "
        "WHERE ticket_id = %s ORDER BY created_at",
        (ticket_id,),
    )


class NoteIn(BaseModel):
    ticket_id: int
    note: str
    author: str = "human"


@router.post("/notes")
def add_note(payload: NoteIn):
    db.execute(
        "INSERT INTO support_interactions (ticket_id, author, note) VALUES (%s, %s, %s)",
        (payload.ticket_id, payload.author, payload.note),
    )
    return {"ok": True}


class StatusIn(BaseModel):
    ticket_id: int
    status: str
    author: str = "human"


@router.post("/tickets/status")
def update_status(payload: StatusIn):
    db.execute("UPDATE tickets SET status = %s WHERE id = %s", (payload.status, payload.ticket_id))
    return {"ok": True}


class PriorityIn(BaseModel):
    ticket_id: int
    priority: str
    author: str = "human"


@router.post("/tickets/priority")
def update_priority(payload: PriorityIn):
    # The tickets_escalate trigger (migration 002) may raise high -> urgent for enterprise customers.
    db.execute("UPDATE tickets SET priority = %s WHERE id = %s", (payload.priority, payload.ticket_id))
    return {"ok": True}


# Serve the single-page UI at "/" (chat proxy is disabled, so "/" is ours).
@ui.get("/", include_in_schema=False)
def home():
    return FileResponse(_STATIC / "index.html")
