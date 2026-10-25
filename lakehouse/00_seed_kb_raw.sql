-- Seeds the RAW sources that the synced tables are built from. Run this BEFORE build_kb.sql.
--   {{uc_catalog}}.{{uc_schema}}.kb_raw       -> (build_kb.sql adds embedding) -> synced to Lakebase
--   {{uc_catalog}}.{{uc_schema}}.customer_360 -> synced to Lakebase directly (analytics, no embedding)
-- Fictional, sanitized data. Topics match the UI chat chips (nightly sync, token rotation,
-- dashboard latency) and the operational seed (Acme Robotics = customer 1, Bluefin = 2).

CREATE SCHEMA IF NOT EXISTS {{uc_catalog}}.{{uc_schema}};

-- ---------------------------------------------------------------------------
-- Knowledge base — raw articles (no embedding yet; build_kb.sql adds it).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE {{uc_catalog}}.{{uc_schema}}.kb_raw (
  id      BIGINT,
  title   STRING,
  content STRING
);

INSERT INTO {{uc_catalog}}.{{uc_schema}}.kb_raw (id, title, content) VALUES
  (1, 'Nightly sync jobs fail around 2 AM',
      'Scheduled sync jobs that run in the 1:00-3:00 AM window can fail because they overlap the ' ||
      'platform maintenance window and because a long-lived service token may have expired. ' ||
      'Symptoms: the job errors out at ~2 AM with a connection or authorization failure. ' ||
      'Fix: reschedule the job to start after 3:15 AM, and rotate the service token if it is older ' ||
      'than 90 days. Confirm the next run completes cleanly before closing the ticket.'),
  (2, 'Rotating an expired service token',
      'Service tokens used by automated jobs expire 90 days after issue. Once expired, any job ' ||
      'authenticating with it fails with an authorization error. To rotate: generate a new token, ' ||
      'update the job/connection secret, then trigger a manual run to verify. Add a note to the ' ||
      'ticket recording that the token was rotated and the run succeeded.'),
  (3, 'Dashboards slow to load / reports take 30s or more',
      'Reports that take 30 seconds or longer to render are usually caused by large table scans on ' ||
      'a cold cache, missing indexes, or an under-sized warehouse. Fixes: pre-aggregate the heavy ' ||
      'tables, add indexes on the filtered/joined columns, and increase the warehouse or compute ' ||
      'size for interactive dashboards. Warm the cache with a scheduled query before peak hours.'),
  (4, 'Scheduling jobs around the maintenance window',
      'The platform maintenance window runs nightly from 1:00 to 3:00 AM. Jobs scheduled inside ' ||
      'that window may be paused, throttled, or dropped. Move recurring jobs to start after 3:15 AM ' ||
      'to avoid overlap, and stagger heavy jobs so they do not all start on the hour.'),
  (5, 'Understanding churn score and account health',
      'Each account has a churn score from 0 to 1 (higher = more likely to churn) and a lifetime ' ||
      'value. A churn score above 0.6 with an open high-priority ticket signals an at-risk account ' ||
      'that should be escalated to the account team. Use the customer_360 table for lifetime value, ' ||
      'churn score, CSAT, and renewal date when briefing on an account.');

-- ---------------------------------------------------------------------------
-- customer_360 — analytics profile the agent joins for account briefings.
-- Keyed by customer_id to match the operational `customers` table (Acme=1, Bluefin=2).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE {{uc_catalog}}.{{uc_schema}}.customer_360 (
  customer_id    BIGINT,
  lifetime_value DECIMAL(12,2),
  churn_score    DOUBLE,
  csat_score     DOUBLE,
  renewal_date   DATE,
  region         STRING
);

INSERT INTO {{uc_catalog}}.{{uc_schema}}.customer_360
  (customer_id, lifetime_value, churn_score, csat_score, renewal_date, region) VALUES
  (1, 480000.00, 0.72, 3.8, DATE'2026-03-15', 'NA'),   -- Acme Robotics: high value, at-risk (open high-pri ticket)
  (2,  95000.00, 0.28, 4.4, DATE'2026-08-01', 'EU');   -- Bluefin Analytics: healthy

-- Enable Change Data Feed — required for the TRIGGERED synced table (create_synced_tables.py).
ALTER TABLE {{uc_catalog}}.{{uc_schema}}.customer_360
  SET TBLPROPERTIES (delta.enableChangeDataFeed = true);
