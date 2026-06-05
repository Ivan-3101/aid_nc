-- AGENT-DMS-008: Migrate legacy agent tables → dms_* schema
-- Run in STAGING first. Validate with Phase 3 script before production.
-- All INSERT statements use gen_random_uuid() so re-running creates duplicate
-- records — wrap in a transaction and validate counts before committing.
--
-- Column names are derived from the postrequisites queries in the agent configs.
-- Run '\d agents.<table>' against your DB and adjust if columns differ.
--
-- Run order within this script:
--   Phase 1A  — placeholder category
--   Phase 1B  — dms_documents
--   Phase 1C  — dms_document_versions
--   Phase 1D  — dms_chunks  (one per legacy row)

BEGIN;

-- ── PHASE 1A: placeholder category for migrated records ───────────────────────
-- Spring Boot will reassign proper categories via the admin UI after migration.

INSERT INTO agents.dms_categories (itenantid, name, color)
SELECT DISTINCT CAST(cmetadata->>'itenantid' AS INTEGER), 'Migrated', '#CCCCCC'
FROM langchain_pg_embedding
WHERE collection_id IN (
    SELECT uuid FROM langchain_pg_collections
    WHERE name IN ('userManual','caseagentv1','rulesDev','sqlagentv1','uinavigatorv1')
)
AND cmetadata->>'itenantid' IS NOT NULL
ON CONFLICT (itenantid, name) DO NOTHING;

-- ── PHASE 1B — agents.user_manual ────────────────────────────────────────────
-- Expected columns: docid, doc_name, version, date, pageid, page_content
-- itenantid is in PGVector cmetadata, not in the table itself.
-- We join via docid (= langchain_pg_embedding id).

INSERT INTO agents.dms_documents
    (id, itenantid, category_id, document_name, scope, created_by, created_at)
SELECT
    gen_random_uuid(),
    CAST(e.cmetadata->>'itenantid' AS INTEGER),
    c.id,
    COALESCE(um.doc_name, 'Migrated UserManual Document'),
    'tenant',
    0,
    NOW()
FROM (
    SELECT DISTINCT doc_name, CAST(e2.cmetadata->>'itenantid' AS INTEGER) AS itenantid
    FROM agents.user_manual um2
    JOIN langchain_pg_embedding e2 ON e2.id::text = um2.docid::text
) grp
JOIN agents.dms_categories c
    ON c.itenantid = grp.itenantid AND c.name = 'Migrated'
JOIN agents.user_manual um ON um.doc_name = grp.doc_name
JOIN langchain_pg_embedding e
    ON e.id::text = um.docid::text
    AND CAST(e.cmetadata->>'itenantid' AS INTEGER) = grp.itenantid
LIMIT 1;  -- one document record per distinct doc_name + tenant

-- Document versions for user_manual
INSERT INTO agents.dms_document_versions
    (id, document_id, version_number, sha256_hash, classification,
     version_status, is_active, created_by, ingested_at)
SELECT
    gen_random_uuid(),
    d.id,
    1,
    encode(sha256((d.document_name || d.itenantid::text)::bytea), 'hex'),
    'Internal',
    'active',
    true,
    0,
    NOW()
FROM agents.dms_documents d
WHERE d.created_by = 0
ON CONFLICT DO NOTHING;

-- Chunks for user_manual (strategy = page)
INSERT INTO agents.dms_chunks
    (id, version_id, chunk_index, page_number, chunk_text, chunk_strategy)
SELECT
    gen_random_uuid(),
    dv.id,
    (ROW_NUMBER() OVER (PARTITION BY dv.id ORDER BY um.pageid)) - 1,
    um.pageid,
    um.page_content,
    'page'
FROM agents.user_manual um
JOIN agents.dms_documents d
    ON d.document_name = COALESCE(um.doc_name, 'Migrated UserManual Document')
    AND d.itenantid    = CAST(
        (SELECT cmetadata->>'itenantid' FROM langchain_pg_embedding
         WHERE id::text = um.docid::text LIMIT 1)
        AS INTEGER)
JOIN agents.dms_document_versions dv ON dv.document_id = d.id;

-- ── PHASE 1B — agents.rulespecificl1action ────────────────────────────────────
-- Expected columns: docid, ruleid, caseid, batchdate, monthlyprofile, accountmaster
-- Strategy: single (whole row = one chunk)

INSERT INTO agents.dms_documents
    (id, itenantid, category_id, document_name, scope, created_by, created_at)
SELECT
    gen_random_uuid(),
    CAST(e.cmetadata->>'itenantid' AS INTEGER),
    c.id,
    'Case-' || r.caseid::text,
    'tenant',
    0,
    NOW()
FROM agents.rulespecificl1action r
JOIN langchain_pg_embedding e ON e.id::text = r.docid::text
JOIN agents.dms_categories c
    ON c.itenantid = CAST(e.cmetadata->>'itenantid' AS INTEGER)
    AND c.name = 'Migrated';

INSERT INTO agents.dms_document_versions
    (id, document_id, version_number, sha256_hash, classification,
     version_status, is_active, created_by, ingested_at)
SELECT
    gen_random_uuid(), d.id, 1,
    encode(sha256((d.document_name || d.itenantid::text)::bytea), 'hex'),
    'Internal', 'active', true, 0, NOW()
FROM agents.dms_documents d WHERE d.created_by = 0 ON CONFLICT DO NOTHING;

INSERT INTO agents.dms_chunks
    (id, version_id, chunk_index, chunk_text, chunk_strategy)
SELECT
    gen_random_uuid(),
    dv.id,
    0,
    'ruleid: ' || r.ruleid::text
        || ', caseid: ' || r.caseid::text
        || ', batchdate: ' || r.batchdate::text,
    'single'
FROM agents.rulespecificl1action r
JOIN agents.dms_documents d   ON d.document_name = 'Case-' || r.caseid::text
JOIN agents.dms_document_versions dv ON dv.document_id = d.id;

-- ── PHASE 1C — agents.rulesdev ────────────────────────────────────────────────
-- Expected columns: docid, ruleid, ruledescription, rulethresholds,
--                   rulefieldsdescription, ruletype, rulejson

INSERT INTO agents.dms_documents
    (id, itenantid, category_id, document_name, scope, created_by, created_at)
SELECT
    gen_random_uuid(),
    CAST(e.cmetadata->>'itenantid' AS INTEGER),
    c.id,
    'Rule-' || rd.ruleid::text,
    'tenant', 0, NOW()
FROM agents.rulesdev rd
JOIN langchain_pg_embedding e ON e.id::text = rd.docid::text
JOIN agents.dms_categories c
    ON c.itenantid = CAST(e.cmetadata->>'itenantid' AS INTEGER)
    AND c.name = 'Migrated';

INSERT INTO agents.dms_document_versions
    (id, document_id, version_number, sha256_hash, classification,
     version_status, is_active, created_by, ingested_at)
SELECT gen_random_uuid(), d.id, 1,
    encode(sha256((d.document_name || d.itenantid::text)::bytea), 'hex'),
    'Internal', 'active', true, 0, NOW()
FROM agents.dms_documents d WHERE d.created_by = 0 ON CONFLICT DO NOTHING;

INSERT INTO agents.dms_chunks
    (id, version_id, chunk_index, chunk_text, chunk_strategy)
SELECT
    gen_random_uuid(), dv.id, 0,
    COALESCE(rd.ruledescription, '') || ' ' || COALESCE(rd.ruletype, ''),
    'single'
FROM agents.rulesdev rd
JOIN agents.dms_documents d   ON d.document_name = 'Rule-' || rd.ruleid::text
JOIN agents.dms_document_versions dv ON dv.document_id = d.id;

-- ── PHASE 1D — agents.sqldev ──────────────────────────────────────────────────
-- Expected columns: docid, itenantid, queryid, sqldescription,
--                   sqlfielddescriptions, querytemplate

INSERT INTO agents.dms_documents
    (id, itenantid, category_id, document_name, scope, created_by, created_at)
SELECT
    gen_random_uuid(),
    sd.itenantid,
    c.id,
    'Query-' || sd.queryid::text,
    'tenant', 0, NOW()
FROM agents.sqldev sd
JOIN agents.dms_categories c
    ON c.itenantid = sd.itenantid AND c.name = 'Migrated';

INSERT INTO agents.dms_document_versions
    (id, document_id, version_number, sha256_hash, classification,
     version_status, is_active, created_by, ingested_at)
SELECT gen_random_uuid(), d.id, 1,
    encode(sha256((d.document_name || d.itenantid::text)::bytea), 'hex'),
    'Internal', 'active', true, 0, NOW()
FROM agents.dms_documents d WHERE d.created_by = 0 ON CONFLICT DO NOTHING;

INSERT INTO agents.dms_chunks
    (id, version_id, chunk_index, chunk_text, chunk_strategy)
SELECT
    gen_random_uuid(), dv.id, 0,
    COALESCE(sd.sqldescription, '') || ' ' || COALESCE(sd.querytemplate, ''),
    'single'
FROM agents.sqldev sd
JOIN agents.dms_documents d   ON d.document_name = 'Query-' || sd.queryid::text
JOIN agents.dms_document_versions dv ON dv.document_id = d.id;

-- ── PHASE 1E — agents.uinavigator ────────────────────────────────────────────
-- Expected columns: docid, screen_section, function, steps_to_reach, url

INSERT INTO agents.dms_documents
    (id, itenantid, category_id, document_name, scope, created_by, created_at)
SELECT
    gen_random_uuid(),
    CAST(e.cmetadata->>'itenantid' AS INTEGER),
    c.id,
    COALESCE(un.screen_section, 'UI-' || un.docid::text),
    'tenant', 0, NOW()
FROM agents.uinavigator un
JOIN langchain_pg_embedding e ON e.id::text = un.docid::text
JOIN agents.dms_categories c
    ON c.itenantid = CAST(e.cmetadata->>'itenantid' AS INTEGER)
    AND c.name = 'Migrated';

INSERT INTO agents.dms_document_versions
    (id, document_id, version_number, sha256_hash, classification,
     version_status, is_active, created_by, ingested_at)
SELECT gen_random_uuid(), d.id, 1,
    encode(sha256((d.document_name || d.itenantid::text)::bytea), 'hex'),
    'Internal', 'active', true, 0, NOW()
FROM agents.dms_documents d WHERE d.created_by = 0 ON CONFLICT DO NOTHING;

INSERT INTO agents.dms_chunks
    (id, version_id, chunk_index, chunk_text, chunk_strategy)
SELECT
    gen_random_uuid(), dv.id, 0,
    COALESCE(un.screen_section, '') || ': ' || COALESCE(un.steps_to_reach, ''),
    'single'
FROM agents.uinavigator un
JOIN agents.dms_documents d
    ON d.document_name = COALESCE(un.screen_section, 'UI-' || un.docid::text)
JOIN agents.dms_document_versions dv ON dv.document_id = d.id;

-- Validate counts before committing (uncomment to check, then re-run in tx)
-- SELECT 'dms_documents migrated:', COUNT(*) FROM agents.dms_documents WHERE created_by = 0;
-- SELECT 'dms_chunks migrated:',    COUNT(*) FROM agents.dms_chunks;

COMMIT;
