"""One-time setup so GitHub Actions can branch, migrate, and deploy (Ep3). Idempotent.

    uv run python scripts/setup_ci.py --github-repo <owner>/<repo>

Databricks side:
  1. Service principal `building-on-lakebase-ci` (created if missing).
  2. CAN_MANAGE on the Lakebase project, so CI can create/delete branches.
  3. A Postgres role for the SP on `production` (OAuth login, CREATEROLE for app grants).
  4. A NOLOGIN `support_owner` role that OWNS the operational schema + tables, granted to you and
     the SP. migrate.py runs `SET ROLE support_owner`, so whoever migrates (you or CI) can ALTER
     the tables and new objects keep one owner. Branches copy roles, so every ci-pr-* has this.
  5. A NOLOGIN `support_app` role holding what the app needs (read/write operational tables,
     read synced KB tables). The SP gets it WITH ADMIN OPTION, so after deploying the app CI just
     runs `GRANT support_app TO <app SP>` (scripts/grant_app.py).

GitHub side (only with --github-repo; uses your `gh` login):
  6. A fresh OAuth secret for the SP -> repo secrets DATABRICKS_HOST / _CLIENT_ID / _CLIENT_SECRET.
     The secret goes straight from the API to `gh secret set` (stdin) — it is never printed.
  7. Repo variables for the non-secret config (project, schemas, UC catalog, ...).
  8. A `production` environment with you as required reviewer — the approval gate before prod.

Use --branch dev-<you> to rehearse step 4 on a branch first.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.postgres import (  # noqa: E402
    Role,
    RoleAttributes,
    RoleAuthMethod,
    RoleIdentityType,
    RoleRoleSpec,
)

PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT", "lakebase-customer-support")
SCHEMA = os.getenv("LAKEBASE_SCHEMA", "customer_support")
SYNCED_SCHEMA = os.getenv("LAKEBASE_SYNCED_SCHEMA", "kb_synced")
OWNER_ROLE = os.getenv("MIGRATION_ROLE", "support_owner")
APP_ROLE = os.getenv("APP_ROLE", "support_app")
SP_NAME = "building-on-lakebase-ci"

# Non-secret config CI needs. Values come from your .env (falling back to the repo defaults).
REPO_VARIABLES = {
    "LAKEBASE_AUTOSCALING_PROJECT": PROJECT,
    "LAKEBASE_SCHEMA": SCHEMA,
    "LAKEBASE_SYNCED_SCHEMA": SYNCED_SCHEMA,
    "LAKEBASE_SEARCH_PATH": os.getenv("LAKEBASE_SEARCH_PATH", "customer_support, kb_synced, public"),
    "UC_CATALOG": os.getenv("UC_CATALOG", "main"),
    "UC_SCHEMA": os.getenv("UC_SCHEMA", "customer_support"),
    "PROD_APP_NAME": os.getenv("PROD_APP_NAME", "support-copilot-ep3"),
    "MANAGED_MEMORY_STORE": os.getenv("PROD_MEMORY_STORE", "support-copilot-ep3"),
    "MANAGED_SESSION_STORE": os.getenv("PROD_SESSION_STORE", "support-copilot-ep3"),
}


def ensure_sp(w: WorkspaceClient):
    found = list(w.service_principals.list(filter=f'displayName eq "{SP_NAME}"'))
    if found:
        print(f"  ✓ service principal exists: {SP_NAME} ({found[0].application_id})")
        return found[0]
    sp = w.service_principals.create(display_name=SP_NAME, active=True)
    print(f"  ✓ created service principal: {SP_NAME} ({sp.application_id})")
    return sp


def grant_project(w: WorkspaceClient, app_id: str) -> None:
    w.api_client.do(
        "PATCH",
        f"/api/2.0/permissions/database-projects/{PROJECT}",
        body={"access_control_list": [{"service_principal_name": app_id, "permission_level": "CAN_MANAGE"}]},
    )
    print(f"  ✓ CAN_MANAGE on Lakebase project '{PROJECT}'")


def ensure_pg_role(w: WorkspaceClient, branch: str, app_id: str) -> None:
    parent = f"projects/{PROJECT}/branches/{branch}"
    for r in w.postgres.list_roles(parent=parent):
        if r.status and r.status.postgres_role == app_id:
            print(f"  ✓ Postgres role exists on '{branch}'")
            return
    w.postgres.create_role(
        parent=parent,
        role=Role(
            spec=RoleRoleSpec(
                postgres_role=app_id,
                identity_type=RoleIdentityType.SERVICE_PRINCIPAL,
                auth_method=RoleAuthMethod.LAKEBASE_OAUTH_V1,
                attributes=RoleAttributes(createrole=True),
            )
        ),
    ).wait()
    print(f"  ✓ Postgres role created on '{branch}'")


def ensure_roles(branch: str, me: str, sp_app_id: str | None) -> None:
    os.environ["LAKEBASE_AUTOSCALING_BRANCH"] = branch
    from agent_server import db

    members = [me] + ([sp_app_id] if sp_app_id else [])
    with db._get_pool().connection() as conn, conn.cursor() as cur, conn.transaction():
        for role in (OWNER_ROLE, APP_ROLE):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE ROLE "{role}" NOLOGIN')

        # Owner role: owns the operational schema + every table in it.
        for m in members:
            cur.execute(f'GRANT "{OWNER_ROLE}" TO "{m}"')
        cur.execute(f'ALTER SCHEMA "{SCHEMA}" OWNER TO "{OWNER_ROLE}"')
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s", (SCHEMA,))
        for t in [r["tablename"] for r in cur.fetchall()]:
            cur.execute(f'ALTER TABLE "{SCHEMA}"."{t}" OWNER TO "{OWNER_ROLE}"')

        # App role: what the deployed app may do — now, and on tables future migrations create.
        cur.execute(f'GRANT USAGE ON SCHEMA "{SCHEMA}" TO "{APP_ROLE}"')
        cur.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{SCHEMA}" TO "{APP_ROLE}"')
        cur.execute(f'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "{SCHEMA}" TO "{APP_ROLE}"')
        cur.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER_ROLE}" IN SCHEMA "{SCHEMA}" '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{APP_ROLE}"'
        )
        cur.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER_ROLE}" IN SCHEMA "{SCHEMA}" '
            f'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "{APP_ROLE}"'
        )
        cur.execute(f'GRANT USAGE ON SCHEMA "{SYNCED_SCHEMA}" TO "{APP_ROLE}"')
        cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{SYNCED_SCHEMA}" TO "{APP_ROLE}"')
        if sp_app_id:
            cur.execute(f'GRANT "{APP_ROLE}" TO "{sp_app_id}" WITH ADMIN OPTION')
    print(f"  ✓ '{OWNER_ROLE}' owns '{SCHEMA}' (members: {', '.join(members)}) on '{branch}'")
    print(f"  ✓ '{APP_ROLE}' has app privileges on '{SCHEMA}' + read on '{SYNCED_SCHEMA}'")


def gh(*args: str, stdin: str | None = None) -> None:
    subprocess.run(["gh", *args], input=stdin, text=True, check=True, stdout=subprocess.DEVNULL)


def setup_github(w: WorkspaceClient, sp, repo: str, me: str) -> None:
    secret = w.service_principal_secrets_proxy.create(service_principal_id=sp.id)
    gh("secret", "set", "DATABRICKS_HOST", "--repo", repo, stdin=w.config.host)
    gh("secret", "set", "DATABRICKS_CLIENT_ID", "--repo", repo, stdin=sp.application_id)
    gh("secret", "set", "DATABRICKS_CLIENT_SECRET", "--repo", repo, stdin=secret.secret)
    print("  ✓ repo secrets: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET")
    for k, v in {**REPO_VARIABLES, "OWNER_USER": me}.items():
        gh("variable", "set", k, "--repo", repo, "--body", v)
    print(f"  ✓ repo variables: {', '.join(REPO_VARIABLES)}, OWNER_USER")

    owner = repo.split("/")[0]
    uid = subprocess.run(["gh", "api", f"users/{owner}", "--jq", ".id"], text=True, check=True, capture_output=True).stdout.strip()
    gh(
        "api", "-X", "PUT", f"repos/{repo}/environments/production", "--input", "-",
        stdin=json.dumps({"reviewers": [{"type": "User", "id": int(uid)}]}),
    )
    print(f"  ✓ environment 'production' (required reviewer: {owner})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="production", help="Lakebase branch for the Postgres setup")
    ap.add_argument("--github-repo", help="owner/repo — also configure GitHub secrets/vars/environment")
    ap.add_argument("--skip-sp", action="store_true", help="only the Postgres roles, no SP (rehearse on a branch)")
    args = ap.parse_args()

    w = WorkspaceClient()
    me = w.current_user.me().user_name
    if args.skip_sp:
        ensure_roles(args.branch, me, None)
        return 0

    sp = ensure_sp(w)
    grant_project(w, sp.application_id)
    ensure_pg_role(w, args.branch, sp.application_id)
    ensure_roles(args.branch, me, sp.application_id)
    if args.github_repo:
        setup_github(w, sp, args.github_repo, me)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
