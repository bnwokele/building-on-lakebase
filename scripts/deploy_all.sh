#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy_all.sh — ONE COMMAND, FULLY AUTOMATIC, END TO END.
#
# Wraps every step needed to stand this app up from nothing, with zero required flags:
#
#   preflight  verify Databricks auth + required .env config
#   discover   find a usable SQL warehouse automatically (for the embedding build) — or use --warehouse-id
#   ── then delegates the proven pipeline to scripts/deploy.sh (full, not --app-only): ──
#   0   ensure Lakebase project / branch / endpoint
#   A1  build lakehouse Delta sources   (kb_raw, customer_360, kb_articles + ai_query embeddings)
#   A2  create UC -> Lakebase SYNCED tables   (customer_360, kb_articles -> kb_synced)
#   A3  operational TABLES + SEED data + KB hybrid-search indexes
#   B   DAB: bundle validate -> deploy -> run (build + start), retrying transient mirror timeouts
#   C   grant the app service principal on Lakebase (customer_support / kb_synced / public)
#
# Managed session + memory stores (`csp`) auto-create on first use in the Databricks-managed project
# `databricks-internal-custom-agents` — nothing to provision here.
#
# Everything is read from .env; everything is idempotent and safe to re-run.
#
# Usage:
#   ./scripts/deploy_all.sh                          # fully automatic (discovers the warehouse)
#   ./scripts/deploy_all.sh --warehouse-id <id>      # pin a specific SQL warehouse
#   ./scripts/deploy_all.sh --app-name customer-support-copilot --profile <profile>
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."                      # repo root

# ---- config: load .env safely (do NOT `source`: values may contain spaces/commas) --------------
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    val=${val%\"}; val=${val#\"}
    export "$key=$val"
  done < .env
fi
PROFILE="${DATABRICKS_CONFIG_PROFILE:-}"
APP_NAME="${APP_NAME:-customer-support-copilot}"
WAREHOUSE_ID=""

while [[ $# -gt 0 ]]; do case "$1" in
  -p|--profile)   PROFILE="$2"; shift 2 ;;
  --app-name)     APP_NAME="$2"; shift 2 ;;
  --warehouse-id) WAREHOUSE_ID="$2"; shift 2 ;;
  -h|--help)      sed -n '2,36p' "$0"; exit 0 ;;
  *) echo "unknown arg: $1 (see --help)"; exit 2 ;;
esac; done

say(){ echo; echo "══ $* ══"; }
[ -n "$PROFILE" ] || { echo "ERROR: set DATABRICKS_CONFIG_PROFILE in .env or pass --profile"; exit 1; }
PF=(--profile "$PROFILE")

# ---- preflight: auth + required config ---------------------------------------------------------
say "preflight — auth + config"
if ! ME=$(databricks current-user me "${PF[@]}" -o json 2>/dev/null); then
  echo "ERROR: not authenticated for profile '$PROFILE'."
  echo "       Run:  databricks auth login --host <workspace-url> --profile $PROFILE"
  exit 1
fi
echo "   authenticated: $(echo "$ME" | uv run python -c "import sys,json;d=json.load(sys.stdin);print(d.get('userName') or d.get('user_name') or d.get('displayName'))" 2>/dev/null)"
: "${LAKEBASE_AUTOSCALING_PROJECT:?set LAKEBASE_AUTOSCALING_PROJECT in .env}"
: "${LAKEBASE_SCHEMA:?set LAKEBASE_SCHEMA in .env}"
: "${LAKEBASE_SYNCED_SCHEMA:?set LAKEBASE_SYNCED_SCHEMA in .env}"
echo "   profile=$PROFILE  app=$APP_NAME  project=$LAKEBASE_AUTOSCALING_PROJECT"
echo "   schemas: operational=$LAKEBASE_SCHEMA  synced=$LAKEBASE_SYNCED_SCHEMA"
echo "   UC source: ${UC_CATALOG:-<unset>}.${UC_SCHEMA:-<unset>}  (built by the lakehouse step)"

# ---- discover a SQL warehouse (for the ai_query embedding build) -------------------------------
# The lakehouse step computes embeddings via ai_query, which needs a Serverless/Pro SQL warehouse.
# If the caller didn't pin one, pick the best available: prefer RUNNING + serverless/Pro, else any.
if [ -z "$WAREHOUSE_ID" ]; then
  say "discover — picking a SQL warehouse automatically"
  WH_JSON=$(databricks warehouses list "${PF[@]}" -o json 2>/dev/null || echo '[]')
  WAREHOUSE_ID=$(echo "$WH_JSON" | uv run python -c '
import sys, json
raw = json.load(sys.stdin)
whs = raw.get("warehouses", raw) if isinstance(raw, dict) else raw
def rank(w):
    serverless = bool(w.get("enable_serverless_compute")) or (w.get("warehouse_type") == "PRO")
    running = (w.get("state") == "RUNNING")
    return (running, serverless)            # True sorts last → pick with max()
if not whs:
    sys.exit(0)
best = max(whs, key=rank)
sys.stderr.write(f"   chose: {best.get(\"name\")}  (id={best.get(\"id\")}, state={best.get(\"state\")}, type={best.get(\"warehouse_type\")})\n")
print(best.get("id",""))
' 2>&1 1>/tmp/_wid.$$ || true)
  echo "$WAREHOUSE_ID"                               # the stderr "chose:" line
  WAREHOUSE_ID=$(cat /tmp/_wid.$$ 2>/dev/null); rm -f /tmp/_wid.$$
  if [ -z "$WAREHOUSE_ID" ]; then
    echo "ERROR: no SQL warehouse found in this workspace. Create one (Serverless/Pro) or pass"
    echo "       --warehouse-id <id>. (The embedding build needs ai_query on a SQL warehouse.)"
    exit 1
  fi
else
  echo "   using pinned warehouse: $WAREHOUSE_ID"
fi

# ---- run the full pipeline (ensure -> lakehouse -> synced -> seed -> app -> grant) --------------
say "deploy — running the full end-to-end pipeline"
echo "   (delegating steps 0/A/B/C to scripts/deploy.sh with the discovered warehouse)"
exec bash scripts/deploy.sh --profile "$PROFILE" --app-name "$APP_NAME" --warehouse-id "$WAREHOUSE_ID"
