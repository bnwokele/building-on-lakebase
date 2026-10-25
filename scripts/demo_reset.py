"""Reset Episode 3 to its "before" state so the demo can be rehearsed / recorded again.

    uv run python scripts/demo_reset.py --all          # DRY RUN: show what would happen
    uv run python scripts/demo_reset.py --all --yes    # do it
    uv run python scripts/demo_reset.py --db --yes     # or pick parts: --branches --db --git --app

  --branches  delete every ci-pr-* and dev-* Lakebase branch (never `production`)
  --db        on production: undo migration 002 (scripts/demo/undo_002.sql) and re-seed the demo
              tickets/notes (sql/reset.sql + sql/migrations/001_baseline.sql)
  --git       close the open demo PR, delete feature/priority-escalation, and reset `main` to the
              `ep3-start` tag (a FORCE push — you are asked to type 'main' to confirm)
  --app       redeploy the prod app from `ep3-start` (no priority badge) with your CLI profile

Ends with a check: no ci-pr-* branches, no sla_hours column, #482 is `high`, app shows `high`.
Then re-run the demo:  git checkout -b feature/priority-escalation ep3-feature-broken && git push -u origin HEAD
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
# --db targets production (DEMO_DB_BRANCH=dev-<you> to rehearse the reset on a branch).
os.environ["LAKEBASE_AUTOSCALING_BRANCH"] = os.getenv("DEMO_DB_BRANCH", "production")
sys.path.insert(0, str(ROOT))

from databricks.sdk import WorkspaceClient  # noqa: E402

from agent_server import db  # noqa: E402

FEATURE_BRANCH = "feature/priority-escalation"
START_TAG = "ep3-start"
PROD_APP = os.getenv("PROD_APP_NAME", "support-copilot-ep3")
MIGRATION_ROLE = os.getenv("MIGRATION_ROLE", "support_owner")
CI_SP_NAME = "building-on-lakebase-ci"


def run(cmd: list[str], dry: bool, cwd: Path = ROOT, check: bool = True) -> None:
    print(f"    $ {' '.join(cmd)}")
    if not dry:
        subprocess.run(cmd, cwd=cwd, check=check)


def out(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True).stdout.strip()


def reset_branches(dry: bool) -> None:
    print("\n▸ Lakebase branches")
    run([sys.executable, "scripts/branch.py", "list"], dry=False)
    run([sys.executable, "scripts/branch.py", "sweep", "--include-dev"], dry)


def reset_db(dry: bool) -> None:
    print(f"\n▸ database (branch {os.environ['LAKEBASE_AUTOSCALING_BRANCH']})")
    applied = [r["version"] for r in db.query("SELECT version FROM schema_migrations")]
    print(f"    applied migrations: {', '.join(sorted(applied))}")
    steps = []
    if "002" in applied:
        steps.append(ROOT / "scripts" / "demo" / "undo_002.sql")
    steps += [ROOT / "sql" / "reset.sql", ROOT / "sql" / "migrations" / "001_baseline.sql"]
    for f in steps:
        print(f"    run {f.relative_to(ROOT)}")
    if dry:
        return
    with db._get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f'SET ROLE "{MIGRATION_ROLE}"')
        try:
            cur.execute(f"SET search_path = {db._SEARCH_PATH}")
            with conn.transaction():
                for f in steps:
                    cur.execute(f.read_text())
        finally:
            cur.execute("RESET ROLE")
    print("    ✓ schema + demo data reset")


def reset_git(dry: bool) -> None:
    print("\n▸ git / GitHub")
    for pr in out(["gh", "pr", "list", "--head", FEATURE_BRANCH, "--state", "open", "--json", "number", "--jq", ".[].number"]).split():
        run(["gh", "pr", "close", pr, "--comment", "Demo reset"], dry)
    if out(["git", "ls-remote", "--heads", "origin", FEATURE_BRANCH]):
        run(["git", "push", "origin", "--delete", FEATURE_BRANCH], dry)
    if out(["git", "branch", "--list", FEATURE_BRANCH]):
        run(["git", "checkout", "main"], dry)
        run(["git", "branch", "-D", FEATURE_BRANCH], dry)

    run(["git", "fetch", "origin", "main", "--tags"], dry=False)
    start, remote = out(["git", "rev-parse", f"{START_TAG}^{{commit}}"]), out(["git", "rev-parse", "origin/main"])
    if start == remote:
        print(f"    ✓ origin/main already at {START_TAG}")
        return
    print(f"    origin/main will be FORCE-reset to {START_TAG}. Commits that will be dropped:")
    print("      " + out(["git", "log", "--oneline", f"{START_TAG}..origin/main"]).replace("\n", "\n      "))
    if dry:
        print(f"    $ git push --force-with-lease=main:{remote} origin {START_TAG}^{{commit}}:refs/heads/main")
        return
    if input("    Type 'main' to confirm the force push: ").strip() != "main":
        sys.exit("    aborted")
    run(["git", "push", f"--force-with-lease=main:{remote}", "origin", f"{START_TAG}^{{commit}}:refs/heads/main"], dry)
    run(["git", "checkout", "main"], dry)
    run(["git", "reset", "--hard", START_TAG], dry)


def reset_app(dry: bool) -> None:
    print(f"\n▸ app '{PROD_APP}' (deploy from {START_TAG})")
    w = WorkspaceClient()
    sp = next(iter(w.service_principals.list(filter=f'displayName eq "{CI_SP_NAME}"')), None)
    env = {
        "BUNDLE_VAR_owner_user": w.current_user.me().user_name,
        "BUNDLE_VAR_ci_service_principal": sp.application_id if sp else "",
        "BUNDLE_VAR_app_name": PROD_APP,
        "BUNDLE_VAR_lakebase_project": os.getenv("LAKEBASE_AUTOSCALING_PROJECT", ""),
        "BUNDLE_VAR_uc_catalog": os.getenv("UC_CATALOG", ""),
    }
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "ep3-start"
        run(["git", "worktree", "add", "--detach", str(wt), START_TAG], dry)
        try:
            print("    (with " + ", ".join(f"{k}={v}" for k, v in env.items()) + ")")
            if not dry:
                os.environ.update({k: v for k, v in env.items() if v})
            run(["databricks", "bundle", "deploy", "-t", "prod"], dry, cwd=wt)
            run(["databricks", "bundle", "run", "support_copilot", "-t", "prod"], dry, cwd=wt)
        finally:
            run(["git", "worktree", "remove", "--force", str(wt)], dry, check=False)


def check() -> int:
    print("\n▸ check")
    ok = True
    leftovers = [b for b in out([sys.executable, "scripts/branch.py", "list"]).split("\n") if "ci-pr-" in b]
    ok &= not leftovers
    print(f"    {'✓' if not leftovers else '✗'} no ci-pr-* branches {leftovers or ''}")
    cols = [r["column_name"] for r in db.query(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'tickets'",
        (db.SCHEMA,),
    )]
    ok &= "sla_hours" not in cols
    print(f"    {'✓' if 'sla_hours' not in cols else '✗'} tickets has no sla_hours column")
    (t,) = db.query("SELECT priority FROM tickets WHERE id = 482")
    ok &= t["priority"] == "high"
    print(f"    {'✓' if t['priority'] == 'high' else '✗'} #482 priority is '{t['priority']}'")
    app_ok = subprocess.run(
        [sys.executable, "scripts/smoke_app.py", "--app", PROD_APP, "--expect", "high", "--retries", "2"], cwd=ROOT
    ).returncode == 0
    ok &= app_ok
    print(f"\n{'✓ ready to record' if ok else '✗ not fully reset — see above'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    for part in ("branches", "db", "git", "app"):
        ap.add_argument(f"--{part}", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--yes", action="store_true", help="actually do it (default is a dry run)")
    ap.add_argument("--check", action="store_true", help="only run the final check")
    args = ap.parse_args()
    if args.check:
        return check()

    dry = not args.yes
    if dry:
        print("DRY RUN — nothing will change. Re-run with --yes to apply.")
    parts = [p for p in ("git", "branches", "db", "app") if args.all or getattr(args, p)]
    if not parts:
        ap.error("pick --all or at least one of --branches --db --git --app")
    for p in parts:
        {"git": reset_git, "branches": reset_branches, "db": reset_db, "app": reset_app}[p](dry)
    return 0 if dry else check()


if __name__ == "__main__":
    raise SystemExit(main())
