-- AGENT-DMS-002: Seed doctypes config into masters.sysconfig
-- Run ONCE after DMS-002-create-dms-tables.sql.
-- The app's add_config() loads this row into globals.config['doctypes'] at startup.
--
-- To update doctypes later: edit the JSON below and re-run; the ON CONFLICT
-- clause will upsert the row.  The app's sync_doctypes_to_db() will then
-- propagate changes to agents.dms_doctypes on next startup.

INSERT INTO masters.sysconfig (cfgname, application, env, config)
VALUES (
    'doctypes',
    'DIA',
    'agentpn',
    '[
      {
        "name": "RuleSpecificL1Action",
        "collection": "caseagentv1",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "single", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      },
      {
        "name": "UserManual",
        "collection": "userManual",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "page", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      },
      {
        "name": "RulesDev",
        "collection": "rulesDev",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "token", "chunk_size": 256, "overlap": 30 },
        "similarity_threshold": 0.85
      },
      {
        "name": "SQLDev",
        "collection": "sqlagentv1",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "page", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      },
      {
        "name": "uinavigatorv1",
        "collection": "uinavigatorv1",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "single", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      },
      {
        "name": "PolicyDocument",
        "collection": "policyDocument",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "token", "chunk_size": 512, "overlap": 50 },
        "similarity_threshold": 0.85
      },
      {
        "name": "ClaimDocument",
        "collection": "claimDocument",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": true,
        "ocr_agent": "openaiVision",
        "extraction_agent": "ocrToFhir",
        "chunking": { "strategy": "character", "chunk_size": 2000, "overlap": 200 },
        "similarity_threshold": 0.85
      },
      {
        "name": "KYCDocument",
        "collection": "kycDocument",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": true,
        "ocr_agent": "openaiVision",
        "extraction_agent": null,
        "chunking": { "strategy": "character", "chunk_size": 1500, "overlap": 150 },
        "similarity_threshold": 0.85
      },
      {
        "name": "CSVData",
        "collection": "csvData",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "row", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      },
      {
        "name": "JSONData",
        "collection": "jsonData",
        "embedding_model": "sentence-transformers/all-mpnet-base-v2",
        "is_scanned": false,
        "ocr_agent": null,
        "extraction_agent": null,
        "chunking": { "strategy": "row", "chunk_size": null, "overlap": null },
        "similarity_threshold": 0.85
      }
    ]'::jsonb
)
ON CONFLICT (cfgname, application, env)
DO UPDATE SET config = EXCLUDED.config;
