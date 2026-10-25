-- EP4 · Hybrid Lakebase Search over the synced KB table (kb_articles). Table/index names are
-- hardcoded (fixed by the data model); deploy_seed.py runs this against the operational schema.
-- Run AFTER the synced table has landed data (BM25 computes corpus statistics at build time,
-- so the rows must exist before the index is built).

CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE;   -- lakebase_ann index type
CREATE EXTENSION IF NOT EXISTS lakebase_text;             -- lakebase_bm25 index type

-- Vector side: the embedding arrived precomputed from the lakehouse (ai_query) and was
-- mapped to vector(1024) during sync. Cosine ANN via lakebase_ann.
CREATE INDEX IF NOT EXISTS kb_embedding_idx
  ON kb_articles USING lakebase_ann (embedding vector_cosine_ops);

-- Keyword side: the `body_tsv` tsvector column is created BY THE SYNC (databricks.yml
-- synced_database_tables.kb_articles.spec.extra_columns, STORED_GENERATED) — no manual ALTER
-- TABLE here. We only build the BM25 index (its NAME is what search.py passes to to_bm25query()).
CREATE INDEX IF NOT EXISTS kb_body_bm25
  ON kb_articles USING lakebase_bm25 (body_tsv);
