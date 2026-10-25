"""One-shot Lakebase seed + SP grant for the deployed app (idempotent, graceful).

Run AFTER `bundle deploy` + `bundle run` (deploy.sh does this for you). It runs as YOU (the
deployer, project+branch coords from .env) and:

  1. sql/01_operational.sql  — operational schema + tables + seed (always; idempotent)
  2. sql/02_kb_search.sql     — KB hybrid-search indexes  (only if kb_articles has rows)
  3. grant the app's service principal access to the operational + synced schemas (best-effort)

Long-term memory is Databricks-MANAGED (AgentKit REST), not a pgvector store in this project,
so there is no agent_memory schema to index or grant here.

Steps 2-3 degrade gracefully: if a prerequisite isn't there yet (KB not synced from the
lakehouse, or you lack owner rights to grant), it prints exactly what to do and continues —
re-run this script once the prereq is met.

    uv run python scripts/deploy_seed.py               # seed + grant
    uv run python scripts/deploy_seed.py --app support-copilot --skip-grant
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent))  # allow importing the sibling render_sql module
from render_sql import render  # noqa: E402

# Import after load_dotenv so db.py reads the project/branch coords.
from agent_server import db  # noqa: E402

SQL_DIR = Path(__file__).parent.parent / "sql"
SCHEMA = os.getenv("LAKEBASE_SCHEMA", "customer_support")
SYNCED_SCHEMA = os.getenv("LAKEBASE_SYNCED_SCHEMA", "kb_synced")  # where synced tables land
APP_NAME = os.getenv("APP_NAME", "customer-support-copilot")
PROFILE = os.getenv("DATABRICKS_CONFIG_PROFILE")

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def ok(m):
    print(f"  {GREEN}✓{RESET} {m}")


def warn(m):
    print(f"  {YELLOW}⚠{RESET} {m}")


def info(m):
    print(f"{DIM}▶ {m}{RESET}")


def _regclass(qualified: str):
    """Return the resolved table name if it exists (via search_path), else None."""
    rows = db.query("SELECT to_regclass(%s) AS t", (qualified,))
    return rows[0]["t"] if rows else None


def run_operational() -> None:
    info("1/3  Operational tables + seed (sql/01_operational.sql)")
    db.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    db.execute(render((SQL_DIR / "01_operational.sql").read_text()))
    n = db.query("SELECT count(*) AS n FROM customers")[0]["n"]
    ok(f"operational schema '{SCHEMA}' ready ({n} customers seeded)")


def run_kb_indexes() -> None:
    info("2/3  KB hybrid-search indexes (sql/02_kb_search.sql)")
    if not _regclass("kb_articles"):
        warn(
            "kb_articles not found — build the lakehouse sources (lakehouse/00_seed_kb_raw.sql "
            "then build_kb.sql) and let the synced table land, then re-run this script."
        )
        return
    n = db.query("SELECT count(*) AS n FROM kb_articles")[0]["n"]
    if n == 0:
        warn("kb_articles is empty — wait for the synced table to finish, then re-run.")
        return
    try:
        db.execute(render((SQL_DIR / "02_kb_search.sql").read_text()))
        ok(f"KB indexes built over {n} articles (lakebase_ann + lakebase_bm25)")
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "shared_preload_libraries" in msg or "lakebase_vector" in msg or "lakebase_text" in msg:
            warn(
                "Lakebase Search isn't enabled on this project yet — the search extensions can't "
                "be created. Enable it once in the UI, then re-run this script:\n"
                "      Project > Settings > Lakebase Search > Enable  (restarts computes; irreversible)"
            )
        else:
            warn(f"KB index step failed: {e}")


def _sp_client_id(app_name: str) -> str | None:
    cmd = ["databricks", "apps", "get", app_name, "-o", "json"]
    if PROFILE:
        cmd += ["--profile", PROFILE]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("service_principal_client_id")
    except Exception as e:  # noqa: BLE001
        warn(f"could not resolve the app's service principal ({e}). Is '{app_name}' deployed?")
        return None


def grant_sp(app_name: str) -> None:
    info("3/3  Grant the app service principal access (best-effort)")
    sp = _sp_client_id(app_name)
    if not sp:
        warn("skipping grant — deploy the app first, then re-run: uv run python scripts/deploy_seed.py")
        return

    from databricks_ai_bridge.lakebase import (
        LakebaseClient,
        SchemaPrivilege,
        SequencePrivilege,
        TablePrivilege,
    )

    # Operational schema — FULL access. USAGE to enter, CREATE so bootstrap() can run its
    # CREATE TABLE/INDEX IF NOT EXISTS migrations; SELECT/INSERT/UPDATE/DELETE on tables; and
    # USAGE/SELECT/UPDATE on sequences (BIGSERIAL/SERIAL columns like support_interactions.id
    # need the backing sequence for INSERT).
    op_schema_privs = [SchemaPrivilege.USAGE, SchemaPrivilege.CREATE]
    op_table_privs = [
        TablePrivilege.SELECT,
        TablePrivilege.INSERT,
        TablePrivilege.UPDATE,
        TablePrivilege.DELETE,
    ]
    op_seq_privs = [SequencePrivilege.USAGE, SequencePrivilege.SELECT, SequencePrivilege.UPDATE]

    with LakebaseClient(
        project=os.environ["LAKEBASE_AUTOSCALING_PROJECT"],
        branch=os.getenv("LAKEBASE_AUTOSCALING_BRANCH", "production"),
    ) as client:
        try:
            client.create_role(sp, "SERVICE_PRINCIPAL")
            ok(f"SP role ready: {sp}")
        except Exception as e:  # noqa: BLE001
            (ok if "already exists" in str(e).lower() else warn)(f"role: {e}")

        # Operational schema — full access (app owns these tables).
        try:
            client.grant_schema(sp, op_schema_privs, [SCHEMA])
            client.grant_all_tables_in_schema(sp, op_table_privs, [SCHEMA])
            client.grant_all_sequences_in_schema(sp, op_seq_privs, [SCHEMA])
            ok(f"granted FULL access on operational schema '{SCHEMA}'")
        except Exception as e:  # noqa: BLE001
            warn(f"grant on operational '{SCHEMA}' skipped ({e})")

        # Synced schema — READ-ONLY. The sync pipeline owns these tables; the app only reads the KB.
        # Revoke any write/CREATE first so re-running enforces read-only (idempotent; a REVOKE of a
        # privilege that was never granted is a harmless no-op), then grant schema USAGE + SELECT.
        try:
            client.execute(f'REVOKE CREATE ON SCHEMA "{SYNCED_SCHEMA}" FROM "{sp}"')
            client.execute(
                f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES '
                f'IN SCHEMA "{SYNCED_SCHEMA}" FROM "{sp}"'
            )
            client.grant_schema(sp, [SchemaPrivilege.USAGE], [SYNCED_SCHEMA])
            client.grant_all_tables_in_schema(sp, [TablePrivilege.SELECT], [SYNCED_SCHEMA])
            ok(f"granted READ-ONLY (USAGE + SELECT) on synced schema '{SYNCED_SCHEMA}'")
        except Exception as e:  # noqa: BLE001
            warn(f"grant on synced '{SYNCED_SCHEMA}' skipped ({e})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed Lakebase + grant the app SP (idempotent).")
    ap.add_argument("--app", default=APP_NAME, help="Deployed app name (default: $APP_NAME or support-copilot)")
    ap.add_argument("--skip-grant", action="store_true", help="Seed only; don't grant the SP")
    ap.add_argument(
        "--phase",
        choices=["data", "post", "all"],
        default="all",
        help="data = operational + KB indexes (pre-app); post = SP grant "
        "(after the app is deployed); all = everything (default).",
    )
    args = ap.parse_args()

    print(f"\n{GREEN}Seeding Lakebase for '{args.app}'{RESET} (phase={args.phase}, project={os.getenv('LAKEBASE_AUTOSCALING_PROJECT')}, schema={SCHEMA})\n")
    if args.phase in ("data", "all"):
        run_operational()
        run_kb_indexes()
    if args.phase in ("post", "all"):
        if not args.skip_grant:
            grant_sp(args.app)
    print(f"\n{GREEN}▶ Seed ({args.phase}) complete.{RESET} Re-run any time — it's idempotent.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
