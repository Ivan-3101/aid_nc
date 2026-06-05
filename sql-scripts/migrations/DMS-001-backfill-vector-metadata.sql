-- AGENT-DMS-001: Backfill vector metadata for tenant isolation filters
-- Run ONCE manually BEFORE deploying the updated get_similarities code.
-- Safe to re-run: all WHERE clauses are idempotent.

DO $$
DECLARE
  renamed_count  INTEGER;
  backfill_count INTEGER;
BEGIN

  -- Step 1: Normalise iTenantID casing -> itenantid
  -- Existing vectors were ingested with the mixed-case key "iTenantID".
  -- The new filter code searches on lowercase "itenantid"; without this rename
  -- all tenant isolation filters would return zero results on historical data.
  UPDATE langchain_pg_embedding
  SET cmetadata = (cmetadata - 'iTenantID')
                  || jsonb_build_object('itenantid', cmetadata->>'iTenantID')
  WHERE cmetadata ? 'iTenantID'
    AND NOT (cmetadata ? 'itenantid');

  GET DIAGNOSTICS renamed_count = ROW_COUNT;
  RAISE NOTICE 'Step 1 — iTenantID -> itenantid key renamed: % record(s)', renamed_count;

  -- Step 2: Backfill is_active and scope on records that have neither field.
  -- Without is_active = true, the mandatory filter excludes all legacy vectors.
  -- scope = "tenant" is the safe default; platform-wide docs can be updated
  -- separately if required.
  UPDATE langchain_pg_embedding
  SET cmetadata = cmetadata || '{"is_active": true, "scope": "tenant"}'::jsonb
  WHERE cmetadata->>'is_active' IS NULL;

  GET DIAGNOSTICS backfill_count = ROW_COUNT;
  RAISE NOTICE 'Step 2 — is_active/scope backfilled: % record(s)', backfill_count;

  RAISE NOTICE 'Migration DMS-001 complete.';
END $$;
