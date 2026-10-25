"""Support-copilot tools (the operational data layer, exposed to the agent) — OpenAI Agents SDK.

Declared with the OpenAI Agents SDK `@function_tool`. search_customer/search_kb are grounding;
propose_ticket_action is the "agent writes with confirm" tool — it RETURNS a proposal, it does NOT
write. Confirmation (and the actual write) happens through the app UI / `/api/*` path.
"""
import json

from agents import function_tool

from agent_server import db, search


@function_tool
def search_customer(customer: str) -> dict:
    """Look up a customer by NAME (e.g. "Acme Robotics", partial names OK) or numeric id.
    Returns their profile — tier, and lifetime value + churn score from the synced
    customer_360 table — plus their tickets."""
    q = str(customer).strip()
    if q.isdigit():
        rows = db.query("SELECT id, name, tier, email FROM customers WHERE id = %s", (int(q),))
    else:
        rows = db.query(
            "SELECT id, name, tier, email FROM customers WHERE name ILIKE %s ORDER BY name LIMIT 1",
            (f"%{q}%",),
        )
    if not rows:
        return {"found": False, "query": customer}
    c = rows[0]
    profile = db.query(
        "SELECT lifetime_value, churn_score FROM customer_360 WHERE customer_id = %s", (c["id"],)
    )
    tickets = db.query(
        "SELECT id, subject, status, priority FROM tickets WHERE customer_id = %s ORDER BY created_at DESC",
        (c["id"],),
    )
    return {"found": True, "customer": {**c, **(profile[0] if profile else {})}, "tickets": tickets}


@function_tool
def search_kb(query: str) -> list:
    """Hybrid Lakebase Search (vector + BM25) over the product knowledge base. Cite titles."""
    return search.hybrid_search_kb(query)


@function_tool
def propose_ticket_action(ticket_id: int, action: str, value: str) -> str:
    """Propose a ticket change for human confirmation (does NOT commit).
    action='status' (value=open|pending|resolved), action='priority' (value=low|normal|high|urgent),
    or action='note' (value=note text)."""
    # Return a JSON *string* (not a dict): the OpenAI Agents SDK serializes a dict return with
    # Python repr() (single quotes), which isn't valid JSON — the dashboard's JSON.parse(output)
    # would then fail and the Confirm button wouldn't render. json.dumps keeps `output` valid JSON.
    return json.dumps({"type": "proposal", "ticket_id": ticket_id, "action": action, "value": value})


def support_tools() -> list:
    return [search_customer, search_kb, propose_ticket_action]
