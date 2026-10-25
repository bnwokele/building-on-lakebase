#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Deploy the MANAGED Support Copilot (managed sessions + memory) end to end:
#
#   0  ensure Lakebase project/branch/endpoint
#   A1 build lakehouse Delta sources (kb_raw, customer_360, kb_articles + ai_query embeddings)
#   A2 create UC->Lakebase synced tables (kb_articles, customer_360 -> $LAKEBASE_SYNCED_SCHEMA)
#   A3 operational tables + seed + KB hybrid-search indexes   (NO memory-index — memory is managed)
#   B  DAB: bundle validate -> deploy (experiment + app + postgres) -> run (build + start), with retry
#   C  grant the app service principal on Lakebase (customer_support / kb_synced / public)
#
# Managed memory + session stores are created automatically by the app SP on first use, in the
# Databricks-managed project `databricks-internal-custom-agents` — nothing to provision here.
#
# Config comes from .env (DATABRICKS_CONFIG_PROFILE, LAKEBASE_*, UC_*). Idempotent; safe to re-run.
#
#   ./scripts/deploy.sh --warehouse-id <id>            # full deploy incl. lakehouse build
#   ./scripts/deploy.sh                                # assume UC sources already exist
#   ./scripts/deploy.sh --app-only                     # just validate/deploy/run the app
#   ./scripts/deploy.sh --app-only --target test       # deploy to a specific bundle target (default: dev)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."                      # repo root

# ---- config: load .env, then flags override ----
# Parse .env line-by-line (do NOT `source` it): values may contain spaces/commas
# (e.g. LAKEBASE_SEARCH_PATH="a, b, c"), which `. ./.env` would word-split and try to run.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    val=${val%\"}; val=${val#\"}          # strip optional surrounding quotes
    export "$key=$val"
  done < .env
fi
PROFILE="${DATABRICKS_CONFIG_PROFILE:-}"
APP_NAME="${APP_NAME:-customer-support-copilot}"
APP_KEY="support_copilot"                    # resources.apps.<key> in databricks.yml
TARGET="dev"
WAREHOUSE_ID=""
APP_ONLY=false

while [[ $# -gt 0 ]]; do case "$1" in
  -p|--profile)     PROFILE="$2"; shift 2 ;;
  --app-name)       APP_NAME="$2"; shift 2 ;;
  -t|--target)      TARGET="$2"; shift 2 ;;
  --warehouse-id)   WAREHOUSE_ID="$2"; shift 2 ;;
  --app-only)       APP_ONLY=true; shift ;;
  *) echo "unknown arg: $1"; exit 2 ;;
esac; done

[ -n "$PROFILE" ] || { echo "ERROR: set DATABRICKS_CONFIG_PROFILE in .env or pass --profile"; exit 1; }
PF=(--profile "$PROFILE")
# Map .env values -> bundle variables. The names differ (.env uses LAKEBASE_SCHEMA, the bundle
# uses operational_schema), so map explicitly. Fed as BUNDLE_VAR_* env vars (which DABs reads
# natively) rather than --var flags: the CLI's --var treats commas as separators between
# assignments, which would split comma-bearing values like search_path. Only export when the .env
# value is present; anything unset falls back to the default in databricks.yml.
set_var(){ [ -n "${2:-}" ] && export "BUNDLE_VAR_$1=$2"; }
set_var lakebase_project   "${LAKEBASE_AUTOSCALING_PROJECT:-}"
set_var lakebase_branch    "${LAKEBASE_AUTOSCALING_BRANCH:-}"
set_var operational_schema "${LAKEBASE_SCHEMA:-}"
set_var synced_schema      "${LAKEBASE_SYNCED_SCHEMA:-}"
set_var search_path        "${LAKEBASE_SEARCH_PATH:-}"
set_var uc_catalog         "${UC_CATALOG:-}"
set_var uc_schema          "${UC_SCHEMA:-}"
set_var llm_endpoint       "${LLM_ENDPOINT:-}"
set_var embedding_endpoint "${DATABRICKS_EMBEDDING_ENDPOINT:-}"
set_var memory_store       "${MANAGED_MEMORY_STORE:-}"
set_var session_store      "${MANAGED_SESSION_STORE:-}"
# app_name stays a --var (no comma, keeps the array non-empty for `"${VAR[@]}"` under set -u).
VAR=(--var "app_name=$APP_NAME")
step(){ echo; echo "▶ $*"; }

echo "profile=$PROFILE  app=$APP_NAME  target=$TARGET  warehouse=${WAREHOUSE_ID:-<none>}"

# Ship no uv.lock: the Apps build runs a fresh resolve (a stale lock fails --locked). See DEPLOY notes.
rm -f uv.lock

if ! $APP_ONLY; then
  step "0/5  ensure Lakebase project + branch + endpoint"
  uv run python scripts/ensure_lakebase_project.py

  step "A1  build lakehouse Delta sources (embeddings via ai_query — needs a SQL warehouse)"
  if [ -n "$WAREHOUSE_ID" ]; then
    uv run python scripts/run_lakehouse_sql.py --warehouse-id "$WAREHOUSE_ID" \
      lakehouse/00_seed_kb_raw.sql lakehouse/build_kb.sql
  else
    echo "   (skipped — no --warehouse-id; assuming ${UC_CATALOG:-<uc>}.${UC_SCHEMA:-<schema>} sources already exist)"
  fi

  step "A2  create UC->Lakebase synced tables (TRIGGERED; embedding->vector, generated body_tsv)"
  uv run python scripts/create_synced_tables.py

  step "A3  operational tables + seed + KB search indexes"
  uv run python scripts/deploy_seed.py --app "$APP_NAME" --phase data --skip-grant
fi

step "B1  bundle validate"
databricks bundle validate -t "$TARGET" "${VAR[@]}" "${PF[@]}"

step "B2  bundle deploy (experiment + app + postgres resource)"
databricks bundle deploy -t "$TARGET" "${VAR[@]}" "${PF[@]}"

step "B3  bundle run (build + start) — retry transient package-mirror timeouts on large wheels"
n=0
until databricks bundle run "$APP_KEY" -t "$TARGET" "${VAR[@]}" "${PF[@]}"; do
  n=$((n+1))
  if [ "$n" -ge 5 ]; then
    echo "   build failed after $n attempts. Likely a package-mirror timeout on a large wheel"
    echo "   (pyarrow/scipy/botocore). Re-run this script — the mirror is intermittent."
    exit 1
  fi
  echo "   attempt $n failed (large-wheel mirror timeout?) — retrying in 15s..."
  sleep 15
done

step "C  grant the app service principal on Lakebase"
SP=$(databricks apps get "$APP_NAME" "${PF[@]}" -o json \
      | uv run python -c "import sys,json;print(json.load(sys.stdin).get('service_principal_client_id',''))")
[ -n "$SP" ] || { echo "   could not resolve app SP — re-run once the app exists"; exit 1; }
echo "   app SP: $SP"
uv run python - "$SP" <<'PY'
import os, sys
# .env values are already exported into the environment by this script's loader (and running a
# heredoc script via stdin breaks python-dotenv's find_dotenv), so read straight from os.environ.
from databricks_ai_bridge.lakebase import (
    LakebaseClient, SchemaPrivilege, SequencePrivilege, TablePrivilege,
)
sp = sys.argv[1]
op_schema_privs = [SchemaPrivilege.USAGE, SchemaPrivilege.CREATE]
op_table_privs  = [TablePrivilege.SELECT, TablePrivilege.INSERT, TablePrivilege.UPDATE, TablePrivilege.DELETE]
op_seq_privs    = [SequencePrivilege.USAGE, SequencePrivilege.SELECT, SequencePrivilege.UPDATE]
op_schema     = os.getenv("LAKEBASE_SCHEMA", "customer_support")
synced_schema = os.getenv("LAKEBASE_SYNCED_SCHEMA", "kb_synced")
with LakebaseClient(
    project=os.environ["LAKEBASE_AUTOSCALING_PROJECT"],
    branch=os.getenv("LAKEBASE_AUTOSCALING_BRANCH", "production"),
) as c:
    try:
        c.create_role(sp, "SERVICE_PRINCIPAL"); print(f"   SP role ready: {sp}")
    except Exception as e:  # noqa: BLE001
        print("   role:", "exists" if "exist" in str(e).lower() else e)
    # Operational schema — FULL access (the app owns tickets/customers/notes; CREATE for bootstrap).
    try:
        c.grant_schema(sp, op_schema_privs, [op_schema])
        c.grant_all_tables_in_schema(sp, op_table_privs, [op_schema])
        c.grant_all_sequences_in_schema(sp, op_seq_privs, [op_schema])
        print(f"   granted FULL access on operational schema '{op_schema}'")
    except Exception as e:  # noqa: BLE001
        print(f"   grant on operational '{op_schema}' skipped: {e}")
    # Synced schema — READ-ONLY (the sync pipeline owns these tables; the app only reads the KB).
    # Revoke writes/CREATE first so re-running enforces read-only (REVOKE of a non-grant is a no-op).
    try:
        c.execute(f'REVOKE CREATE ON SCHEMA "{synced_schema}" FROM "{sp}"')
        c.execute(f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA "{synced_schema}" FROM "{sp}"')
        c.grant_schema(sp, [SchemaPrivilege.USAGE], [synced_schema])
        c.grant_all_tables_in_schema(sp, [TablePrivilege.SELECT], [synced_schema])
        print(f"   granted READ-ONLY (USAGE + SELECT) on synced schema '{synced_schema}'")
    except Exception as e:  # noqa: BLE001
        print(f"   grant on synced '{synced_schema}' skipped: {e}")
PY

step "done"
URL=$(databricks apps get "$APP_NAME" "${PF[@]}" -o json \
       | uv run python -c "import sys,json;print(json.load(sys.stdin).get('url',''))")
echo "   app URL: $URL"
echo
echo "   Managed memory + session stores are auto-created by the app SP on first use, in the"
echo "   Databricks-managed project 'databricks-internal-custom-agents' (databases memory-<store> /"
echo "   session-<store>). There is NO memory table in your own Lakebase — that's expected."
