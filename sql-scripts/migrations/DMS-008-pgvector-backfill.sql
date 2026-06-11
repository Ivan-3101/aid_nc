-- AGENT-DMS-008 Phase 2: Backfill missing metadata on legacy vectors
-- Run AFTER DMS-008-legacy-data-migration.sql and BEFORE cutover.
-- Uses a batched loop to avoid long-running lock contention.

DO $$
DECLARE
    batch_size INT := 10000;
    updated    INT;
BEGIN
    LOOP
        UPDATE langchain_pg_embedding
        SET cmetadata = cmetadata || jsonb_build_object(
            'is_active',        true,
            'scope',            'tenant',
            'version_status',   'active',
            'classification',   'Internal',
            'injection_flag',   false
        )
        WHERE id IN (
            SELECT id FROM langchain_pg_embedding
            WHERE cmetadata->>'is_active' IS NULL
              AND collection_id IN (
                  SELECT uuid FROM agents.langchain_pg_collection
                  WHERE name IN (
                      'caseagentv1', 'userManual', 'rulesDev',
                      'sqlagentv1', 'uinavigatorv1'
                  )
              )
            LIMIT batch_size
        );

        GET DIAGNOSTICS updated = ROW_COUNT;
        RAISE NOTICE 'Backfilled % rows', updated;
        EXIT WHEN updated = 0;
        PERFORM pg_sleep(0.1);  -- brief pause between batches
    END LOOP;

    -- Final validation
    DECLARE remaining INT;
    BEGIN
        SELECT COUNT(*) INTO remaining
        FROM langchain_pg_embedding
        WHERE cmetadata->>'is_active' IS NULL;
        RAISE NOTICE 'Vectors still missing is_active: %', remaining;
    END;
END $$;
