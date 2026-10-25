"""Smoke-check a deployed app's dashboard API (Ep3 deploy + demo reset).

    uv run python scripts/smoke_app.py --app support-copilot-ep3                 # just reachable
    uv run python scripts/smoke_app.py --app support-copilot-ep3 --expect urgent # feature live
    uv run python scripts/smoke_app.py --app support-copilot-ep3 --expect high   # "before" state

Prints ticket #482 as the app serves it. Auth: whatever the Databricks SDK resolves (your CLI
profile locally, the CI service principal in GitHub Actions).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from databricks.sdk import WorkspaceClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--expect", help="expected priority of ticket #482")
    ap.add_argument("--retries", type=int, default=10)
    args = ap.parse_args()

    w = WorkspaceClient()
    try:
        url = w.apps.get(args.app).url.rstrip("/") + "/api/tickets?customer_id=1"
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ app '{args.app}': {e}", file=sys.stderr)
        return 1
    for attempt in range(1, args.retries + 1):
        try:
            r = requests.get(url, headers=w.config.authenticate(), timeout=30)
            if r.ok:
                break
            print(f"  attempt {attempt}: HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  attempt {attempt}: {e}")
        time.sleep(15)
    else:
        print(f"  ✗ {url} not healthy", file=sys.stderr)
        return 1

    t = next((t for t in r.json() if t["id"] == 482), None)
    print(f"  #482 via {args.app}: {json.dumps(t, default=str)}")
    if args.expect and (t is None or t["priority"] != args.expect):
        print(f"  ✗ expected priority '{args.expect}'", file=sys.stderr)
        return 1
    print("  ✓ app smoke check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
