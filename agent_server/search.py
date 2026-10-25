"""Hybrid Lakebase Search (vector + BM25, RRF) over the synced KB table.

Verified against the Lakebase Search docs. `kb_articles` resolves via search_path to
LAKEBASE_SCHEMA (same as the operational tables). Requires the synced KB table + the
lakebase_ann / lakebase_bm25 indexes (sql/02_kb_search.sql).
"""
import os

from agent_server import db

# KB table (kb_articles) + BM25 index (kb_body_bm25) are fixed by the data model — hardcoded.
# The table is UNqualified so it resolves via db.py's search_path (schemas ARE configurable);
# 'kb_body_bm25' is the string to_bm25query() needs, resolved on the same search_path.
_HYBRID = """
WITH vector_ranked AS (
  SELECT id, RANK() OVER (ORDER BY dist) AS rank
  FROM (SELECT id, embedding <=> %(qvec)s::vector AS dist
        FROM kb_articles ORDER BY dist LIMIT 40) v),
keyword_ranked AS (
  SELECT id, RANK() OVER (ORDER BY score) AS rank
  FROM (SELECT id, body_tsv <@> to_bm25query(to_tsvector('english', %(q)s), 'kb_body_bm25') AS score
        FROM kb_articles ORDER BY score LIMIT 40) k)
SELECT d.id, d.title, d.content,
       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0) AS rrf_score
FROM kb_articles d
LEFT JOIN vector_ranked v ON d.id = v.id
LEFT JOIN keyword_ranked k ON d.id = k.id
WHERE v.id IS NOT NULL OR k.id IS NOT NULL
ORDER BY rrf_score DESC, d.id
LIMIT %(limit)s;
"""


def embed(text: str) -> list[float]:
    from mlflow.deployments import get_deploy_client

    resp = get_deploy_client("databricks").predict(
        endpoint=os.environ.get("DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en"),
        inputs={"input": text},
    )
    return resp["data"][0]["embedding"]


def hybrid_search_kb(query_text: str, limit: int = 5) -> list[dict]:
    # Format as a pgvector string literal ("[...]") — the embedding endpoint can return a mix
    # of int/float, which psycopg refuses to adapt as a raw Python list.
    qvec = "[" + ",".join(f"{float(x)}" for x in embed(query_text)) + "]"
    return db.query(_HYBRID, {"qvec": qvec, "q": query_text, "limit": limit})
