-- FIX: Add missing itenantid param to postrequisite queries that filter on it.
--
-- Root cause: the similar_cases (and similar) postrequisite queries include
--   AND d.itenantid = :itenantid
-- for tenant isolation, but the params array only declares the docid mapping.
-- execute_query_with_params then raises:
--   "A value is required for bind parameter 'itenantid'"
--
-- When these queries run as postrequisites, agent_data IS the similarities list,
-- e.g. [{"docid":"…","RuleID":"R001","itenantid":17}, ...].
-- "valueField": "0.itenantid" reads itenantid from the first similarity result.
--
-- How to apply:
--   1. Run this script once against your database.
--   2. Call POST /reloadconfig so the app picks up the updated config.
--
-- The jsonb_set calls add the itenantid param entry to each affected agent's
-- postrequisites[0].params array (the similar_cases query is always index 0).
-- Adjust the array index if your config has a different ordering.

-- ── rulesDev ──────────────────────────────────────────────────────────────────
UPDATE masters.sysconfig
SET config = jsonb_set(
    config,
    '{postrequisites, 0, params}',
    (config->'postrequisites'->0->'params') ||
    '[{"name": "itenantid", "valueField": "0.itenantid"}]'::jsonb
)
WHERE cfgname   = 'agents'
  AND config @> '[{"agent": "rulesDev"}]'
  AND NOT (config->'postrequisites'->0->'params' @> '[{"name":"itenantid"}]');

-- ── userManual ────────────────────────────────────────────────────────────────
UPDATE masters.sysconfig
SET config = jsonb_set(
    config,
    '{postrequisites, 0, params}',
    (config->'postrequisites'->0->'params') ||
    '[{"name": "itenantid", "valueField": "0.itenantid"}]'::jsonb
)
WHERE cfgname   = 'agents'
  AND config @> '[{"agent": "userManual"}]'
  AND NOT (config->'postrequisites'->0->'params' @> '[{"name":"itenantid"}]');

-- ── userManualPM ──────────────────────────────────────────────────────────────
UPDATE masters.sysconfig
SET config = jsonb_set(
    config,
    '{postrequisites, 0, params}',
    (config->'postrequisites'->0->'params') ||
    '[{"name": "itenantid", "valueField": "0.itenantid"}]'::jsonb
)
WHERE cfgname   = 'agents'
  AND config @> '[{"agent": "userManualPM"}]'
  AND NOT (config->'postrequisites'->0->'params' @> '[{"name":"itenantid"}]');

-- ── sqlagentv1 ────────────────────────────────────────────────────────────────
UPDATE masters.sysconfig
SET config = jsonb_set(
    config,
    '{postrequisites, 0, params}',
    (config->'postrequisites'->0->'params') ||
    '[{"name": "itenantid", "valueField": "0.itenantid"}]'::jsonb
)
WHERE cfgname   = 'agents'
  AND config @> '[{"agent": "sqlagentv1"}]'
  AND NOT (config->'postrequisites'->0->'params' @> '[{"name":"itenantid"}]');

-- ── Verify (run separately to confirm changes) ────────────────────────────────
-- SELECT cfgname, config->'postrequisites'->0->'params' AS params
-- FROM masters.sysconfig
-- WHERE cfgname = 'agents'
--   AND config @> '[{"agent": "rulesDev"}]';
