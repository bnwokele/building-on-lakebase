# Deploying the Customer Support Copilot

This app deploys as a **Databricks App** via a **Databricks Asset Bundle** (`databricks.yml`). The
app connects to **your** Lakebase project for operational data and uses **Databricks managed
sessions + memory** for state. All workspace-specific values are bundle variables — override them
without editing files.

## 1. Prerequisites

- A Databricks workspace with **Lakebase**, **Model Serving** (an LLM + an embedding endpoint), and
  **managed sessions/memory** available.
- The **Databricks CLI**, authenticated: `databricks auth login --host <your-workspace-host>`.
- [`uv`](https://docs.astral.sh/uv/) installed locally.
- A **Lakebase project + branch** (autoscaling). Note its project id and branch.
- **Lakebase Search enabled on that project.** Hybrid KB search uses the `lakebase_ann` (vector) and
  `lakebase_bm25` (keyword) indexes, which come from two Postgres extensions: **`lakebase_vector`**
  and **`lakebase_text`**. Enabling Lakebase Search = installing those two extensions. Two equivalent
  ways:
  - **Project settings** (**Project → Settings → Lakebase Search**) — the toggle installs both
    extensions for you.
  - **SQL** — install them directly on the project:
    ```sql
    CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE;  -- CASCADE pulls in the vector/pgvector dependency
    CREATE EXTENSION IF NOT EXISTS lakebase_text;
    ```

  Do this *before* building the KB indexes (step 5); otherwise `sql/02_kb_search.sql` fails because
  the index types (`lakebase_ann` / `lakebase_bm25`) don't exist. (Enabling via the setting restarts
  the project's compute and can't be undone, so do it once up front.)

## 2. Configure

```bash
cp .env.example .env
```
Fill in your CLI profile, Lakebase project/branch, Unity Catalog catalog/schema, and model-serving
endpoints. The bundle reads its defaults from `databricks.yml`; override any at deploy time, e.g.:

```bash
databricks bundle deploy \
  --var app_name=customer-support-copilot \
  --var lakebase_project=<your-project> \
  --var uc_catalog=<your-catalog>
```

## 3. Build the knowledge-base sources (lakehouse)

The KB and customer profile originate as Delta tables in Unity Catalog; embeddings are computed
upstream (via `ai_query()`), so this step needs a **SQL warehouse**. The lakehouse SQL uses
`{{placeholder}}` identifiers filled from your `.env` config.

`run_lakehouse_sql.py` renders *and* runs each file on the warehouse you pass — no copy/paste, no
separate render step. Pass the two template files **in order** (`00_seed_kb_raw.sql` →
`build_kb.sql`). Get a warehouse id with `databricks warehouses list`:

```bash
uv run python scripts/run_lakehouse_sql.py --warehouse-id <your-warehouse-id> \
  lakehouse/00_seed_kb_raw.sql lakehouse/build_kb.sql
```

## 4. Create the synced tables

Replicate the UC sources into Lakebase (TRIGGERED sync; maps `embedding → vector`, adds a generated
tsvector for BM25):

```bash
uv run python scripts/create_synced_tables.py
```

## 5. Create + seed the operational tables

Run as **you** (project/branch come from `.env`) — no app or service principal needed yet. This
creates the operational schema + tables + seed data and builds the KB hybrid-search indexes on the
synced `kb_articles` (needs **Lakebase Search** enabled — see step 1):

```bash
uv run python scripts/deploy_seed.py --phase data
```

`--phase data` = operational tables + KB indexes only, **no grant**. The service-principal grant
happens in step 6, after the app's SP exists.

## 6. Deploy the app (+ grant the service principal)

The bundle takes its values from `--var` flags, **not** `.env`. `scripts/deploy.sh` bridges them: it
loads `.env`, maps each value to the matching bundle variable (`LAKEBASE_SCHEMA` →
`operational_schema`, `LAKEBASE_AUTOSCALING_PROJECT` → `lakebase_project`, …), then runs
`bundle validate → deploy → run` and grants the app's service principal on your Lakebase schemas.
Because the vars come from `.env`, the deployed app points at the **same** project/schemas you built
in steps 3–5.

Since the lakehouse, synced tables, and operational seed are already done, deploy just the app:

```bash
# default dev target:
./scripts/deploy.sh --app-only

# …or an isolated target (recommended if you already run an app in dev — keeps separate state/app):
./scripts/deploy.sh --app-only --target test
```

`--target` defaults to `dev`; each target has its own deployment state and app. Use a separate target
(e.g. `test`) to keep a test deploy from touching an existing `dev` deployment.

This creates the app (with its own **service principal**), binds the **`postgres`** (Lakebase) and
**`experiment`** resources, uploads the code, injects `LAKEBASE_AUTOSCALING_ENDPOINT`, and grants the
app SP on your Lakebase schemas: **full access** (USAGE/CREATE + table DML + sequences) on the
**operational** schema (the app owns tickets/customers/notes and needs CREATE for bootstrap
migrations), and **read-only** (USAGE + SELECT) on the **synced** schema (the sync pipeline owns
those tables — the app only reads the KB). The synced grant revokes any writes first, so it's
idempotent and safe to re-run.

> `scripts/deploy_all.sh` runs the whole pipeline (lakehouse → synced → seed → deploy → grant) end to
> end in one command, discovering a warehouse automatically.

## 7. Managed sessions + memory

Nothing to provision — the memory store and session store are **auto-created by the app on first
use** (names come from `MANAGED_MEMORY_STORE` / `MANAGED_SESSION_STORE`). Databricks provisions and
runs the backing database; the app reaches it only over REST.

## 8. Verify

```bash
uv run pytest scripts/test_app_e2e.py -v -n0 \
  --agent-url https://<your-app>-....databricksapps.com \
  --token "$(databricks auth token -p <profile> | jq -r .access_token)"
```
The suite checks the operational API, the agent tools, managed session recall, managed-memory
save/recall/update, and the propose→confirm flow.

## Troubleshooting

- **`access method "lakebase_ann"/"lakebase_bm25" does not exist`** (or the extensions can't be
  created) — Lakebase Search isn't enabled on the project. Enable it (**Project → Settings → Lakebase
  Search**), which installs `lakebase_vector` + `lakebase_text`, then re-run the KB index step.
- **`type vector does not exist`** — the connection's `search_path` doesn't include the schema
  holding the pgvector `vector` type. On most projects it's in `public`, which `LAKEBASE_SEARCH_PATH`
  already includes; add the schema that has it if yours differs.
- **503 / "service principal may not have access"** — run step 6 to grant the app's service
  principal on the Lakebase schemas (do it after the app's SP role exists; open the app once if
  needed).
- **App build fails installing packages** — package downloads can time out transiently; re-run the
  deploy. `app.yaml` already raises `UV_HTTP_TIMEOUT` and serializes downloads to help with large
  wheels.
- **`bundle deploy` panics / segfaults (`ResourceApp.OverrideChangeDesc` nil pointer)** — the CLI's
  new default **"direct" deployment engine** (v1.17+) crashes while diffing this `apps` resource on
  any *change* (a fresh create plans fine; every re-deploy/update crashes). `databricks.yml` pins
  `engine: terraform` to avoid it — keep that line until the CLI bug is fixed. Note: an existing
  *direct*-engine deployment **state** overrides the `engine` setting (the CLI warns and keeps using
  the state's engine), so to switch an already-deployed target back to terraform you need a fresh
  state (a new target, or `databricks bundle destroy` first).
