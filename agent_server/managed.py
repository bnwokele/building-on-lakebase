"""Databricks managed sessions + memory for the OpenAI Agents SDK.

The OpenAI Agents SDK has a first-class **Session** slot, and Databricks ships a managed Session
that plugs straight into it, so short-term memory is one call:

    from databricks_agentkit.openai import session_store
    result = await Runner.run(agent, messages, session=session_store(session_id, actor=actor))

`session_store(...)` returns a `DatabricksSessionStore` (an Agents-SDK `SessionABC`) backed by the
managed **Session Store REST API** — durable across restarts/replicas, with no Lakebase connection
of your own. The SDK reads prior turns from it before the run and appends the new ones after.

The SDK is the Databricks **AgentKit SDK** (`pip install "databricks-agentbricks[openai]"`,
module `databricks_agentkit`).

Long-term memory is exposed as agent tools (search/save), scoped per user by `actor_id`, backed by
the AgentKit **memory store** client. Writes use **upsert-on-stable-path** (managed `add()` refuses
to overwrite an existing (actor, path), so a preference *update* must update the existing entry,
otherwise the stale value sticks). `actor` is closed over, never a tool argument, so the model
cannot read or write another user's memory.

Docs:
  https://docs.databricks.com/aws/en/agents/agent-memory/managed-memory
  https://docs.databricks.com/aws/en/agents/agent-memory/managed-sessions
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from agents import function_tool
from agents.memory import SessionABC
from databricks.sdk import WorkspaceClient
from databricks_agentkit import AgentKitClient
from databricks_agentkit.openai import session_store as _agentkit_session_store
from mlflow.genai.agent_server import get_request_headers

logger = logging.getLogger(__name__)

# The managed Session Store REST call can occasionally hit a transient transport error: a
# truncated response (`ChunkedEncodingError`) or a platform `Cancelled`/`DeadlineExceeded`/
# `Unavailable`. The native session adapter only catches NotFound, so any of these would surface
# as a 500 on an otherwise-fine turn. We (a) retry the session's HTTP ops on these transient
# errors, and (b) if they still fail, degrade to no short-term history for that turn rather than
# erroring — the LLM, tools, and long-term memory all still work.
_TRANSIENT: tuple = ()
try:
    from requests.exceptions import ChunkedEncodingError, ConnectionError as _ReqConnError, Timeout
    _TRANSIENT += (ChunkedEncodingError, _ReqConnError, Timeout)
except Exception:  # noqa: BLE001
    pass
try:
    from databricks.sdk.errors import platform as _dbx_platform  # transient gRPC/platform statuses
    _TRANSIENT += tuple(
        c for c in (
            getattr(_dbx_platform, "Cancelled", None),
            getattr(_dbx_platform, "DeadlineExceeded", None),
            getattr(_dbx_platform, "Aborted", None),
            getattr(_dbx_platform, "InternalError", None),
            getattr(_dbx_platform, "TemporarilyUnavailable", None),
        )
        if isinstance(c, type)
    )
except Exception:  # noqa: BLE001
    pass

MEMORY_STORE_NAME = os.getenv("MANAGED_MEMORY_STORE", "support-copilot")
SESSION_STORE_NAME = os.getenv("MANAGED_SESSION_STORE", "support-copilot")

_client: Optional[AgentKitClient] = None
_memory_store = None
_stores_ready = False


def _managed_client() -> AgentKitClient:
    """Build the AgentKit client (cached), per the docs: AgentKitClient(WorkspaceClient())."""
    global _client
    if _client is None:
        _client = AgentKitClient(WorkspaceClient())
    return _client


def _get_or_create(collection, name):
    """create() the store, or fall back to get() if it already exists (idempotent)."""
    try:
        return collection.create(name)
    except Exception as e:  # noqa: BLE001 — "already exists" is the expected path on re-create
        if "exist" in str(e).lower():
            return collection.get(name)
        raise


def memory_store():
    global _memory_store
    if _memory_store is None:
        _memory_store = _get_or_create(_managed_client().memory_stores, MEMORY_STORE_NAME)
    return _memory_store


def ensure_stores() -> None:
    """Create-or-get BOTH managed stores once, at startup. The native session adapter creates the
    per-conversation *session* on first use, but not the store itself — so we ensure the store
    (and the memory store) exist up front. Idempotent; degrades to a warning if managed state is
    unavailable (e.g. local dev without managed state configured)."""
    global _stores_ready
    if _stores_ready:
        return
    try:
        _get_or_create(_managed_client().session_stores, SESSION_STORE_NAME)
        memory_store()
        _stores_ready = True
        logger.info("managed stores ready: session=%s memory=%s", SESSION_STORE_NAME, MEMORY_STORE_NAME)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not ensure managed stores (continuing): %s", e)


# ── Managed SESSIONS (short-term) — native OpenAI Agents SDK Session ────────────
class _ResilientSession(SessionABC):
    """Wraps the managed Session so each REST op retries on a transient transport error, instead of
    failing the whole turn on a one-off truncated response. Delegates everything else to the inner
    AgentKit session; retries only the narrow `_TRANSIENT` set (not real errors like NotFound)."""

    def __init__(self, inner: SessionABC, retries: int = 3) -> None:
        self._inner = inner
        self._retries = retries

    @property
    def session_id(self) -> str:
        return self._inner.session_id

    async def _with_retry(self, make_coro):
        last: Optional[BaseException] = None
        for attempt in range(self._retries):
            try:
                return await make_coro()  # fresh coroutine each attempt
            except _TRANSIENT as e:  # type: ignore[misc]
                last = e
                logger.warning("managed session transient error (try %d): %s", attempt + 1, e)
                await asyncio.sleep(0.4 * (attempt + 1))
        raise last  # exhausted retries

    async def get_items(self, limit: int | None = None):
        # Best-effort: on persistent failure, run this turn WITHOUT prior history rather than 500.
        try:
            return await self._with_retry(lambda: self._inner.get_items(limit=limit))
        except Exception as e:  # noqa: BLE001
            logger.warning("managed session read failed; running without history this turn: %s", e)
            return []

    async def add_items(self, items):
        # Best-effort: if the transcript can't be saved, don't fail the turn (it's already answered).
        try:
            return await self._with_retry(lambda: self._inner.add_items(items))
        except Exception as e:  # noqa: BLE001
            logger.warning("managed session write failed; turn not persisted to session: %s", e)
            return None

    async def pop_item(self):
        try:
            return await self._with_retry(lambda: self._inner.pop_item())
        except Exception as e:  # noqa: BLE001
            logger.warning("managed session pop failed: %s", e)
            return None

    async def clear_session(self):
        try:
            return await self._with_retry(lambda: self._inner.clear_session())
        except Exception as e:  # noqa: BLE001
            logger.warning("managed session clear failed: %s", e)
            return None


def managed_session(session_id: str, actor: str) -> SessionABC:
    """The managed Session for one conversation, ready to hand to Runner.run(session=...).

    Backed by the Databricks Session Store REST API and partitioned by `actor` (the signed-in
    user), so each user's transcripts stay separate. This is the whole short-term-memory story —
    no manual load/append (that's what the OpenAI Agents SDK Session slot buys us). Wrapped so a
    transient truncated response retries rather than 500-ing the turn."""
    inner = _agentkit_session_store(session_id, actor=actor, store=SESSION_STORE_NAME)
    return _ResilientSession(inner)


# ── Managed MEMORY (long-term) as agent tools ──────────────────────────────────
def memory_tools(actor_id: str) -> list:
    """Long-term memory tools bound to one user (actor_id), backed by the managed memory store.

    `actor_id` is captured in the closures — the model sees only each tool's own parameters and
    cannot set or spoof whose memory it touches."""
    store = memory_store()

    @function_tool
    def search_memory(query: str) -> str:
        """Search the user's long-term memory for relevant facts. Use at the START of a
        conversation and whenever you need prior context about this user."""
        try:
            results = store.search(actor_id=actor_id, query=query, limit=10)
        except Exception as e:  # noqa: BLE001
            logger.warning("managed memory search failed: %s", e)
            return "Memory is unavailable right now."
        lines = []
        for r in results:
            mem = getattr(r, "memory", r)
            path = getattr(mem, "path", "") or (mem.get("path", "") if isinstance(mem, dict) else "")
            content = getattr(mem, "content", "") or (mem.get("content", "") if isinstance(mem, dict) else "")
            lines.append(f"- {path}: {content}")
        return "Relevant memories:\n" + "\n".join(lines) if lines else "No memories found for this user."

    @function_tool
    def save_memory(path: str, content: str, description: str = "") -> str:
        """Save/UPDATE a durable, long-term fact about the user (preferences, ongoing goals). `path`
        is a stable slug like '/preferences/communication.md' — reuse the SAME path to update an
        existing fact (e.g. always '/preferences/communication.md' for how they like to be
        contacted). Do NOT save one-off or sensitive details."""
        try:
            # Upsert: managed add() refuses an existing (actor, path), so update it if present.
            existing = next(
                (m for m in store.list(actor_id=actor_id, path_prefix=path) if getattr(m, "path", None) == path),
                None,
            )
            if existing is not None:
                existing.update(content=content, description=description or None)
                return f"Updated memory at {path}."
            store.add(actor_id=actor_id, path=path, content=content, description=description)
            return f"Saved memory at {path}."
        except Exception as e:  # noqa: BLE001
            logger.warning("managed memory save failed: %s", e)
            return "Could not save memory right now."

    return [search_memory, save_memory]


# ── Trusted identity ────────────────────────────────────────────────────────────
def get_user_id(request) -> Optional[str]:
    """actor source, most-trusted first:
    1) the Databricks Apps authenticated user (X-Forwarded-Email header) — cannot be spoofed by
       the caller; read straight from the request headers (no middleware needed);
    2) local-dev fallback: custom_inputs.user_id, else request.context.user_id.
    """
    try:
        hdrs = get_request_headers() or {}
    except Exception:  # noqa: BLE001 — outside a request / header capture unavailable
        hdrs = {}
    email = hdrs.get("x-forwarded-email") or hdrs.get("X-Forwarded-Email")
    if email:
        return email
    ci = dict(getattr(request, "custom_inputs", None) or {})
    if ci.get("user_id"):
        return ci["user_id"]
    ctx = getattr(request, "context", None)
    return getattr(ctx, "user_id", None) if ctx else None
