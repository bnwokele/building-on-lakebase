-- DEMO RESET ONLY (scripts/demo_reset.py): undo migration 002 on production so Episode 3 can be
-- recorded again from the "before" state. Real releases roll forward with a new migration.
DROP TRIGGER IF EXISTS tickets_escalate ON tickets;
DROP FUNCTION IF EXISTS escalate_ticket();
ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_priority_check;
UPDATE tickets SET priority = 'high' WHERE priority = 'urgent';
ALTER TABLE tickets DROP COLUMN IF EXISTS escalated_at;
ALTER TABLE tickets DROP COLUMN IF EXISTS sla_hours;
DELETE FROM schema_migrations WHERE version = '002';
