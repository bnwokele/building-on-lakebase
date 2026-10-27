"""Preview the dashboard against any Lakebase branch, e.g. a PR's ci-pr-<N>, before merging (Ep3).

    uv run python scripts/preview_branch.py ci-pr-3            # http://localhost:8001

Serves the same dashboard + /api routes as the deployed app (agent_server/webapp.py), connected
to the branch you name, so you can try the PR's database + app change on a copy of production
while the production app is untouched. The chat copilot is not started (dashboard only).
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("branch_id", help="Lakebase branch, e.g. ci-pr-3")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()

    # Must be set before agent_server.db builds its connection pool.
    os.environ["LAKEBASE_AUTOSCALING_BRANCH"] = args.branch_id
    os.environ.pop("LAKEBASE_AUTOSCALING_ENDPOINT", None)

    import uvicorn
    from fastapi import FastAPI

    from agent_server import webapp

    app = FastAPI(title=f"Support Copilot preview — {args.branch_id}")
    app.include_router(webapp.router)
    app.include_router(webapp.ui)
    print(f"  Dashboard on Lakebase branch '{args.branch_id}': http://localhost:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
