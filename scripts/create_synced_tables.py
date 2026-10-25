"""Create the UC->Lakebase synced tables via the SDK (notebook Step 6 pattern), idempotent.

Replaces the DABs `synced_database_tables` resource so we control the sync explicitly:
  - kb_articles : TRIGGERED, embedding -> vector(1024) (type_overrides), body_tsv tsvector
                  STORED-generated from `content` (extra_columns).
  - customer_360: TRIGGERED, plain analytics (no overrides).

The Postgres target schema == the schema part of synced_table_id, so we place the synced tables'
UC metadata in ${uc_catalog}.${synced_schema} -> they land in the `synced_schema` Postgres schema.

PREREQUISITES (Phase A step 1, on a warehouse): the Delta sources exist AND have Change Data Feed
enabled (TRIGGERED sync requires CDF) — build_kb.sql / 00_seed_kb_raw.sql do the ALTER.

    uv run python scripts/create_synced_tables.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.errors import NotFound  # noqa: E402
from databricks.sdk.service.postgres import (  # noqa: E402
    SyncedTable,
    SyncedTableSyncedTableSpec,
    SyncedTableSyncedTableSpecExtraColumn,
    SyncedTableSyncedTableSpecExtraColumnMaintenance,
    SyncedTableSyncedTableSpecPgSpecificType,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy,
    SyncedTableSyncedTableSpecTypeOverride,
)

UC_CATALOG = os.getenv("UC_CATALOG", "main")
UC_SCHEMA = os.getenv("UC_SCHEMA", "customer_support")                      # source Delta schema
SYNCED_SCHEMA = os.getenv("LAKEBASE_SYNCED_SCHEMA", "kb_synced")      # target Postgres (+UC) schema
PROJECT = os.getenv("LAKEBASE_AUTOSCALING_PROJECT", "lakebase-customer-support")
BRANCH = os.getenv("LAKEBASE_AUTOSCALING_BRANCH", "production")
DB = os.getenv("LAKEBASE_DATABASE", "databricks_postgres")         # logical Postgres db
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "1024"))
TRIGGERED = SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.TRIGGERED

GREEN, YELLOW, RESET = "\033[32m", "\033[33m", "\033[0m"


def _ensure_target_schema(w: WorkspaceClient) -> None:
    """The synced_table_id lives in a UC schema whose name == the Postgres target schema."""
    try:
        w.schemas.create(name=SYNCED_SCHEMA, catalog_name=UC_CATALOG)
        print(f"  {GREEN}✓{RESET} UC schema {UC_CATALOG}.{SYNCED_SCHEMA} created")
    except Exception as e:  # noqa: BLE001
        if "already exists" in str(e).lower():
            print(f"  {GREEN}✓{RESET} UC schema {UC_CATALOG}.{SYNCED_SCHEMA} exists")
        else:
            print(f"  {YELLOW}⚠{RESET} could not create UC schema {UC_CATALOG}.{SYNCED_SCHEMA}: {e}")


def _wait_online(w: WorkspaceClient, synced_id: str, timeout: int = 900) -> str:
    """Poll until the synced table's initial sync has landed data in Postgres. create/op.wait()
    only creates the object; the DLT pipeline that copies the data runs asynchronously."""
    start = time.time()
    while True:
        s = w.postgres.get_synced_table(name=f"synced_tables/{synced_id}")
        ds = s.status.detailed_state if s.status else None
        name = getattr(ds, "value", None) or str(ds)
        if "ONLINE" in name:                     # ONLINE / ONLINE_NO_PENDING_UPDATE / ...
            return name
        if "FAILED" in name or "OFFLINE" in name:
            raise RuntimeError(f"sync failed for {synced_id}: {name} — {getattr(s.status, 'message', '')}")
        if time.time() - start > timeout:
            raise TimeoutError(f"{synced_id} not ONLINE after {timeout}s (state={name})")
        print(f"    … {synced_id.split('.')[-1]}: {name}")
        time.sleep(10)


def _create(w: WorkspaceClient, table: str, pk: list[str], spec_extra: dict) -> None:
    source = f"{UC_CATALOG}.{UC_SCHEMA}.{table}"
    synced_id = f"{UC_CATALOG}.{SYNCED_SCHEMA}.{table}"
    try:
        w.postgres.get_synced_table(name=f"synced_tables/{synced_id}")
        print(f"  {GREEN}✓{RESET} synced table exists: {synced_id} (waiting for data to land)")
    except NotFound:
        print(f"  creating synced table {synced_id}  (source {source}, TRIGGERED)")
        op = w.postgres.create_synced_table(
            synced_table=SyncedTable(
                spec=SyncedTableSyncedTableSpec(
                    source_table_full_name=source,
                    branch=f"projects/{PROJECT}/branches/{BRANCH}",
                    primary_key_columns=pk,
                    scheduling_policy=TRIGGERED,
                    postgres_database=DB,
                    create_database_objects_if_missing=True,
                    **spec_extra,
                )
            ),
            synced_table_id=synced_id,
        )
        op.wait()
    state = _wait_online(w, synced_id)
    print(f"  {GREEN}✓{RESET} online (data landed): {synced_id} ({state})")


def main() -> int:
    w = WorkspaceClient()
    print(f"\n{GREEN}Creating synced tables{RESET} {UC_CATALOG}.{UC_SCHEMA} -> Postgres {SYNCED_SCHEMA} (branch {BRANCH})\n")
    _ensure_target_schema(w)

    # kb_articles: vector mapping + generated tsvector for BM25.
    _create(
        w,
        "kb_articles",
        ["id"],
        {
            "type_overrides": [
                SyncedTableSyncedTableSpecTypeOverride(
                    column_name="embedding",
                    pg_type=SyncedTableSyncedTableSpecPgSpecificType.PG_SPECIFIC_TYPE_VECTOR,
                    size=EMBEDDING_DIMS,
                )
            ],
            "extra_columns": [
                SyncedTableSyncedTableSpecExtraColumn(
                    column_name="body_tsv",
                    column_type="tsvector",
                    compute="to_tsvector('english', content)",
                    maintenance=SyncedTableSyncedTableSpecExtraColumnMaintenance.STORED_GENERATED,
                )
            ],
        },
    )

    # customer_360: plain analytics.
    _create(w, "customer_360", ["customer_id"], {})

    print(f"\n{GREEN}▶ Synced tables ready.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
