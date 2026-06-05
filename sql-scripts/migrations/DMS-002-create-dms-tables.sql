-- AGENT-DMS-002: Create DMS table schema
-- Run ONCE manually; safe to re-run (all statements use IF NOT EXISTS / IF NOT EXISTS).
-- Schema: agents  (matches existing agents.rulesdev, agents.user_manual, etc.)

-- 1. Doctype registry — mirrors the JSON config; synced from app on every startup.
CREATE TABLE IF NOT EXISTS agents.dms_doctypes (
    name                    VARCHAR(100)    PRIMARY KEY,
    collection              VARCHAR(100)    NOT NULL,
    embedding_model         VARCHAR(200)    NOT NULL,
    is_scanned              BOOLEAN         DEFAULT FALSE,
    ocr_agent               VARCHAR(100),
    extraction_agent        VARCHAR(100),
    chunking_strategy       VARCHAR(20)     NOT NULL DEFAULT 'single',
    chunk_size              INTEGER,
    chunk_overlap           INTEGER,
    similarity_threshold    DECIMAL(4,2)    DEFAULT 0.85
);

-- 2. Categories — per-tenant; created/managed by admin UI via Spring Boot.
CREATE TABLE IF NOT EXISTS agents.dms_categories (
    id                          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    itenantid                   INTEGER         NOT NULL,
    name                        VARCHAR(100)    NOT NULL,
    color                       VARCHAR(7),
    similarity_threshold        DECIMAL(4,2)    DEFAULT 0.85,
    default_chunking_strategy   VARCHAR(20)     DEFAULT 'token',
    chunk_size                  INTEGER         DEFAULT 512,
    chunk_overlap               INTEGER         DEFAULT 50,
    created_at                  TIMESTAMP       DEFAULT NOW(),
    UNIQUE (itenantid, name)
);

-- 3. Documents — one row per logical document, independent of version.
CREATE TABLE IF NOT EXISTS agents.dms_documents (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    itenantid       INTEGER         NOT NULL,
    category_id     UUID            REFERENCES agents.dms_categories(id),
    document_name   VARCHAR(500)    NOT NULL,
    scope           VARCHAR(10)     NOT NULL DEFAULT 'tenant',
    created_by      INTEGER         NOT NULL,
    created_at      TIMESTAMP       DEFAULT NOW()
);

-- 4. Document versions — tracks every version of a document.
CREATE TABLE IF NOT EXISTS agents.dms_document_versions (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id             UUID            NOT NULL REFERENCES agents.dms_documents(id),
    version_number          INTEGER         NOT NULL,
    storage_url             TEXT,
    sha256_hash             VARCHAR(64)     NOT NULL,
    sha256_verified         BOOLEAN         DEFAULT FALSE,
    file_size_bytes         BIGINT,
    file_extension          VARCHAR(20),
    classification          VARCHAR(20)     NOT NULL DEFAULT 'Internal',
    version_status          VARCHAR(20)     NOT NULL DEFAULT 'draft',
    is_active               BOOLEAN         NOT NULL DEFAULT FALSE,
    summary                 TEXT,
    allowed_roles           JSONB           DEFAULT '[]',
    allowed_agents          JSONB           DEFAULT '["ALL_AGENTS"]',
    document_date           DATE,
    retention_expiry_date   DATE,
    entity_tags             JSONB           DEFAULT '[]',
    org_unit_id             VARCHAR(100),
    created_by              INTEGER         NOT NULL,
    approved_by             INTEGER,
    checker_notes           TEXT,
    approved_at             TIMESTAMP,
    ingested_at             TIMESTAMP,
    UNIQUE (document_id, version_number)
);

-- 5. Audit table — soft-deleted versions are moved here, not physically deleted.
CREATE TABLE IF NOT EXISTS agents.dms_document_versions_audit (
    LIKE agents.dms_document_versions INCLUDING ALL,
    archived_at     TIMESTAMP   DEFAULT NOW(),
    archived_by     INTEGER
);

-- 6. Chunks — replaces agents.user_manual and all legacy per-doctype tables.
CREATE TABLE IF NOT EXISTS agents.dms_chunks (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id      UUID        NOT NULL REFERENCES agents.dms_document_versions(id),
    chunk_index     INTEGER     NOT NULL,
    page_number     INTEGER,
    chunk_text      TEXT        NOT NULL,
    chunk_strategy  VARCHAR(20),
    created_at      TIMESTAMP   DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_dms_documents_tenant
    ON agents.dms_documents(itenantid);

CREATE INDEX IF NOT EXISTS idx_dms_versions_document
    ON agents.dms_document_versions(document_id);

CREATE INDEX IF NOT EXISTS idx_dms_versions_status
    ON agents.dms_document_versions(version_status, is_active);

CREATE INDEX IF NOT EXISTS idx_dms_chunks_version
    ON agents.dms_chunks(version_id);
