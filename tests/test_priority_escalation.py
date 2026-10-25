"""Ep3 feature: priority escalation. Runs against a copy of production (the PR's branch)."""

import psycopg
import pytest

from agent_server import db


def test_existing_rows_backfilled_with_sla():
    # The key check: production already has tickets. ADD COLUMN ... NOT NULL without a default
    # fails on real rows — an empty test database would never catch it.
    rows = db.query("SELECT id, sla_hours FROM tickets")
    assert rows
    assert all(r["sla_hours"] is not None for r in rows)


def test_acme_482_escalated_to_urgent():
    (t,) = db.query("SELECT priority, sla_hours, escalated_at FROM tickets WHERE id = 482")
    assert t["priority"] == "urgent"
    assert t["sla_hours"] == 4
    assert t["escalated_at"] is not None


def test_non_enterprise_ticket_not_escalated():
    (t,) = db.query("SELECT priority, sla_hours, escalated_at FROM tickets WHERE id = 491")
    assert t["priority"] != "urgent"
    assert t["sla_hours"] == 24
    assert t["escalated_at"] is None


def test_new_enterprise_ticket_is_escalated_by_trigger(tx):
    tx.execute(
        "INSERT INTO tickets (id, customer_id, status, priority, subject) "
        "VALUES (900001, 1, 'open', 'high', 'test') RETURNING priority, sla_hours, escalated_at"
    )
    row = tx.fetchone()
    assert row["priority"] == "urgent" and row["sla_hours"] == 4 and row["escalated_at"]


def test_priority_check_rejects_unknown_value(tx):
    with pytest.raises(psycopg.errors.CheckViolation):
        tx.execute("UPDATE tickets SET priority = 'critical' WHERE id = 491")


def test_api_returns_badge_fields(api):
    r = api.get("/api/tickets", params={"customer_id": 1})
    assert r.status_code == 200
    t = next(t for t in r.json() if t["id"] == 482)
    assert t["priority"] == "urgent"
    assert t["sla_hours"] == 4
    assert t["escalated_at"]


def test_api_priority_update_goes_through_trigger(api):
    # Writes on the branch are safe — production never sees them. Reset 491 afterwards anyway.
    (before,) = db.query("SELECT priority FROM tickets WHERE id = 491")
    try:
        r = api.post("/api/tickets/priority", json={"ticket_id": 491, "priority": "high"})
        assert r.status_code == 200
        (t,) = db.query("SELECT priority FROM tickets WHERE id = 491")
        assert t["priority"] == "high"  # Bluefin is 'pro' tier -> not escalated
    finally:
        db.execute("UPDATE tickets SET priority = %s WHERE id = 491", (before["priority"],))
