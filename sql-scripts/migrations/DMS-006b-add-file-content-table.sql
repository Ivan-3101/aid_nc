-- DMS-006b: DB-backed file storage for environments without external object storage
-- Run ONCE; safe to re-run (IF NOT EXISTS).
--
-- When IngestRequest.file_content is provided (base64 binary), the pipeline
-- stores the raw bytes here instead of requiring S3/GCS.  storage_url is left
-- NULL in dms_document_versions for these records.

CREATE TABLE IF NOT EXISTS agents.dms_file_content (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id      UUID        NOT NULL REFERENCES agents.dms_document_versions(id),
    content         BYTEA       NOT NULL,
    file_extension  VARCHAR(20),
    checksum        VARCHAR(64),               -- SHA-256 hex of content
    created_at      TIMESTAMP   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dms_file_content_version
    ON agents.dms_file_content(version_id);
