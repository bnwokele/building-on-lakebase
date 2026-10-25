"""End-to-end tests for THIS app (OpenAI Agents SDK + AgentKit managed session/memory).

Exercises the real surface the app exposes — the operational `/api/*` routes (Lakebase) and the
agent `/responses` (tools, managed session, managed memory, propose→confirm). Replaces the stale
`test_long_running_agent.py`, which targets the long-running Responses API this app doesn't use.

Run against a LOCAL server (start it first), serial (shared session/memory state):

    uv run start-server &                      # or in another terminal
    uv run pytest scripts/test_app_e2e.py -v -n0

Against the deployed app, pass its URL + a token:
    uv run pytest scripts/test_app_e2e.py -v -n0 \
        --agent-url https://<your-app>-....databricksapps.com --token "$(databricks auth token -p <profile> | jq -r .access_token)"
"""
from __future__ import annotations

import uuid

import httpx
import pytest

TIMEOUT = 90.0
# Unique per run so managed-memory assertions are deterministic (no leftover from earlier runs).
USER = f"pytest-{uuid.uuid4().hex[:8]}@test.example"
# Managed SESSIONS are durable, so thread ids must be unique per run — otherwise a reran test
# reuses a thread that already holds the prior answer and the agent skips the tool call.
RUN = uuid.uuid4().hex[:6]


@pytest.fixture(scope="session")
def base_url(request):
    return request.config.getoption("--agent-url").rstrip("/")


@pytest.fixture(scope="session")
def headers(request):
    tok = request.config.getoption("--token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _invoke(base_url, headers, text, thread, user=USER):
    """POST one turn to /responses; return (reply_text, tool_names, proposals)."""
    body = {"input": [{"role": "user", "content": text}],
            "custom_inputs": {"user_id": user, "thread_id": thread}}
    r = httpx.post(f"{base_url}/responses", json=body, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    reply, tools, proposals = "", [], []
    for it in data.get("output", []):
        t = it.get("type")
        if t == "message":
            reply = "".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
        elif t == "function_call":
            tools.append(it.get("name"))
        elif t == "function_call_output":
            out = it.get("output")
            if isinstance(out, str):
                try:
                    import json
                    out = json.loads(out)
                except Exception:
                    out = {}
            if isinstance(out, dict) and out.get("type") == "proposal":
                proposals.append(out)
    return reply, tools, proposals


# ── operational API (Lakebase, direct Postgres) ────────────────────────────────
def test_health(base_url, headers):
    r = httpx.get(f"{base_url}/health", headers=headers, timeout=TIMEOUT)
    assert r.status_code == 200


def test_api_customers(base_url, headers):
    r = httpx.get(f"{base_url}/api/customers", headers=headers, timeout=TIMEOUT)
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert any("Acme" in n for n in names), names


def test_api_tickets(base_url, headers):
    r = httpx.get(f"{base_url}/api/tickets", params={"customer_id": 1}, headers=headers, timeout=TIMEOUT)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── agent tools (grounding) ─────────────────────────────────────────────────────
def test_tool_search_customer(base_url, headers):
    reply, tools, _ = _invoke(base_url, headers, "Brief me on Acme Robotics.", f"t-cust-{RUN}")
    assert "search_customer" in tools, tools
    assert reply


def test_tool_search_kb(base_url, headers):
    reply, tools, _ = _invoke(base_url, headers, "Why do nightly syncs fail at 2 a.m.?", f"t-kb-{RUN}")
    assert "search_kb" in tools, tools
    assert reply


# ── managed SESSION (short-term): recall within one thread ──────────────────────
def test_managed_session(base_url, headers):
    th = f"t-sess-{uuid.uuid4().hex[:6]}"
    _invoke(base_url, headers, "My name is Dana and I'm investigating ticket number 5.", th)
    reply, _, _ = _invoke(base_url, headers, "Which ticket number am I investigating?", th)
    assert "5" in reply, reply


# ── managed MEMORY (long-term): save → cross-thread recall → upsert update ──────
def test_managed_memory_save_recall_update(base_url, headers):
    _invoke(base_url, headers, "Remember that I prefer email over Slack for updates.", f"t-mem1-{RUN}")
    reply, tools, _ = _invoke(base_url, headers, "How do I prefer to be contacted?", f"t-mem2-{RUN}")
    assert "search_memory" in tools, tools
    assert "email" in reply.lower(), reply
    # upsert: update then recall in a fresh thread must reflect the NEW value, not the stale one
    _invoke(base_url, headers, "Actually, I prefer Slack now, not email.", f"t-mem3-{RUN}")
    reply2, _, _ = _invoke(base_url, headers, "What's my current contact preference?", f"t-mem4-{RUN}")
    assert "slack" in reply2.lower(), reply2


# ── propose → confirm (agent proposes a write; does not commit) ─────────────────
def test_propose_ticket_action(base_url, headers):
    # Use a concrete, always-valid action (add a note to a known ticket) so the agent proposes in
    # one turn — "resolve the open ticket" is ambiguous when the customer has no open ticket.
    _, tools, proposals = _invoke(
        base_url, headers,
        "Add a note to ticket 482 saying we rotated the auth token.", f"t-prop-{RUN}")
    assert "propose_ticket_action" in tools, tools
    assert proposals, "expected at least one proposal in the output"
    assert proposals[0].get("ticket_id") is not None
