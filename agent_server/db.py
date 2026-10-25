"""Operational Lakebase access, reused by the agent's tools.

Uses databricks_ai_bridge.lakebase.LakebasePool for auto-rotating OAuth tokens. Points at
the operational schema (LAKEBASE_SCHEMA, default 'customer_support') in your Lakebase project.
Long-term memory is Databricks-MANAGED (AgentKit REST) — it does not live in this project —
so there is no agent_memory schema here.
"""
import os
from pathlib import Path

from databricks_ai_bridge.lakebase import LakebasePool

_pool: LakebasePool | None = None
# All schema names come from the environment (no hardcoded literals): operational tables live
# in LAKEBASE_SCHEMA, synced tables (kb_articles, customer_360) land in LAKEBASE_SYNCED_SCHEMA.
SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "customer_support")
_SYNCED_SCHEMA = os.environ.get("LAKEBASE_SYNCED_SCHEMA", "kb_synced")
# search_path lets queries use UNqualified names (so no schema is hardcoded in any SQL). An
# explicit LAKEBASE_SEARCH_PATH wins; otherwise compose operational + synced + public (deduped).
# `public` is REQUIRED — the pgvector `vector` type + `<=>` operator, the Lakebase Search
# extensions (lakebase_vector/lakebase_text), the lakebase_ann/lakebase_bm25 access methods, and
# the vector_cosine_ops opclass all live there.
_SEARCH_PATH = os.environ.get(
    "LAKEBASE_SEARCH_PATH",
    ", ".join(dict.fromkeys([SCHEMA, _SYNCED_SCHEMA, "public"])),
)


def _get_pool() -> LakebasePool:
    """Build the pool the same way the memory store resolves Lakebase (see
    utils_memory.init_lakebase_config): prefer an autoscaling ENDPOINT, else PROJECT+BRANCH.

    - Databricks App: the `postgres` app resource injects LAKEBASE_AUTOSCALING_ENDPOINT.
    - Local dev: .env provides LAKEBASE_AUTOSCALING_PROJECT (+ optional BRANCH).

    Endpoint takes priority (they're mutually exclusive in the library), so a stray local
    PROJECT var can't shadow the injected endpoint on Databricks.
    """
    global _pool
    if _pool is None:
        endpoint = os.environ.get("LAKEBASE_AUTOSCALING_ENDPOINT") or None
        project = os.environ.get("LAKEBASE_AUTOSCALING_PROJECT") or None
        branch = os.environ.get("LAKEBASE_AUTOSCALING_BRANCH") or "production"
        common = dict(schema=SCHEMA, min_size=1, max_size=8)
        if endpoint:
            _pool = LakebasePool(autoscaling_endpoint=endpoint, **common)
        elif project:
            _pool = LakebasePool(project=project, branch=branch, **common)
        else:
            raise RuntimeError(
                "Lakebase is not configured. Set LAKEBASE_AUTOSCALING_ENDPOINT (Databricks "
                "App — injected by the `postgres` resource) or LAKEBASE_AUTOSCALING_PROJECT "
                "[+ LAKEBASE_AUTOSCALING_BRANCH] (local dev, in .env)."
            )
    return _pool


def query(sql: str, params=None) -> list[dict]:
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"SET search_path = {_SEARCH_PATH}")
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params=None) -> None:
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"SET search_path = {_SEARCH_PATH}")
        cur.execute(sql, params)


def bootstrap() -> str:
    """Create the operational schema + tables + seed data if the tickets table is absent."""
    ddl = (Path(__file__).parent.parent / "sql" / "01_operational.sql").read_text()
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        cur.execute("SELECT to_regclass(%s) AS t", (f"{SCHEMA}.tickets",))
        if cur.fetchone()["t"] is not None:
            return f"schema '{SCHEMA}' already initialized"
        cur.execute(ddl)
        return f"schema '{SCHEMA}' initialized (tables + seed data)"
