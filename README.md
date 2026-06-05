# DIA — DronaPay Intelligent Agent

FastAPI service that powers DronaPay's multi-agent RAG (Retrieval-Augmented Generation) system and Document Management System (DMS) ingestion pipeline.

---

## How it works

```
Spring Boot / UI
       │
       ▼
POST /ingest_document ──► Background pipeline
       │                        │
       │                   1. SHA-256 dedup check
       │                   2. Store file bytes (DB or S3)
       │                   3. Extract text (PDF/DOCX/OCR/CSV/JSON)
       │                   4. Prompt injection scan
       │                   5. Chunk  (single / token / character / page / row)
       │                   6. Chunk count guard
       │                   7. Embed  (HuggingFace or OpenAI)
       │                   8. Insert vectors → PGVector
       │                   9. Insert chunks  → agents.dms_chunks
       │                  10. Update status  → pending_approval
       │
GET /document_status/{id} ──► Poll status
       │
POST /check_similarity ──► Near-duplicate detection (before ingestion)
       │
POST /agent ──► Query pipeline
       │               │
       │          1. Run prerequisites  (DB / Redis / API lookups)
       │          2. get_similarities   (PGVector search, tenant-isolated)
       │          3. Run postrequisites (fetch full records by docid)
       │          4. Build prompt + call LLM (OpenAI / Google / Ollama)
       │          5. Return JSON response
       │
POST /archive_version ──► Soft-delete vectors (Maker-Checker callback)
DELETE /internal/tenant_vectors/{id} ──► Hard-delete on tenant offboarding
```

### File ingestion — two paths

| Path | When to use | How it works |
|------|-------------|--------------|
| **`file_content`** (base64) | Local / no cloud storage | Bytes decoded, stored in `agents.dms_file_content` (PostgreSQL). No AWS/GCS needed. |
| **`storage_url`** | Production with S3 / GCS | App fetches from URL; SSRF allowlist (`ALLOWED_STORAGE_ORIGINS`) enforced per-request. |
| **`raw_text`** | Pre-extracted plain text | Used directly, no file handling. |

---

## Local testing with Docker Compose

### 1. Copy and fill in the env file

```bash
cp .env.example .env
```

The only value you must change is `OPENAI_API_KEY` (or configure an Ollama model — see [Agent configuration](#agent-configuration) below). Everything else works as-is for local testing.

### 2. Run the DB migrations

Migrations must be run against the same PostgreSQL instance the app connects to. Edit the connection details in each command to match your `.env`:

```bash
PSQL="psql -h <host> -p <port> -U <DB_USER> -d <dbname>"

# DMS schema and tables (run in order)
$PSQL -f sql-scripts/2025-07-31/0-Script-ALL-SchemaCreation-30-jul-2025.sql
$PSQL -f sql-scripts/2025-07-31/1-Script-ALL-ConfigInsert-30-jul-2025.sql
$PSQL -f sql-scripts/2025-08-06/0-Script-ALL-uiaiagentsInsert-6-aug-2025.sql

# DMS-001: backfill tenant isolation fields on existing vectors
$PSQL -f sql-scripts/migrations/DMS-001-backfill-vector-metadata.sql

# DMS-002: create dms_* tables + doctype registry
$PSQL -f sql-scripts/migrations/DMS-002-create-dms-tables.sql
$PSQL -f sql-scripts/migrations/DMS-002-insert-doctypes-sysconfig.sql

# DMS-006: add error_message and chunk_count columns
$PSQL -f sql-scripts/migrations/DMS-006-add-status-columns.sql

# DMS-006b: add dms_file_content table (DB-backed file storage)
$PSQL -f sql-scripts/migrations/DMS-006b-add-file-content-table.sql
```

### 3. Build and start

```bash
docker compose build --build-arg HF_TOKEN=hf_...
docker compose up
```

The embedding model (`sentence-transformers/all-mpnet-base-v2`) is downloaded **once at build time** and baked into the image. The running container makes no outbound calls to HuggingFace — the image works in air-gapped / no-internet client environments.

The API is available at **http://localhost:8000** once the `dia` service prints `Application startup complete`.

### 4. Iterate without rebuilding

`app.py`, `utils.py`, `db.py`, `globals.py`, and `config.json` are bind-mounted into the container. Uvicorn runs with `--reload`, so saving any of these files restarts the server automatically.

### 5. Useful commands

```bash
docker compose logs -f dia          # follow app logs
docker compose down                 # stop (keep DB volume)
docker compose down -v              # stop and wipe DB volume
```

---

## Environment variables

All variables are read by `entrypoint.sh`, which writes them to the `/app/secrets/` directory before starting uvicorn. See `.env.example` for a copy-paste template.

### Required

| Variable | Description |
|----------|-------------|
| `REST_USER` | HTTP Basic auth username |
| `REST_PWD` | HTTP Basic auth password |
| `DB_USER` | PostgreSQL username |
| `DB_PWD` | PostgreSQL password |
| `TXNDB1_CONN` | psycopg connection string **prefix** for the main app DB |
| `AGENT_CONN` | psycopg connection string **prefix** for the PGVector DB |
| `OPENAI_API_KEY` | OpenAI API key (required unless using Ollama or Google) |

**Connection string format** — `config.json`'s `appname` value (`agentpn`) is appended automatically, so the string must end with the parameter that accepts the app name:

```
TXNDB1_CONN=postgresql+psycopg://user:pwd@host:5432/dbname?application_name=
AGENT_CONN=postgresql+psycopg://user:pwd@host:5432/dbname?options=-c%20search_path=agents&application_name=
```

### Optional — LLM providers

| Variable | Description |
|----------|-------------|
| `GOOGLE_API_KEY` | Google Generative AI key |

The LLM provider is set per-agent in the agent config (`model_config.provider`). Global default is in `config.json` (`llm_provider`). Supported values: `openai`, `google`, `ollama`.

For Ollama, set `ollama_base_url` in `config.json` and optionally:

| Variable | Description |
|----------|-------------|
| `OLLAMA_USERNAME` | Ollama HTTP Basic auth username |
| `OLLAMA_PASSWORD` | Ollama HTTP Basic auth password |

### DMS ingestion settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOWED_STORAGE_ORIGINS` | *(empty)* | Comma-separated URL prefixes allowed for `storage_url`. Required if you use external storage. Leave empty when using `file_content` (DB path). Example: `https://my-bucket.s3.amazonaws.com` |
| `MAX_CHUNKS_PER_DOCUMENT` | `2000` | Hard limit on chunks per document. Requests exceeding this are rejected. |
| `EMBED_BATCH_SIZE` | `100` | Max chunks sent to the embedding model in one call. |
| `MIN_CHUNK_SIZE` | `64` | Minimum allowed `chunk_size` in a `chunking_override`. |
| `MAX_CHUNK_SIZE` | `2048` | Maximum allowed `chunk_size` in a `chunking_override`. |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `INTERNAL_API_SECRET` | *(empty)* | Required to call `DELETE /internal/tenant_vectors/{id}`. Set a long random string. Never expose this endpoint publicly. |

---

## Agent configuration

Agent configs are stored in the `masters.sysconfig` database table and loaded at startup via `add_config()`. See [`agent-config-example.json`](agent-config-example.json) for the complete schema. To update configs, update the DB row and call `POST /reloadconfig`.

### config.json (app-level)

```json
{
  "appname": "agentpn",
  "provider": "huggingface",
  "llm_provider": "openai",
  "ollama_base_url": "https://ollama.example.com"
}
```

| Key | Description |
|-----|-------------|
| `appname` | Appended to connection strings from secrets. Must match the `env` column in `masters.sysconfig`. |
| `provider` | Embedding provider: `huggingface` or `openai`. |
| `llm_provider` | Default LLM: `openai`, `google`, or `ollama`. Can be overridden per-agent. |

### Agent entry structure

```json
{
  "agent": "caseagentv1",
  "is_active": true,
  "doctype": "RuleSpecificL1Action",
  "model_config": {
    "provider": "openai",
    "role": "user",
    "params": { "model": "gpt-4.1", "temperature": 0 }
  },
  "vectorstore": {
    "collection": "caseagentv1",
    "embedding_model": "sentence-transformers/all-mpnet-base-v2",
    "data": ["prerequisites.AccountMaster"],
    "filter": ["RuleID"],
    "metadata": ["caseid", "ruleid", "itenantid"],
    "cnt_similarities": 2
  },
  "prerequisites": [...],
  "postrequisites": [...],
  "prompt_template": { "input": [...], "template": "..." }
}
```

**Key fields:**

| Field | Description |
|-------|-------------|
| `is_active` | `true` (default) or `false`. Inactive agents are not loaded into memory; calling `/agent` with their ID returns 404. |
| `doctype` | References a name in the `doctypes` section of the config. Used to resolve chunking strategy and OCR settings. |
| `vectorstore.metadata` | The tenant key **must** be lowercase `itenantid`. A mis-cased variant (e.g. `iTenantID`) raises a `ValueError` at startup. |

### Multi-vectorstore agents

An agent can query multiple collections in a single call using the `vectorstores` array format:

```json
{
  "agent": "fraudAnalysisAgent",
  "vectorstores": [
    {
      "label": "similar_cases",
      "collection": "caseagentv1",
      "data": ["prerequisites.AccountMaster"],
      "filter": ["RuleID"],
      "metadata": ["caseid", "ruleid", "itenantid"],
      "embedding_model": "sentence-transformers/all-mpnet-base-v2",
      "k": 2
    },
    {
      "label": "policy_chunks",
      "collection": "policyDocument",
      "data": ["input_data.user_query"],
      "filter": [],
      "metadata": ["document_id", "version_id", "chunk_index"],
      "embedding_model": "sentence-transformers/all-mpnet-base-v2",
      "k": 3
    }
  ]
}
```

The `/agent` response will have `similarities.similar_cases` and `similarities.policy_chunks` as separate arrays. Single-vectorstore agents are automatically normalized to the same format internally but return a flat list for backward compatibility.

### Doctype registry

Doctypes define chunking strategy, embedding model, and OCR settings for each document category. Stored in `masters.sysconfig` under `cfgname='doctypes'` and synced to `agents.dms_doctypes` on every startup.

```json
{
  "name": "PolicyDocument",
  "collection": "policyDocument",
  "embedding_model": "sentence-transformers/all-mpnet-base-v2",
  "is_scanned": false,
  "ocr_agent": null,
  "extraction_agent": null,
  "chunking": { "strategy": "token", "chunk_size": 512, "overlap": 50 },
  "similarity_threshold": 0.85
}
```

| Chunking strategy | Behaviour | `chunk_size` / `overlap` |
|-------------------|-----------|--------------------------|
| `single` | Entire input = one vector | not required |
| `token` | `TokenTextSplitter` | required |
| `character` | `RecursiveCharacterTextSplitter` | required |
| `page` | Input pre-split into pages | not required |
| `row` | CSV rows or JSON array elements | not required |

---

## API endpoints

All endpoints require HTTP Basic auth (`REST_USER` / `REST_PWD`).

### Agent endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agent` | Run a configured agent (prerequisites → similarity search → LLM) |
| `POST` | `/add_to_vectorstore` | Ingest a single record using an agent's doctype + chunking config |
| `POST` | `/suggest_action` | Similarity search only (no LLM) |
| `POST` | `/recommend_action` | Policy + similarity + LLM chain |
| `POST` | `/reloadconfig?agentname=` | Reload agent configs from DB; `agentname` param limits reload to one agent |

### DMS endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest_document` | Queue a full document ingestion (returns immediately; pipeline runs in background) |
| `GET` | `/document_status/{document_id}` | Poll ingestion status: `queued → extracting → chunking → embedding → pending_approval` (or `failed`) |
| `POST` | `/check_similarity` | Near-duplicate detection before ingestion |
| `POST` | `/archive_version` | Soft-delete vectors for a version (Maker-Checker callback from Spring Boot) |
| `DELETE` | `/internal/tenant_vectors/{itenantid}` | **Hard-delete** all vectors for a deactivated tenant. Requires `X-Internal-Secret` header. |

### `POST /ingest_document`

```json
{
  "document_id": "uuid",
  "version_id": "uuid",
  "itenantid": 17,
  "doctype": "PolicyDocument",
  "file_type": "pdf",

  "file_content": "<base64>",
  "sha256_hash": "abc123...",

  "chunking_override": null,
  "force_new_version": false,
  "force_separate": false
}
```

Provide **exactly one** of:
- `file_content` — base64 file bytes stored in PostgreSQL (`agents.dms_file_content`). No external storage needed.
- `storage_url` — URL to fetch from. Must start with a value in `ALLOWED_STORAGE_ORIGINS`.
- `raw_text` — plain text, used directly.

### `POST /check_similarity`

```json
{
  "itenantid": 17,
  "category": "PolicyDocument",
  "raw_text": "First 5000 chars of document...",
  "sha256_hash": "abc123..."
}
```

Returns `{ "has_near_duplicates": true/false, "threshold": 0.85, "matches": [...] }`. The threshold comes from the doctype config's `similarity_threshold`.

---

## Database schema overview

```
masters.sysconfig          ← agent configs and doctype registry (loaded at startup)

agents.dms_doctypes        ← doctype registry mirror (synced from sysconfig on startup)
agents.dms_categories      ← per-tenant document categories (managed by Spring Boot)
agents.dms_documents       ← one row per logical document
agents.dms_document_versions ← versioned rows with status, hash, classification
agents.dms_chunks          ← chunk text after ingestion
agents.dms_file_content    ← raw file bytes when using file_content path (no S3/GCS)

langchain_pg_embedding     ← PGVector vectors (one row per chunk)
langchain_pg_collections   ← PGVector collection metadata
```

---

## Migration run order

Always run in this sequence on a fresh database:

```
1. 0-Script-ALL-SchemaCreation-30-jul-2025.sql
2. 1-Script-ALL-ConfigInsert-30-jul-2025.sql
3. 0-Script-ALL-uiaiagentsInsert-6-aug-2025.sql
4. DMS-001-backfill-vector-metadata.sql
5. DMS-002-create-dms-tables.sql
6. DMS-002-insert-doctypes-sysconfig.sql
7. DMS-006-add-status-columns.sql
8. DMS-006b-add-file-content-table.sql
```

**Legacy data migration** (run separately after staging validation):
```
9.  DMS-008-legacy-data-migration.sql
10. DMS-008-pgvector-backfill.sql
11. DMS-008-cutover.sql             ← uncomment DROP statements after 48h sign-off
```

---

## Production / CI build (no Compose)

```bash
docker build -t dia:latest --build-arg HF_TOKEN=hf_... .

docker run -d \
  --name dia \
  -p 8000:8000 \
  -v "$(pwd)/secrets:/app/secrets:ro" \
  -v "$(pwd)/logs:/app/logs" \
  dia:latest
```

Secrets are read as individual files from `/app/secrets/`. Required files match the env variable names: `restuser`, `restpwd`, `OPENAI_API_KEY`, `DBUSER`, `DBPWD`, `txndb1`, `agent`.

For production the `CMD` in the Dockerfile is used (`uvicorn` without `--reload`). The `entrypoint.sh` hot-reload mode is only active when Compose invokes it via the `entrypoint:` override.

---

## Local development without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Populate secrets/ — one file per key (same names as env variables)
mkdir -p secrets
echo -n "admin"      > secrets/restuser
echo -n "changeme"   > secrets/restpwd
# ... etc

uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```
