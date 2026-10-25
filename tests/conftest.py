"""Fixtures for tests that run against a REAL Lakebase branch (CI: ci-pr-<N>, local: dev-<you>).

The branch is a copy of production, so these tests see production-shaped rows — not an empty
schema. Writes go through `tx`, which always rolls back.

    LAKEBASE_AUTOSCALING_BRANCH=dev-test uv run pytest tests -n0
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from agent_server import db  # noqa: E402

BRANCH = os.environ.get("LAKEBASE_AUTOSCALING_BRANCH", "production")


@pytest.fixture(scope="session")
def branch() -> str:
    return BRANCH


@pytest.fixture
def tx():
    """A cursor inside a transaction that is ALWAYS rolled back."""
    with db._get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(f"SET search_path = {db._SEARCH_PATH}")
        with conn.transaction():
            yield cur
            raise psycopg.Rollback()


@pytest.fixture(scope="session")
def api():
    """The dashboard /api routes on a bare FastAPI app — no agent / LLM needed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_server import webapp

    app = FastAPI()
    app.include_router(webapp.router)
    return TestClient(app)
