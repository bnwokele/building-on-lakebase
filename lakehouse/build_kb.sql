-- Embeddings are created HERE, in the lakehouse, then ride the synced table into Lakebase
-- as a real vector column. Lakebase does not generate embeddings.
--
-- PREREQUISITE: run 00_seed_kb_raw.sql first — it creates {{uc_catalog}}.{{uc_schema}}.kb_raw
-- (the raw source read below) and .customer_360 (synced directly, no embedding).

CREATE OR REPLACE TABLE {{uc_catalog}}.{{uc_schema}}.kb_articles AS
SELECT
  id,
  title,
  content,
  ai_query('{{embedding_endpoint}}', content) AS embedding   -- ARRAY<FLOAT>, dim 1024
FROM {{uc_catalog}}.{{uc_schema}}.kb_raw;

-- Enable Change Data Feed — required for the TRIGGERED synced table (create_synced_tables.py).
ALTER TABLE {{uc_catalog}}.{{uc_schema}}.kb_articles
  SET TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- customer_360 (plain analytics, no embedding) is created and seeded in 00_seed_kb_raw.sql.

-- After these tables exist, the synced_database_tables in databricks.yml map
-- kb_articles.embedding (ARRAY<FLOAT>) -> vector(1024) via PG_SPECIFIC_TYPE_VECTOR.
