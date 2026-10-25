"""Give a deployed app's service principal its Lakebase privileges (Ep3 deploy step). Idempotent.

    uv run python scripts/grant_app.py --app support-copilot-ep3

The privileges themselves live on the `support_app` role (scripts/setup_ci.py), so this only
ensures the app SP has a Postgres role on the branch and is a member of `support_app`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks_ai_bridge.lakebase import LakebaseClient  # noqa: E402

PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT", "lakebase-customer-support")
BRANCH = os.getenv("LAKEBASE_AUTOSCALING_BRANCH", "production")
APP_ROLE = os.getenv("APP_ROLE", "support_app")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="Databricks App name")
    args = ap.parse_args()

    sp = WorkspaceClient().apps.get(args.app).service_principal_client_id
    print(f"  app '{args.app}' SP: {sp}")
    with LakebaseClient(project=PROJECT, branch=BRANCH) as c:
        try:
            c.create_role(sp, "SERVICE_PRINCIPAL")
            print("  ✓ Postgres role created")
        except Exception as e:  # noqa: BLE001
            if "exist" not in str(e).lower():
                raise
            print("  ✓ Postgres role exists")
        c.execute(f'GRANT "{APP_ROLE}" TO "{sp}"')
    print(f"  ✓ granted '{APP_ROLE}' on branch '{BRANCH}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
