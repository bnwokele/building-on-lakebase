# Building on Lakebase

Code for the **Building on Lakebase** skill builder series. Every episode works on the same app: a
**Support Copilot** (an agent plus a tickets dashboard) on Databricks Apps, with **Lakebase** (managed
Postgres) as its database.

| Episode | Topic | In this repo |
|---|---|---|
| 1. Meet Lakebase | Projects, tables, synced tables | `sql/`, `lakehouse/`, `scripts/create_synced_tables.py` |
| 2. Anatomy of an AI App | Agent + dashboard on one Postgres | `agent_server/`. See **[APP.md](APP.md)** and **[DEPLOY.md](DEPLOY.md)** |
| **3. Branch, Test, and Ship** | **Every pull request gets its own database** | `scripts/branch.py`, `sql/migrations/`, `tests/`, `.github/workflows/` |
| 4. Operate and Recover | Monitoring, PITR, recovery | coming soon |

Tag `ep2-baseline` is the app as it was at the end of Episode 2.

---

## Episode 3: every pull request gets its own Lakebase database

A Lakebase **branch** is a copy-on-write clone of `production`: schema *and* data. It is ready in
seconds and scales to zero when idle. This repo uses a branch for every PR:

```
 PR opened / updated                       PR closed (merged or not)
 ───────────────────                       ─────────────────────────
 create  ci-pr-<N>  from production         delete ci-pr-<N>
 migrate ci-pr-<N>  (sql/migrations)
 pytest  against ci-pr-<N>  (real rows)    merged to main
 comment results on the PR                 ──────────────
                                           ⏸  approve `production` environment
                                           migrate production (the same files)
                                           databricks bundle deploy -t prod
```

Why this matters: the PR's schema change and app change are tried on a **real copy of production
data**, in isolation, before anything is merged. Production keeps serving the whole time, and the
branch is thrown away when the PR closes. Preview the app against the branch with
`scripts/preview_branch.py ci-pr-<N>`.

| Workflow | Trigger | What it does |
|---|---|---|
| [`pr-branch.yml`](.github/workflows/pr-branch.yml) | PR opened/updated | branch, migrate, test, PR comment |
| [`pr-cleanup.yml`](.github/workflows/pr-cleanup.yml) | PR closed | delete the PR's branch |
| [`deploy.yml`](.github/workflows/deploy.yml) | push to `main` | approval gate, migrate prod, bundle deploy, smoke test |
| [`branch-sweeper.yml`](.github/workflows/branch-sweeper.yml) | nightly | delete branches of closed PRs (PR branches also expire after 4h) |

### The same scripts run locally and in CI

```bash
cp .env.example .env            # fill in your profile / project
uv sync

uv run python scripts/branch.py create dev-$USER                          # your own copy of prod
LAKEBASE_AUTOSCALING_BRANCH=dev-$USER uv run python scripts/migrate.py    # apply migrations
LAKEBASE_AUTOSCALING_BRANCH=dev-$USER uv run pytest tests -n0             # test on real data
uv run python scripts/branch.py delete dev-$USER
```

`LAKEBASE_AUTOSCALING_BRANCH` is the only thing that changes between your laptop, a PR, and
production. The app's `db.py` connects to whichever branch it names.

### One-time setup (for your own copy)

Prerequisites: a workspace with Lakebase Autoscaling, the Episode 2 app's project and data (see
[DEPLOY.md](DEPLOY.md)), the `databricks` CLI, `uv`, and `gh`.

```bash
uv run python scripts/setup_ci.py --github-repo <you>/building-on-lakebase
```

This is idempotent. It:

1. Creates the CI **service principal** and gives it `CAN_MANAGE` on the Lakebase project, so it can create and delete branches.
2. Adds Postgres roles:
   - **`support_owner`** owns the operational tables. You and CI are members, so either of you can run migrations.
   - **`support_app`** holds the deployed app's privileges.

   Branches copy roles, so every `ci-pr-*` branch has them too.
3. Sets these on the GitHub repo:
   - Secrets: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`. The secret goes straight to `gh secret set` and is never printed.
   - Variables: the non-secret config.
   - A `production` **environment** with you as required reviewer.

> PRs from forks don't get repository secrets, so run the workflows in **your own** repo.

### Running the Episode 3 demo

```bash
uv run python scripts/demo_reset.py --all          # dry run: shows what would change
uv run python scripts/demo_reset.py --all --yes    # reset branches, prod schema/data, git, app

git checkout -b feature/priority-escalation ep3-feature
git push -u origin HEAD
gh pr create --fill                                # CI creates ci-pr-<N>, migrates, tests (~30 s)

uv run python scripts/preview_branch.py ci-pr-<N>  # http://localhost:8001: the feature on the branch
```

Then merge the PR and approve the `production` deployment: the same migration runs on production
and the app is redeployed. Tags: `ep3-start` (tooling, before the feature) and `ep3-feature` (the
feature: migration 002, the badge, and tests).

## Security

No credentials are in this repo. Local config lives in `.env`, which is git-ignored. Copy
[`.env.example`](.env.example) to create it. CI authenticates as a service principal through GitHub
secrets. Every database connection uses short-lived Lakebase OAuth tokens, with no passwords.
