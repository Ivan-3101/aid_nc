-- AGENT-DMS-006: Add error_message and chunk_count to dms_document_versions
-- Run BEFORE deploying the updated app code.
-- Both statements are idempotent (IF NOT EXISTS).

ALTER TABLE agents.dms_document_versions
    ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE agents.dms_document_versions
    ADD COLUMN IF NOT EXISTS chunk_count INTEGER;

-- Confirm the index defined in DMS-002 exists; create it if not.
CREATE INDEX IF NOT EXISTS idx_dms_versions_document
    ON agents.dms_document_versions(document_id);
