-- 001 — baseline: the Ep2 operational schema (customers, tickets, support_interactions).
-- Idempotent, so it is a no-op on a database that already has these tables.

CREATE TABLE IF NOT EXISTS customers (
  id     BIGINT PRIMARY KEY,
  name   TEXT,
  tier   TEXT,
  email  TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
  id           BIGINT PRIMARY KEY,
  customer_id  BIGINT REFERENCES customers(id),
  status       TEXT,          -- open | pending | resolved
  priority     TEXT,          -- low | normal | high
  subject      TEXT,
  body         TEXT,
  created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS support_interactions (
  id          BIGSERIAL PRIMARY KEY,
  ticket_id   BIGINT REFERENCES tickets(id),
  author      TEXT,           -- 'human' | 'agent'
  note        TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- Fictional seed data (sanitized — no real customers).
INSERT INTO customers (id, name, tier, email) VALUES
  (1, 'Acme Robotics', 'enterprise', 'ops@acme.example'),
  (2, 'Bluefin Analytics', 'pro', 'support@bluefin.example')
ON CONFLICT (id) DO NOTHING;

INSERT INTO tickets (id, customer_id, status, priority, subject, body) VALUES
  (482, 1, 'open',    'high',   'Sync job failing',       'Nightly sync errors out at 2am.'),
  (491, 2, 'pending', 'normal', 'Dashboard slow to load', 'Reports take 30s+ to render.')
ON CONFLICT (id) DO NOTHING;
