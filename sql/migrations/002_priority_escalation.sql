-- 002 — priority escalation.
-- Open `high` tickets from enterprise customers are escalated to `urgent` with a 4h SLA, and the
-- dashboard shows a priority badge. Every other ticket keeps the default 24h SLA.

ALTER TABLE tickets ADD COLUMN sla_hours INT NOT NULL DEFAULT 24;
ALTER TABLE tickets ADD COLUMN escalated_at TIMESTAMPTZ;
ALTER TABLE tickets ADD CONSTRAINT tickets_priority_check
  CHECK (priority IN ('low', 'normal', 'high', 'urgent'));

-- The rule, enforced in the database so every writer (dashboard, agent, scripts) gets it.
CREATE FUNCTION escalate_ticket() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
BEGIN
  IF NEW.status = 'open' AND NEW.priority = 'high'
     AND EXISTS (SELECT 1 FROM customers c WHERE c.id = NEW.customer_id AND c.tier = 'enterprise') THEN
    NEW.priority     := 'urgent';
    NEW.sla_hours    := 4;
    NEW.escalated_at := COALESCE(NEW.escalated_at, now());
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER tickets_escalate
  BEFORE INSERT OR UPDATE OF status, priority, customer_id ON tickets
  FOR EACH ROW EXECUTE FUNCTION escalate_ticket();

-- Apply the rule to the tickets that already exist.
UPDATE tickets t SET priority = 'urgent', sla_hours = 4, escalated_at = now()
FROM customers c
WHERE c.id = t.customer_id AND c.tier = 'enterprise' AND t.status = 'open' AND t.priority = 'high';
