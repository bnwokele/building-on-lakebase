"""Render a .sql template: replace {{token}} with config values from the environment / .env.

One source of truth for identifiers (schemas, KB table, BM25 index, catalog) so the SQL isn't
hardcoded. Used two ways:
  • deploy_seed.py imports render() to fill the Lakebase SQL (sql/01-03) before running it.
  • You run it to prep the lakehouse SQL for Databricks SQL:
        uv run python scripts/render_sql.py lakehouse/build_kb.sql       # -> runnable SQL on stdout

Tokens: {{schema}} {{synced_schema}} {{uc_catalog}} {{uc_schema}} {{embedding_endpoint}}
(Table/index names like kb_articles / kb_body_bm25 are fixed by the data model — hardcoded in the SQL.)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def config() -> dict[str, str]:
    """The template values, all env-driven with the current-demo defaults."""
    return {
        "schema": os.getenv("LAKEBASE_SCHEMA", "customer_support"),
        "synced_schema": os.getenv("LAKEBASE_SYNCED_SCHEMA", "kb_synced"),
        "uc_catalog": os.getenv("UC_CATALOG", "main"),
        "uc_schema": os.getenv("UC_SCHEMA", "customer_support"),
        "embedding_endpoint": os.getenv("DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en"),
    }


def render(text: str, cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or config()

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in cfg:
            raise KeyError(f"unknown SQL template token {{{{{key}}}}} — add it to render_sql.config()")
        return cfg[key]

    return _TOKEN.sub(sub, text)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: render_sql.py <file.sql>   (prints rendered SQL to stdout)", file=sys.stderr)
        return 2
    print(render(Path(sys.argv[1]).read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
