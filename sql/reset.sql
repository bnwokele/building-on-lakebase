-- Reset helper — clean operational state for a fresh seed (re-run sql/01_operational.sql after).
TRUNCATE support_interactions RESTART IDENTITY;
DELETE FROM tickets;
DELETE FROM customers;
-- Re-run sql/01_operational.sql to reseed.
