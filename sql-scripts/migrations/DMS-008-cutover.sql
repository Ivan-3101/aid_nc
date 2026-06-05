-- AGENT-DMS-008 Phase 4: Cutover — run ONLY after Phase 3 validation passes
-- and the team has explicitly signed off.
-- Replace <app_user> with the PostgreSQL role used by the application.

-- Step 1: Set legacy tables to read-only
REVOKE INSERT, UPDATE, DELETE ON agents.user_manual          FROM <app_user>;
REVOKE INSERT, UPDATE, DELETE ON agents.rulespecificl1action FROM <app_user>;
REVOKE INSERT, UPDATE, DELETE ON agents.rulesdev             FROM <app_user>;
REVOKE INSERT, UPDATE, DELETE ON agents.sqldev               FROM <app_user>;
REVOKE INSERT, UPDATE, DELETE ON agents.uinavigator          FROM <app_user>;

-- Step 2: Monitor for 48 hours before proceeding to drops.
-- Run the Phase 3 validation script against production to confirm parity.

-- Step 3 (AFTER 48-hour sign-off): Drop legacy tables.
-- UNCOMMENT ONLY AFTER EXPLICIT TEAM APPROVAL.
-- DROP TABLE IF EXISTS agents.user_manual;
-- DROP TABLE IF EXISTS agents.rulespecificl1action;
-- DROP TABLE IF EXISTS agents.rulesdev;
-- DROP TABLE IF EXISTS agents.sqldev;
-- DROP TABLE IF EXISTS agents.uinavigator;
