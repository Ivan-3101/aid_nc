-- FIX: Add created_at column to agents.dms_document_versions
--
-- Why created_at and not ingested_at?
-- ─────────────────────────────────────────────────────────────────────────────
-- ingested_at  — set at the END of the ingest pipeline (step 11).
--                It is NULL for any version in queued / extracting / chunking /
--                embedding / failed states.
--                Ordering by ingested_at DESC would put those in-progress rows
--                LAST, so GET /document_status would return a completed old
--                version instead of the current one being processed.
--
-- created_at   — set to NOW() at row creation (DEFAULT NOW()).
--                It is always populated, never changes, and correctly identifies
--                "the most recently created version row" regardless of pipeline
--                state.
--
-- Run order:
--   Apply this script, then deploy the app code.
--   No restart needed — app picks up the column automatically.

-- Step 1: add column (idempotent — no-op if it already exists)
ALTER TABLE agents.dms_document_versions
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();

-- Step 2: back-fill existing rows that have created_at = NULL.
-- Uses ingested_at as a fallback proxy, then approved_at, then NOW().
-- Rows with all three NULL are new draft rows — setting them to NOW() is safe.
UPDATE agents.dms_document_versions
SET    created_at = COALESCE(ingested_at, approved_at, NOW())
WHERE  created_at IS NULL;

-- Step 3: propagate the same column to the audit table if it exists.
ALTER TABLE agents.dms_document_versions_audit
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();

UPDATE agents.dms_document_versions_audit
SET    created_at = COALESCE(ingested_at, approved_at, NOW())
WHERE  created_at IS NULL;

-- Verify
SELECT COUNT(*)            AS total_versions,
       COUNT(created_at)   AS have_created_at,
       COUNT(*) FILTER (WHERE created_at IS NULL) AS still_null
FROM   agents.dms_document_versions;
