"""Baseline checks: the branch has the schema AND production's data."""

from agent_server import db


def test_migrations_recorded():
    versions = [r["version"] for r in db.query("SELECT version FROM schema_migrations ORDER BY version")]
    assert "001" in versions


def test_branch_has_production_data():
    # A copy-on-write branch carries production rows — this is what makes the tests meaningful.
    rows = db.query("SELECT id FROM tickets")
    assert rows, "branch has no tickets — was it created from production?"


def test_ticket_482_present():
    (t,) = db.query("SELECT status, customer_id FROM tickets WHERE id = 482")
    assert t["customer_id"] == 1


def test_api_lists_tickets(api):
    r = api.get("/api/tickets", params={"customer_id": 1})
    assert r.status_code == 200
    assert {"id", "subject", "status", "priority"} <= set(r.json()[0])
