"""Create / delete / inspect Lakebase branches — one per pull request (Ep3).

A branch is a copy-on-write clone of `production`: schema AND data, ready in seconds, scales to
zero when idle. CI creates `ci-pr-<N>` when a PR opens, runs migrations + tests on it, and
deletes it when the PR closes. The same commands work on a laptop.

    uv run python scripts/branch.py create ci-pr-42        # fresh copy of production (idempotent)
    uv run python scripts/branch.py endpoint ci-pr-42      # print the READ_WRITE host
    uv run python scripts/branch.py delete ci-pr-42
    uv run python scripts/branch.py list
    uv run python scripts/branch.py sweep --keep 42 57     # delete ci-pr-* except open PRs

`create` always re-copies from the source branch (replace_existing), so every CI run starts from
current production data. A TTL (BRANCH_TTL_HOURS, default 24) is a safety net: if teardown never
runs, Lakebase expires the branch on its own.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.postgres import (  # noqa: E402
    Branch,
    BranchSpec,
    Endpoint,
    EndpointSpec,
    EndpointType,
)
from google.protobuf.duration_pb2 import Duration  # noqa: E402

PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT", "lakebase-customer-support")
SOURCE = os.getenv("LAKEBASE_SOURCE_BRANCH", "production")
TTL_HOURS = int(os.getenv("BRANCH_TTL_HOURS", "24"))
# Branches this tool is allowed to delete. `production` (or any other name) is never touched.
DELETABLE_PREFIXES = ("ci-pr-", "dev-")


def _project() -> str:
    return f"projects/{PROJECT}"


def _branch_path(branch_id: str) -> str:
    return f"{_project()}/branches/{branch_id}"


def _rw_endpoint(w: WorkspaceClient, branch_id: str):
    for ep in w.postgres.list_endpoints(parent=_branch_path(branch_id)):
        if ep.status and ep.status.endpoint_type == EndpointType.ENDPOINT_TYPE_READ_WRITE:
            return ep
    return None


def create(w: WorkspaceClient, branch_id: str) -> None:
    print(f"  Branching '{SOURCE}' -> '{branch_id}' (copy-on-write, TTL {TTL_HOURS}h)...")
    w.postgres.create_branch(
        parent=_project(),
        branch_id=branch_id,
        branch=Branch(
            spec=BranchSpec(
                source_branch=_branch_path(SOURCE),
                ttl=Duration(seconds=TTL_HOURS * 3600),
            )
        ),
        replace_existing=True,
    ).wait()
    print(f"  ✓ branch ready: {_branch_path(branch_id)}")

    # The app/tests connect through a READ_WRITE compute endpoint on the branch.
    ep = _rw_endpoint(w, branch_id)
    if ep is None:
        print("  Creating READ_WRITE endpoint...")
        ep = w.postgres.create_endpoint(
            parent=_branch_path(branch_id),
            endpoint_id="primary",
            endpoint=Endpoint(
                spec=EndpointSpec(
                    endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
                    autoscaling_limit_min_cu=0.5,
                    autoscaling_limit_max_cu=2.0,
                )
            ),
        ).wait()
    print(f"  ✓ endpoint: {ep.name} ({ep.status.hosts.host})")


def delete(w: WorkspaceClient, branch_id: str) -> None:
    if not branch_id.startswith(DELETABLE_PREFIXES):
        sys.exit(f"  refusing to delete '{branch_id}' — only {DELETABLE_PREFIXES} branches")
    try:
        w.postgres.get_branch(name=_branch_path(branch_id))
    except Exception:  # noqa: BLE001
        print(f"  ✓ '{branch_id}' already gone")
        return
    w.postgres.delete_branch(name=_branch_path(branch_id)).wait()
    print(f"  ✓ deleted {_branch_path(branch_id)}")


def endpoint(w: WorkspaceClient, branch_id: str) -> None:
    ep = _rw_endpoint(w, branch_id)
    if ep is None:
        sys.exit(f"  no READ_WRITE endpoint on '{branch_id}'")
    print(ep.status.hosts.host)


def list_branches(w: WorkspaceClient) -> list[str]:
    ids = []
    for b in w.postgres.list_branches(parent=_project()):
        bid = b.name.rsplit("/", 1)[-1]
        ids.append(bid)
        expires = b.status.expire_time.ToJsonString() if b.status and b.status.expire_time else "-"
        print(f"  {bid:<24} {b.status.current_state.value if b.status else '?':<10} expires {expires}")
    return ids


def sweep(w: WorkspaceClient, keep: list[str], prefixes: tuple[str, ...]) -> None:
    """Delete every branch with one of `prefixes` except ci-pr-<N> for N in `keep` (open PRs)."""
    keep_ids = {f"ci-pr-{n}" for n in keep}
    for b in w.postgres.list_branches(parent=_project()):
        bid = b.name.rsplit("/", 1)[-1]
        if bid.startswith(prefixes) and bid not in keep_ids:
            delete(w, bid)
    print(f"  ✓ sweep done (kept: {', '.join(sorted(keep_ids)) or 'none'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("action", choices=["create", "delete", "endpoint", "list", "sweep"])
    ap.add_argument("branch_id", nargs="?")
    ap.add_argument("--keep", nargs="*", default=[], help="sweep: open PR numbers to keep")
    ap.add_argument("--include-dev", action="store_true", help="sweep: also delete dev-* branches")
    args = ap.parse_args()
    if args.action not in ("list", "sweep") and not args.branch_id:
        ap.error("branch_id is required")

    w = WorkspaceClient()
    if args.action == "create":
        create(w, args.branch_id)
    elif args.action == "delete":
        delete(w, args.branch_id)
    elif args.action == "endpoint":
        endpoint(w, args.branch_id)
    elif args.action == "sweep":
        sweep(w, args.keep, DELETABLE_PREFIXES if args.include_dev else ("ci-pr-",))
    else:
        list_branches(w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
