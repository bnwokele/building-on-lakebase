"""Apply versioned SQL migrations (sql/migrations/NNN_*.sql) to a Lakebase branch.

Point it at any branch with LAKEBASE_AUTOSCALING_BRANCH — a PR branch in CI, `production` on
merge. The SAME files run in both places, so production only ever gets a migration that already
passed on a copy of production data.

    LAKEBASE_AUTOSCALING_BRANCH=ci-pr-42 uv run python scripts/migrate.py
    uv run python scripts/migrate.py --status

Applied versions are recorded in <LAKEBASE_SCHEMA>.schema_migrations. Each file runs in its own
transaction: if it fails, nothing from that file is applied and the run exits non-zero.

If you are a member of MIGRATION_ROLE (default `support_owner`, see scripts/setup_ci.py), the run
does `SET ROLE` to it, so you and CI can both ALTER the tables and new objects keep one owner.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from agent_server import db  # noqa: E402

MIGRATIONS = ROOT / "sql" / "migrations"
MIGRATION_ROLE = os.environ.get("MIGRATION_ROLE", "support_owner")


def _migrations() -> list[tuple[str, Path]]:
    return [(p.name.split("_", 1)[0], p) for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))]


def _applied(cur) -> dict[str, str]:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
             version    TEXT PRIMARY KEY,
             name       TEXT NOT NULL,
             checksum   TEXT NOT NULL,
             applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
    )
    cur.execute("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show applied/pending, change nothing")
    args = ap.parse_args()

    branch = os.environ.get("LAKEBASE_AUTOSCALING_BRANCH", "production")
    print(f"  Migrating schema '{db.SCHEMA}' on branch '{branch}'")

    with db._get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (db.SCHEMA,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE SCHEMA "{db.SCHEMA}"')
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s AND pg_has_role(current_user, oid, 'MEMBER')",
            (MIGRATION_ROLE,),
        )
        if cur.fetchone():
            cur.execute(f'SET ROLE "{MIGRATION_ROLE}"')
            print(f"  (as role '{MIGRATION_ROLE}')")
        try:
            return _apply(conn, cur, args.status)
        finally:
            cur.execute("RESET ROLE")  # pooled connection — don't leak the role


def _apply(conn, cur, status_only: bool) -> int:
    cur.execute(f"SET search_path = {db._SEARCH_PATH}")
    applied = _applied(cur)

    for version, path in _migrations():
        sql = path.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
        if version in applied:
            if applied[version] != checksum:
                print(f"  ⚠ {path.name} changed after it was applied (checksum mismatch)")
            print(f"  ✓ {path.name} (already applied)")
            continue
        if status_only:
            print(f"  … {path.name} (pending)")
            continue
        print(f"  → applying {path.name}")
        try:
            with conn.transaction():
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (version, path.name, checksum),
                )
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {path.name} FAILED — rolled back, nothing applied:\n    {e}", file=sys.stderr)
            return 1
        print(f"  ✓ {path.name} applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
