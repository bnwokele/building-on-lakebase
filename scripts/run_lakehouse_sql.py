"""Run the lakehouse Delta-build SQL against a SQL warehouse (Statement Execution API).

This is the ONE part of the KB pipeline that needs a warehouse — embeddings are computed here
via ai_query(), in the lakehouse. Renders each file (render_sql, so identifiers come from config)
and runs its statements on the given warehouse. Fully qualified 3-part names, so no catalog/schema
needs to be set on the warehouse.

    uv run python scripts/run_lakehouse_sql.py --warehouse-id <id> \
        lakehouse/00_seed_kb_raw.sql lakehouse/build_kb.sql
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from render_sql import render  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.sql import StatementState  # noqa: E402

_DONE = {StatementState.SUCCEEDED, StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED}


def split_statements(sql: str) -> list[str]:
    """Strip line comments FIRST (a ';' can appear inside a `-- comment`), then split on ';'.
    Our lakehouse SQL has no block comments and no '--' or ';' inside string literals."""
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def run_file(w: WorkspaceClient, warehouse_id: str, path: str) -> None:
    print(f"▶ {path}")
    for i, stmt in enumerate(split_statements(render(Path(path).read_text())), 1):
        r = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=stmt, wait_timeout="30s"
        )
        # ai_query over the KB can exceed the initial wait — poll until terminal.
        while r.status and r.status.state not in _DONE:
            time.sleep(2)
            r = w.statement_execution.get_statement(r.statement_id)
        state = r.status.state if r.status else None
        if state == StatementState.SUCCEEDED:
            print(f"  ✓ stmt {i}")
        else:
            err = r.status.error.message if (r.status and r.status.error) else state
            print(f"  ✗ stmt {i}: {err}", file=sys.stderr)
            raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run lakehouse SQL files on a SQL warehouse.")
    ap.add_argument("--warehouse-id", required=True, help="SQL warehouse id")
    ap.add_argument("files", nargs="+", help="SQL files to render + run, in order")
    args = ap.parse_args()

    w = WorkspaceClient()
    for f in args.files:
        run_file(w, args.warehouse_id, f)
    print("▶ lakehouse build complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
