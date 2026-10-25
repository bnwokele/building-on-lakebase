"""Ensure the Lakebase autoscaling project + branch exist (idempotent). Safe to re-run.

get_project first; create only if missing (notebook Step 1 pattern). If the project already
exists this is a no-op — it never fails on an existing project. Run by deploy.sh before
`bundle deploy` (the `postgres` app resource binds to this project/branch but does NOT create it).

    uv run python scripts/ensure_lakebase_project.py

NOTE: creating a fresh project does NOT enable Lakebase Search — that's a one-time, irreversible
UI toggle (Project > Settings > Lakebase Search > Enable). KB hybrid search needs it on before the
lakebase_vector / lakebase_text extensions can be created (deploy_seed.py degrades gracefully if not).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from databricks.sdk import WorkspaceClient  # noqa: E402

PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT", "lakebase-customer-support")
BRANCH = os.getenv("LAKEBASE_AUTOSCALING_BRANCH", "production")


def main() -> int:
    w = WorkspaceClient()
    try:
        p = w.postgres.get_project(name=f"projects/{PROJECT}")
        print(f"  ✓ Lakebase project exists: {p.name}")
    except Exception:
        from databricks.sdk.service.postgres import Project, ProjectSpec

        print(f"  Creating Lakebase project '{PROJECT}' (pg 17)...")
        op = w.postgres.create_project(
            project=Project(spec=ProjectSpec(display_name="Support Copilot", pg_version=17)),
            project_id=PROJECT,
        )
        p = op.wait()
        print(f"  ✓ created: {p.name}")

    # Creating a project auto-provisions the `production` branch + `primary` endpoint. Confirm an
    # endpoint exists on the target branch so the app has something to connect to.
    try:
        eps = list(w.postgres.list_endpoints(parent=f"projects/{PROJECT}/branches/{BRANCH}"))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ could not list endpoints on branch '{BRANCH}': {e}", file=sys.stderr)
        return 0
    if eps:
        print(f"  ✓ endpoint ready: {eps[0].name} ({eps[0].status.hosts.host})")
    else:
        print(
            f"  ⚠ no endpoint on branch '{BRANCH}' yet — if this is a non-production branch, "
            "create it + an endpoint first (see the databricks-lakebase skill).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
