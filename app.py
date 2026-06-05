
from fastapi import FastAPI, Depends, HTTPException, status, Query, BackgroundTasks, Header
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from typing import Dict, Any, Optional, List
from uuid import UUID
from langchain_huggingface import HuggingFaceEmbeddings
from langchain.schema import Document
from langchain_openai import OpenAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain_openai import ChatOpenAI
from langchain.prompts import PromptTemplate
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text as sa_text
import globals
import db
import os
import json
import uuid
import re
import sys
import zipfile
import io
import hashlib
import base64
import time

import db
import utils
import storage
from datetime import date, datetime
from html import escape
import shlex
import math


# Initialize app and configurations
globals.set_config_params()
logger, hldr_faust = globals.create_logs('DIA')
globals.startup()


app = FastAPI()

security = HTTPBasic()
if globals.secret_data.get('OPENAI_API_KEY'):
    os.environ["OPENAI_API_KEY"] = globals.secret_data['OPENAI_API_KEY']
if globals.secret_data.get('GOOGLE_API_KEY'):
    os.environ["GOOGLE_API_KEY"] = globals.secret_data['GOOGLE_API_KEY']
vector_store = {}
embeddings={}

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    
    if credentials.username != globals.secret_data['restuser'] or credentials.password != globals.secret_data['restpwd']:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class DynamicRequest(BaseModel):
    data: Dict[str, Any]
    agentid: str

# ─── DMS-003 request / response models ────────────────────────────────────────

class IngestRequest(BaseModel):
    document_id: UUID
    version_id: UUID
    itenantid: int
    doctype: str
    file_type: str          # pdf|docx|txt|md|csv|json|png|jpeg|jpg|zip
    storage_url: Optional[str] = None
    raw_text: Optional[str] = None
    file_content: Optional[str] = None  # base64-encoded binary; stored in DB, no external storage needed
    sha256_hash: str
    chunking_override: Optional[dict] = None
    force_new_version: bool = False
    force_separate: bool = False

class IngestResponse(BaseModel):
    document_id: str
    version_id: str
    status: str             # always "queued" on success

class DocumentStatusResponse(BaseModel):
    document_id: str
    version_id: str
    status: str
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None

class SimilarityCheckRequest(BaseModel):
    itenantid: int
    category: str           # doctype name
    raw_text: str
    sha256_hash: str

class ArchiveVersionRequest(BaseModel):
    version_id: str
    itenantid: int

# ─── DMS-003 runtime constants (all overridable via environment) ───────────────

_raw_origins = os.environ.get('ALLOWED_STORAGE_ORIGINS', '')
ALLOWED_STORAGE_ORIGINS: List[str] = [o.strip() for o in _raw_origins.split(',') if o.strip()]

MAX_CHUNKS_PER_DOCUMENT = int(os.environ.get('MAX_CHUNKS_PER_DOCUMENT', 2000))
EMBED_BATCH_SIZE        = int(os.environ.get('EMBED_BATCH_SIZE', 100))

SUPPORTED_ZIP_EXTENSIONS = {'.pdf', '.docx', '.txt', '.md', '.csv', '.json',
                             '.png', '.jpeg', '.jpg'}

# SEC-A4
MIN_CHUNK_SIZE  = int(os.environ.get('MIN_CHUNK_SIZE', 64))
MAX_CHUNK_SIZE  = int(os.environ.get('MAX_CHUNK_SIZE', 2048))

# SEC-A6
INTERNAL_SECRET = os.environ.get('INTERNAL_API_SECRET', '')


def validate_ssrf_config():
    """
    SEC-A1: Warns at startup if ALLOWED_STORAGE_ORIGINS is not set.
    The check does NOT block startup because environments that use DB file
    storage (file_content field) never issue outbound HTTP fetches and
    therefore do not need this variable configured.
    The hard block lives in validate_storage_url(), which is called per-request
    only when a storage_url is actually provided — that is where SSRF matters.
    """
    if not ALLOWED_STORAGE_ORIGINS:
        logger.warning(
            "ALLOWED_STORAGE_ORIGINS is not set. Any request using "
            "storage_url will be rejected (HTTP 500). Set this variable "
            "if you need external file storage."
        )


validate_ssrf_config()

# ──────────────────────────────────────────────────────────────────────────────

def _apply_ollama_params(p: dict) -> dict:
    """Resolve base_url (/v1 suffix) and embed Basic auth credentials into the
    URL if not already present.  Credentials in the URL are handled by httpx at
    the transport level, which keeps them separate from the Bearer token that
    ChatOpenAI sets — both coexist without conflict."""
    from urllib.parse import urlparse, urlunparse
    base_url = p.pop('base_url', globals.config.get('ollama_base_url', 'http://localhost:11434'))
    if not base_url.rstrip('/').endswith('/v1'):
        base_url = f"{base_url.rstrip('/')}/v1"
    parsed = urlparse(base_url)
    # Embed credentials into URL if not already there and secrets are available
    if not parsed.username:
        ollama_user = globals.secret_data.get('OLLAMA_USERNAME')
        ollama_pwd  = globals.secret_data.get('OLLAMA_PASSWORD')
        if ollama_user and ollama_pwd:
            netloc = f"{ollama_user}:{ollama_pwd}@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            base_url = urlunparse(parsed._replace(netloc=netloc))
    p['base_url'] = base_url
    p.setdefault('api_key', 'ollama')
    return p

def get_llm(agent_config):
    """Create an LLM instance based on provider config.

    Provider resolution order:
      1. agent_config['model_config']['provider']  (per-agent override)
      2. globals.config['llm_provider']             (global default)
      3. 'openai'                                   (fallback)

    Auth for Ollama (either style works):
      - Credentials embedded in URL: https://user:pwd@ollama.example.com
      - Secret files: secrets/OLLAMA_USERNAME + secrets/OLLAMA_PASSWORD
    """
    model_config = agent_config['model_config']
    provider = model_config.get('provider', globals.config.get('llm_provider', 'openai'))
    params = model_config.get('params', {})

    if provider == 'openai':
        openai_params = dict(params)
        if 'base_url' in openai_params:
            # Custom base_url means it's pointing at Ollama (or another OpenAI-compatible server)
            openai_params = _apply_ollama_params(openai_params)
        return ChatOpenAI(**openai_params)
    elif provider == 'google':
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(**params)
    elif provider == 'ollama':
        return ChatOpenAI(**_apply_ollama_params(dict(params)))
    else:
        raise ValueError(f"Unsupported LLM provider: '{provider}'. Supported: openai, google, ollama")

# Helper functions
def load_vector_store(agentid: str,embedding_model:str):
    global embeddings
    logger.debug(f"Loading vector store for Agent ID: {agentid}")
    if globals.config["provider"]=="openai":
        
        embeddings[agentid] = OpenAIEmbeddings()
    else:
        embeddings[agentid] = HuggingFaceEmbeddings(model_name=embedding_model)
    cfg =  globals.config['connections'][0]
    return PGVector(
        embeddings=embeddings[agentid],
        collection_name=agentid,
        connection=db.get_connection_str('agent'),
        use_jsonb=True,
        engine_args=cfg["params"]
    )

def get_agent_config(agentid: str):
    return next((agent for agent in globals.config["agents"] if  agent['agent']==agentid), None)

def get_doctype_config(doctype_name: str) -> dict:
    """
    Resolves a doctype name to its full config entry from globals.config['doctypes'].
    Raises HTTPException 404 if the name is not found.
    """
    doctype = next(
        (d for d in globals.config.get('doctypes', []) if d['name'] == doctype_name),
        None,
    )
    if not doctype:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Doctype '{doctype_name}' not found in config. "
                "Add it to the 'doctypes' section in the agent config."
            ),
        )
    return doctype

def has_vectorstore(agent_config: dict) -> bool:
    """Returns True if the agent declares either the legacy or new vectorstore key."""
    return 'vectorstore' in agent_config or 'vectorstores' in agent_config


def normalise_vectorstore_config(agent_config: dict) -> list:
    """
    Returns a list of vectorstore entries regardless of whether the agent
    uses the legacy 'vectorstore' key (single dict) or the new 'vectorstores'
    key (array).  Never modifies the original config dict.
    """
    if 'vectorstores' in agent_config:
        return agent_config['vectorstores']
    elif 'vectorstore' in agent_config:
        vs = agent_config['vectorstore'].copy()
        vs.setdefault('label', 'default')
        vs.setdefault('collection', agent_config['agent'])  # legacy: agentid = collection
        return [vs]
    return []


def load_active_agents(config: dict) -> list:
    """
    Returns only agents where is_active is True or absent (default True).
    Logs a count of active vs archived agents.
    """
    all_agents = config.get('agents', [])
    active   = [a for a in all_agents if a.get('is_active', True)]
    archived = [a for a in all_agents if not a.get('is_active', True)]
    logger.info(
        "Agent config: %d active agents loaded, %d archived (skipped).",
        len(active), len(archived)
    )
    if archived:
        logger.info("Archived agents: %s", [a['agent'] for a in archived])
    return active


def warn_suspicious_tenant(agentid: str, itenantid, endpoint: str):
    """SEC-A5: Detective control — logs suspicious tenant IDs without blocking."""
    if not itenantid or (isinstance(itenantid, int) and itenantid <= 0):
        logger.warning(
            "[SEC-A5] Suspicious itenantid=%r on %s for agent '%s'. "
            "Review caller authentication.", itenantid, endpoint, agentid
        )


def validate_doctype_references():
    """
    Checks that:
      1. Every agent's is_active field (if present) is a boolean (DMS-007).
      2. Every agent with a vectorstore references a known doctype (DMS-002).
    Raises ValueError on first violation, blocking startup.
    """
    registered = {d['name'] for d in globals.config.get('doctypes', [])}
    for agent in globals.config.get('agents', []):
        # DMS-007: is_active must be a boolean if present
        if 'is_active' in agent and not isinstance(agent['is_active'], bool):
            raise ValueError(
                f"Agent '{agent['agent']}': 'is_active' must be a boolean "
                f"(true or false), got: {agent['is_active']!r}"
            )
        if not has_vectorstore(agent):
            continue
        doctype = agent.get('doctype')
        if doctype and doctype not in registered:
            raise ValueError(
                f"Agent '{agent['agent']}' references unknown doctype "
                f"'{doctype}'. Add it to the 'doctypes' section in config."
            )

def sync_doctypes_to_db():
    """
    Upserts the doctype registry from globals.config['doctypes'] into the
    agents.dms_doctypes table.  Called on every startup and reload so the DB
    stays in sync with the JSON config.
    Logs a warning and continues if the table does not exist yet (migration
    not yet applied).
    """
    from sqlalchemy.orm import Session
    from sqlalchemy import text as sa_text
    doctypes = globals.config.get('doctypes', [])
    if not doctypes:
        return
    try:
        with Session(globals.engine) as session:
            for d in doctypes:
                chunking = d.get('chunking', {})
                session.execute(sa_text("""
                    INSERT INTO agents.dms_doctypes
                        (name, collection, embedding_model, is_scanned, ocr_agent,
                         extraction_agent, chunking_strategy, chunk_size,
                         chunk_overlap, similarity_threshold)
                    VALUES
                        (:name, :collection, :embedding_model, :is_scanned, :ocr_agent,
                         :extraction_agent, :chunking_strategy, :chunk_size,
                         :chunk_overlap, :similarity_threshold)
                    ON CONFLICT (name) DO UPDATE SET
                        collection          = EXCLUDED.collection,
                        embedding_model     = EXCLUDED.embedding_model,
                        is_scanned          = EXCLUDED.is_scanned,
                        ocr_agent           = EXCLUDED.ocr_agent,
                        extraction_agent    = EXCLUDED.extraction_agent,
                        chunking_strategy   = EXCLUDED.chunking_strategy,
                        chunk_size          = EXCLUDED.chunk_size,
                        chunk_overlap       = EXCLUDED.chunk_overlap,
                        similarity_threshold= EXCLUDED.similarity_threshold
                """), {
                    'name':               d['name'],
                    'collection':         d['collection'],
                    'embedding_model':    d['embedding_model'],
                    'is_scanned':         d.get('is_scanned', False),
                    'ocr_agent':          d.get('ocr_agent'),
                    'extraction_agent':   d.get('extraction_agent'),
                    'chunking_strategy':  chunking.get('strategy', 'single'),
                    'chunk_size':         chunking.get('chunk_size'),
                    'chunk_overlap':      chunking.get('overlap') or 0,
                    'similarity_threshold': d.get('similarity_threshold', 0.85),
                })
            session.commit()
        logger.info("agents.dms_doctypes synced from config (%d entries)", len(doctypes))
    except Exception as exc:
        logger.warning(
            "agents.dms_doctypes sync skipped — table may not exist yet "
            "(run DMS-002 migration first): %s", exc
        )

# ─── SSRF guard ───────────────────────────────────────────────────────────────

def validate_storage_url(url: str):
    """Rejects storage URLs that are not on the ALLOWED_STORAGE_ORIGINS allowlist."""
    if not ALLOWED_STORAGE_ORIGINS:
        raise HTTPException(
            status_code=500,
            detail="ALLOWED_STORAGE_ORIGINS is not configured. Set it in the environment."
        )
    if not any(url.startswith(origin) for origin in ALLOWED_STORAGE_ORIGINS):
        raise HTTPException(
            status_code=400,
            detail="Untrusted storage URL. URL must start with a configured allowed origin."
        )

# ─── Relational DB helpers ────────────────────────────────────────────────────

def update_version_status(version_id: str, status: str,
                           error: str = None, extra: dict = None):
    """
    Updates version_status and optional columns in dms_document_versions.
    Requires DMS-006-add-status-columns.sql to have been run first.
    """
    set_parts = ['version_status = :status']
    params: dict = {'status': status, 'vid': version_id}
    if error:
        set_parts.append('error_message = :error_message')
        params['error_message'] = error
    if extra:
        if extra.get('ingested_at') == 'NOW()':
            set_parts.append('ingested_at = NOW()')
        if extra.get('chunk_count') is not None:
            set_parts.append('chunk_count = :chunk_count')
            params['chunk_count'] = extra['chunk_count']
    sql = f"UPDATE agents.dms_document_versions SET {', '.join(set_parts)} WHERE id = :vid::uuid"
    try:
        with Session(globals.engine) as session:
            session.execute(sa_text(sql), params)
            session.commit()
    except Exception as exc:
        logger.warning("update_version_status failed for %s: %s", version_id, exc)


def get_version_metadata(version_id: str) -> dict:
    """Returns the dms_document_versions row for the given UUID."""
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            SELECT id, document_id, version_number, classification,
                   allowed_roles, allowed_agents, org_unit_id, entity_tags,
                   document_date, version_status
            FROM agents.dms_document_versions
            WHERE id = :vid::uuid
        """), {'vid': version_id}).fetchone()
        return dict(row._mapping) if row else {}


def get_document_metadata(document_id: str) -> dict:
    """Returns the dms_documents row for the given UUID."""
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            SELECT id, itenantid, category_id, document_name, scope, created_by
            FROM agents.dms_documents WHERE id = :did::uuid
        """), {'did': document_id}).fetchone()
        return dict(row._mapping) if row else {}


def check_sha256_duplicate(document_id: str, sha256_hash: str, itenantid: int) -> Optional[str]:
    """Returns the existing version_id string if a SHA-256 duplicate exists for this tenant."""
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            SELECT v.id FROM agents.dms_document_versions v
            JOIN agents.dms_documents d ON d.id = v.document_id
            WHERE v.sha256_hash = :h AND d.itenantid = :t AND v.is_active = true
            LIMIT 1
        """), {'h': sha256_hash, 't': itenantid}).fetchone()
        return str(row[0]) if row else None


def get_version_status_from_db(document_id: str) -> Optional[dict]:
    """Returns latest version status row for a document UUID (DMS-006 columns required)."""
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            SELECT id AS version_id, version_status, error_message, chunk_count
            FROM agents.dms_document_versions
            WHERE document_id = :did::uuid
            ORDER BY created_at DESC LIMIT 1
        """), {'did': document_id}).fetchone()
        return dict(row._mapping) if row else None


def insert_chunks_to_db(version_id: str, chunks: list, chunking_config: dict):
    """Persists chunk records into agents.dms_chunks."""
    strategy = chunking_config.get('strategy', 'single')
    with Session(globals.engine) as session:
        for i, chunk_text in enumerate(chunks):
            page_number = i + 1 if strategy == 'page' else None
            session.execute(sa_text("""
                INSERT INTO agents.dms_chunks
                    (version_id, chunk_index, page_number, chunk_text, chunk_strategy)
                VALUES (:vid::uuid, :idx, :pnum, :txt, :strat)
            """), {
                'vid': version_id, 'idx': i,
                'pnum': page_number, 'txt': chunk_text, 'strat': strategy,
            })
        session.commit()


def soft_delete_vectors_for_version(version_id: str):
    """
    Sets is_active=false and version_status='archived' on all PGVector records
    for the given version_id.  Uses a direct SQL UPDATE on langchain_pg_embedding
    (JSONB merge); no delete-and-reinsert needed.
    """
    with Session(globals.engine) as session:
        session.execute(sa_text("""
            UPDATE langchain_pg_embedding
            SET cmetadata = cmetadata
                || '{"is_active": false, "version_status": "archived"}'::jsonb
            WHERE cmetadata->>'version_id' = :vid
        """), {'vid': version_id})
        session.commit()
    update_version_status(version_id, 'archived')
    logger.info("Soft-deleted vectors for version %s", version_id)


def create_child_document_records(parent_document_id: str, document_id: str,
                                   version_id: str, itenantid: int,
                                   document_name: str, storage_url: str,
                                   sha256_hash: str, classification: str,
                                   doctype: str):
    """Creates dms_documents + dms_document_versions rows for a child ZIP file."""
    with Session(globals.engine) as session:
        parent_row = session.execute(sa_text("""
            SELECT category_id, scope, created_by FROM agents.dms_documents
            WHERE id = :pid::uuid
        """), {'pid': parent_document_id}).fetchone()
        category_id = str(parent_row[0]) if parent_row and parent_row[0] else None
        scope       = parent_row[1] if parent_row else 'tenant'
        created_by  = parent_row[2] if parent_row else 0

        session.execute(sa_text("""
            INSERT INTO agents.dms_documents
                (id, itenantid, category_id, document_name, scope, created_by)
            VALUES (:id::uuid, :t, :cat::uuid, :name, :scope, :cb)
        """), {
            'id': document_id, 't': itenantid, 'cat': category_id,
            'name': document_name, 'scope': scope, 'cb': created_by,
        })
        session.execute(sa_text("""
            INSERT INTO agents.dms_document_versions
                (id, document_id, version_number, sha256_hash, classification,
                 version_status, is_active, storage_url, created_by)
            VALUES
                (:id::uuid, :did::uuid, 1, :h, :cls, 'draft', false, :url, :cb)
        """), {
            'id': version_id, 'did': document_id, 'h': sha256_hash,
            'cls': classification, 'url': storage_url, 'cb': created_by,
        })
        session.commit()


# ─── DB file storage (alternative to external object storage) ─────────────────

def store_file_to_db(version_id: str, file_bytes: bytes, file_extension: str) -> str:
    """
    Persists raw file bytes in agents.dms_file_content and returns the row UUID.
    Use when no external object storage is configured.
    Requires DMS-006b-add-file-content-table.sql to have been run.
    """
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            INSERT INTO agents.dms_file_content
                (version_id, content, file_extension, checksum)
            VALUES (:vid::uuid, :content, :ext, :checksum)
            RETURNING id::text
        """), {
            'vid':      version_id,
            'content':  file_bytes,
            'ext':      file_extension,
            'checksum': hashlib.sha256(file_bytes).hexdigest(),
        }).fetchone()
        session.commit()
        return str(row[0])


def fetch_file_from_db(version_id: str) -> Optional[bytes]:
    """Retrieves the most recent stored file bytes for a version."""
    with Session(globals.engine) as session:
        row = session.execute(sa_text("""
            SELECT content FROM agents.dms_file_content
            WHERE version_id = :vid::uuid
            ORDER BY created_at DESC LIMIT 1
        """), {'vid': version_id}).fetchone()
        return bytes(row[0]) if row else None


def get_file_bytes(request: IngestRequest) -> bytes:
    """
    Resolves raw file bytes from whichever source is configured.
    Priority: file_content (DB path) → storage_url (external fetch).
    Never called when raw_text is present.
    """
    if request.file_content:
        return base64.b64decode(request.file_content)
    if request.storage_url:
        return storage.fetch_from_storage(request.storage_url)
    raise ValueError(
        "No binary file source available. "
        "Provide storage_url or file_content."
    )


# ─── Internal agent dispatcher ────────────────────────────────────────────────

def run_agent_internal(agentid: str, data: dict) -> dict:
    """
    Calls an agent's pipeline function directly — no HTTP round-trip.
    Replicates the logic inside /agent without the FastAPI scaffolding.
    """
    agent_config = get_agent_config(agentid)
    if not agent_config:
        raise ValueError(f"Agent '{agentid}' not found in configuration.")
    agent_data = {}
    agent_data['input_data'] = data.get('input_data', data)
    agent_data['prerequisites'] = get_requisites(
        agentid, agent_config.get('prerequisites', []), agent_data)
    if 'vectorstore' in agent_config:
        itenantid = utils.get_var(agent_data, 'input_data.itenantid')
        agent_data['similarities'] = get_similarities(
            agentid, agent_config, agent_data, itenantid)
        agent_data['postrequisites'] = get_requisites(
            agentid, agent_config.get('postrequisites', []),
            agent_data['similarities'])
    response = get_chain_result(agentid, agent_config, agent_data)
    if agent_config.get('response_type', 'json') == 'json':
        return json.loads(response.content)
    return {'content': str(response)}


def call_ocr_agent(file_bytes: bytes, doctype_name: str, doctype_config: dict) -> str:
    """
    Encodes file_bytes as base64 and calls the configured OCR agent directly.
    Returns the extracted text string.
    """
    ocr_agent_id = doctype_config.get('ocr_agent')
    if not ocr_agent_id:
        raise ValueError(
            f"Doctype '{doctype_name}' has is_scanned=True but no ocr_agent configured."
        )
    b64 = base64.b64encode(file_bytes).decode('utf-8')
    result = run_agent_internal(ocr_agent_id, {
        'input_data': {'base64_img': b64, 'doc_type': doctype_name}
    })
    # Accept either answer.ocr_text or top-level ocr_text
    answer = result.get('answer', {})
    ocr_text = (
        (answer.get('ocr_text') if isinstance(answer, dict) else None)
        or result.get('ocr_text')
    )
    if not ocr_text:
        raise ValueError(
            f"OCR agent '{ocr_agent_id}' did not return an ocr_text field. "
            f"Response keys: {list(result.keys())}"
        )
    return ocr_text


# ─── Text extraction dispatcher ───────────────────────────────────────────────

def extract_text(request: IngestRequest, doctype_config: dict):
    """
    Returns the textual content of the document as a str (most formats)
    or list[str] (csv / json row strategy).
    Verifies SHA-256 when bytes are fetched from storage.
    """
    if request.raw_text:
        return request.raw_text

    file_bytes = get_file_bytes(request)   # handles file_content and storage_url

    actual_hash = hashlib.sha256(file_bytes).hexdigest()
    if actual_hash != request.sha256_hash:
        update_version_status(str(request.version_id), 'failed',
                              error='SHA-256 hash mismatch between request and fetched file.')
        raise ValueError('SHA-256 mismatch between request and fetched file.')

    ft = request.file_type.lower()

    if ft in ('txt', 'md'):
        return file_bytes.decode('utf-8', errors='replace')
    elif ft == 'pdf' and not doctype_config.get('is_scanned'):
        return utils.extract_pdf_text(file_bytes)
    elif ft == 'pdf' and doctype_config.get('is_scanned'):
        return call_ocr_agent(file_bytes, request.doctype, doctype_config)
    elif ft in ('png', 'jpeg', 'jpg'):
        return call_ocr_agent(file_bytes, request.doctype, doctype_config)
    elif ft == 'docx':
        return utils.extract_docx_text(file_bytes)
    elif ft == 'csv':
        return utils.extract_csv_rows(file_bytes)
    elif ft == 'json':
        return utils.extract_json_records(file_bytes)
    else:
        raise ValueError(f"Unsupported file_type: '{ft}'")


# ─── ZIP handler ──────────────────────────────────────────────────────────────

def handle_zip_ingest(request: IngestRequest, doctype_config: dict):
    """
    Explodes a ZIP archive and runs the full pipeline for each supported member.
    Unsupported extensions are logged and skipped.
    """
    file_bytes = get_file_bytes(request)   # handles file_content and storage_url
    results = []

    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        names = [n for n in zf.namelist() if not n.endswith('/')]
        if not names:
            update_version_status(str(request.version_id), 'failed',
                                  error='ZIP archive is empty.')
            return

        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext not in SUPPORTED_ZIP_EXTENSIONS:
                logger.warning(
                    "ZIP ingest: skipping unsupported file '%s' (ext '%s').", name, ext)
                results.append({'filename': name, 'status': 'skipped',
                                'reason': 'unsupported_type'})
                continue

            child_bytes   = zf.read(name)
            child_doc_id  = str(uuid.uuid4())
            child_ver_id  = str(uuid.uuid4())
            file_type     = ext.lstrip('.')
            child_hash    = hashlib.sha256(child_bytes).hexdigest()

            # Try external storage first; fall back to DB storage
            child_url = None
            child_file_content = None
            try:
                child_url = storage.store_to_storage(
                    child_bytes, f"{request.document_id}/{name}")
                if child_url:
                    validate_storage_url(child_url)  # SEC-A1: guard child URLs too
            except NotImplementedError:
                # No external storage configured — keep bytes in DB
                pass

            parent_meta = get_version_metadata(str(request.version_id))
            create_child_document_records(
                parent_document_id=str(request.document_id),
                document_id=child_doc_id,
                version_id=child_ver_id,
                itenantid=request.itenantid,
                document_name=name,
                storage_url=child_url,
                sha256_hash=child_hash,
                classification=parent_meta.get('classification', 'Internal'),
                doctype=request.doctype,
            )

            # For text files pass as raw_text; for binaries use file_content
            # (base64) so the child pipeline can work without external storage
            if file_type in ('txt', 'md'):
                raw_text_val    = child_bytes.decode('utf-8', errors='replace')
                file_content_val = None
            elif child_url is None:
                raw_text_val    = None
                file_content_val = base64.b64encode(child_bytes).decode('ascii')
            else:
                raw_text_val    = None
                file_content_val = None

            child_request = IngestRequest(
                document_id=uuid.UUID(child_doc_id),
                version_id=uuid.UUID(child_ver_id),
                itenantid=request.itenantid,
                doctype=request.doctype,
                file_type=file_type,
                storage_url=child_url,
                raw_text=raw_text_val,
                file_content=file_content_val,
                sha256_hash=child_hash,
            )
            run_ingest_pipeline(child_request, doctype_config)
            results.append({'filename': name, 'status': 'queued',
                            'document_id': child_doc_id})

    valid_count = sum(1 for r in results if r['status'] == 'queued')
    if valid_count == 0:
        update_version_status(str(request.version_id), 'failed',
                              error='No supported files found in ZIP.')
    else:
        update_version_status(str(request.version_id), 'ready',
                              extra={'zip_children': valid_count})


# ─── Core ingest pipeline (runs in background) ────────────────────────────────

def run_ingest_pipeline(request: IngestRequest, doctype_config: dict):
    """
    Full ingestion pipeline.  Called by BackgroundTasks; never awaited by the
    endpoint, so it can freely use blocking I/O.

    Status progression:
        queued → extracting → chunking → embedding → pending_approval
        (or → failed on any error)
    """
    try:
        # STEP 1 — SHA-256 dedup check
        existing = check_sha256_duplicate(
            str(request.document_id), request.sha256_hash, request.itenantid)
        if existing and not request.force_new_version and not request.force_separate:
            update_version_status(str(request.version_id), 'ready')
            logger.info("Version %s deduplicated from %s", request.version_id, existing)
            return

        # STEP 2 — Persist file bytes to DB if sent as file_content
        # This gives a durable copy for audit / re-processing even when
        # no external object storage is configured.
        if request.file_content and not request.raw_text:
            try:
                store_file_to_db(
                    str(request.version_id),
                    base64.b64decode(request.file_content),
                    request.file_type,
                )
            except Exception as exc:
                logger.warning(
                    "Could not persist file_content to DB for version %s "
                    "(table may not exist yet — run DMS-006b migration): %s",
                    request.version_id, exc
                )

        # STEP 3 — ZIP unpacking (handled separately; exit early)
        if request.file_type.lower() == 'zip':
            handle_zip_ingest(request, doctype_config)
            return

        # STEP 4 — Extract text
        update_version_status(str(request.version_id), 'extracting')
        raw_text = extract_text(request, doctype_config)

        # STEP 5 — Prompt injection scan (flag only; do not block)
        injection_detected = utils.check_injection(
            raw_text if isinstance(raw_text, str) else ' '.join(raw_text))
        if injection_detected:
            logger.warning(
                "Prompt injection pattern detected in version %s", request.version_id)

        # STEP 5 — Chunk
        update_version_status(str(request.version_id), 'chunking')
        chunking_config = (
            request.chunking_override
            if request.chunking_override
            else doctype_config['chunking']
        )
        chunks = utils.apply_chunking(raw_text, chunking_config)

        # STEP 6 — Chunk count guard
        if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
            msg = (f"Document produces {len(chunks)} chunks, "
                   f"exceeds limit of {MAX_CHUNKS_PER_DOCUMENT}.")
            update_version_status(str(request.version_id), 'failed', error=msg)
            return

        # STEP 7 — Embed in batches
        update_version_status(str(request.version_id), 'embedding')
        doctype_key = request.doctype
        embedder = embeddings.get(doctype_key) or embeddings.get(
            doctype_config.get('collection'))
        if embedder is None:
            raise ValueError(
                f"No embedding model loaded for doctype '{doctype_key}'. "
                "Run /reloadconfig or restart the app."
            )

        all_vectors: list = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i:i + EMBED_BATCH_SIZE]
            all_vectors.extend(utils.embed_with_retry(batch, embedder))

        # STEP 8 — Build metadata payload
        version_meta = get_version_metadata(str(request.version_id))
        base_metadata = {
            'document_id':      str(request.document_id),
            'version_id':       str(request.version_id),
            'version_number':   version_meta.get('version_number'),
            'version_status':   'active',
            'chunk_strategy':   chunking_config.get('strategy', 'single'),
            'itenantid':        request.itenantid,
            'scope':            'tenant',
            'category':         request.doctype,
            'classification':   version_meta.get('classification', 'Internal'),
            'allowed_roles':    version_meta.get('allowed_roles', []),
            'allowed_agents':   version_meta.get('allowed_agents', ['ALL_AGENTS']),
            'org_unit_id':      version_meta.get('org_unit_id'),
            'entity_tags':      version_meta.get('entity_tags', []),
            'document_date':    str(version_meta['document_date'])
                                if version_meta.get('document_date') else None,
            'source_file_type': request.file_type,
            'is_ocr':           bool(doctype_config.get('is_scanned', False)),
            'is_active':        True,
            'injection_flag':   injection_detected,
        }

        # STEP 9 — Insert into PGVector
        vs = vector_store.get(doctype_key) or vector_store.get(
            doctype_config.get('collection'))
        if vs is None:
            raise ValueError(
                f"No vector store loaded for doctype '{doctype_key}'.")

        documents: list = []
        for i, chunk_text in enumerate(chunks):
            meta = {**base_metadata, 'chunk_index': i}
            if chunking_config.get('strategy') == 'page':
                meta['page_number'] = i + 1
            documents.append(Document(page_content=chunk_text, metadata=meta))

        ids = [f"{request.version_id}_{i}" for i in range(len(chunks))]
        vs.add_documents(documents, ids=ids)

        # STEP 10 — Persist chunks to relational DB
        insert_chunks_to_db(str(request.version_id), chunks, chunking_config)

        # STEP 11 — Final status update
        update_version_status(
            str(request.version_id), 'pending_approval',
            extra={'chunk_count': len(chunks), 'ingested_at': 'NOW()'}
        )
        logger.info(
            "Ingest complete: version=%s doctype=%s chunks=%d",
            request.version_id, request.doctype, len(chunks)
        )

    except Exception as exc:
        update_version_status(str(request.version_id), 'failed', error=str(exc))
        logger.error(
            "Ingest pipeline failed for version %s: %s",
            request.version_id, exc, exc_info=True
        )


# ──────────────────────────────────────────────────────────────────────────────

def validate_input_fields(data: Dict[str, Any], required_fields: list):
    
    for field in required_fields:
        val=utils.get_var(data,field)
        if val  is None:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

def validate_agent_config_and_vector_store(agentid: str):
    agent_config = get_agent_config(agentid)
    if not agent_config:
        raise HTTPException(status_code=404, detail=f"Agent ID '{agentid}' not found in the configuration.")
    if has_vectorstore(agent_config):
        vs_entries  = normalise_vectorstore_config(agent_config)
        primary_col = vs_entries[0]['collection'] if vs_entries else agentid
        if primary_col not in vector_store and agentid not in vector_store:
            raise HTTPException(status_code=404, detail=f"No vector store found for Agent ID '{agentid}'.")
    return agent_config

def initialize_vector_stores(agentname=None):
    logger.debug(globals.config)
    validate_doctype_references()   # includes is_active type check (DMS-007)
    sync_doctypes_to_db()

    active_agents = load_active_agents(globals.config)   # DMS-007 filter
    cfg = globals.config['connections'][0]

    # ── itenantid casing validation across all vectorstore entries ────────────
    for agent in active_agents:
        for vs_config in normalise_vectorstore_config(agent):
            for key in vs_config.get('metadata', []):
                if key.lower() == 'itenantid' and key != 'itenantid':
                    raise ValueError(
                        f"Agent '{agent['agent']}': tenant key must be "
                        f"'itenantid' (found '{key}'). Fix agent config."
                    )

    # ── Determine which collections to (re)load ───────────────────────────────
    if agentname:
        target_agent       = next((a for a in active_agents if a['agent'] == agentname), None)
        target_collections = (
            {vs['collection'] for vs in normalise_vectorstore_config(target_agent)}
            if target_agent else set()
        )
    else:
        target_collections = None  # None = load everything

    # ── Collect unique (collection → embedding_model) across all active agents ─
    collection_models: dict = {}
    for agent in active_agents:
        for vs_config in normalise_vectorstore_config(agent):
            col = vs_config['collection']
            emb_model = vs_config.get('embedding_model', '')
            if col not in collection_models and emb_model:
                collection_models[col] = emb_model

    # ── Load one PGVector instance per collection ─────────────────────────────
    for collection, emb_model in collection_models.items():
        if target_collections is not None and collection not in target_collections:
            continue
        if globals.config["provider"] == "openai":
            emb = OpenAIEmbeddings()
        else:
            emb = HuggingFaceEmbeddings(model_name=emb_model)
        embeddings[collection] = emb
        vector_store[collection] = PGVector(
            embeddings=emb,
            collection_name=collection,
            connection=db.get_connection_str('agent'),
            use_jsonb=True,
            engine_args=cfg["params"],
        )

    # ── Agent-ID aliases for backward compatibility ───────────────────────────
    for agent in active_agents:
        vs_entries = normalise_vectorstore_config(agent)
        if vs_entries:
            primary = vs_entries[0]['collection']
            if primary in vector_store:
                embeddings[agent['agent']] = embeddings[primary]
                vector_store[agent['agent']] = vector_store[primary]

    # ── Doctype-name aliases (DMS-003 / DMS-005) ──────────────────────────────
    for doctype in globals.config.get('doctypes', []):
        name       = doctype['name']
        collection = doctype['collection']
        if name not in embeddings:
            if collection in embeddings:
                embeddings[name]   = embeddings[collection]
                vector_store[name] = vector_store[collection]
            else:
                emb_model = doctype['embedding_model']
                if globals.config["provider"] == "openai":
                    emb = OpenAIEmbeddings()
                else:
                    emb = HuggingFaceEmbeddings(model_name=emb_model)
                embeddings[name] = emb
                vector_store[name] = PGVector(
                    embeddings=emb,
                    collection_name=collection,
                    connection=db.get_connection_str('agent'),
                    use_jsonb=True,
                    engine_args=cfg["params"],
                )

    logger.debug("Vector stores initialized: %s", list(vector_store.keys()))

initialize_vector_stores()




# Define prompt template and retrieve from config
def get_prompt_template(config):
    prompt_config = config.get("prompt_template", {})
    return PromptTemplate(
        input_variables=prompt_config.get("input", [
            "policy", "case_attributes", "policy_action", "policy_remarks", "similar_cases"
        ]),
        template=prompt_config.get("template", """
            You are an expert in fraud detection policies and case analysis. Below is the policy document:

            {policy}

            A new case has the following attributes:
            {case_attributes}

            Policy-based suggestion:
            Action: {policy_action}
            Remarks: {policy_remarks}

            From the vector similarity results, here are the most relevant historical cases:
            {similar_cases}

            Based on the policy suggestion and the historical cases, decide the most appropriate action and provide remarks.

            Output your response in the format:
            Action: <action>
            Remarks: <remarks>
            """)
    )

def get_similarities(agentid, agentconfig, data, itenantid):
    """
    Runs similarity searches for every vectorstore entry in the agent config.
    Returns a dict keyed by label, e.g. {'default': [...]} or
    {'similar_cases': [...], 'policy_chunks': [...]}.
    Returns {} when the agent has no vectorstore.
    """
    vs_entries = normalise_vectorstore_config(agentconfig)
    if not vs_entries:
        return {}

    results = {}
    for vs_config in vs_entries:
        label      = vs_config.get('label', 'default')
        collection = vs_config['collection']

        # Build query string from declared data fields
        query_parts = [
            f"{key}: {utils.get_var(data, key)}"
            for key in vs_config.get('data', [])
            if utils.get_var(data, key) is not None
        ]
        if not query_parts:
            results[label] = []
            continue
        query = ", ".join(query_parts)

        # Resolve embedder: prefer collection-level, fall back to agent-level
        embedder = embeddings.get(collection) or embeddings.get(agentid)
        if not embedder:
            logger.warning(
                "No embedder for collection '%s' or agent '%s'. Skipping.",
                collection, agentid
            )
            results[label] = []
            continue

        query_vector = embedder.embed_query(query)

        # Mandatory filters (DMS-001)
        search_filter = {'itenantid': itenantid, 'is_active': True}
        for field in vs_config.get('filter', []):
            val = utils.get_var(data, f'input_data.{field}')
            if val is not None:
                search_filter[field] = val

        vs = vector_store.get(collection)
        if not vs:
            logger.warning("No vector store for collection '%s'. Skipping.", collection)
            results[label] = []
            continue

        k = vs_config.get('cnt_similarities') or vs_config.get('k', 2)
        raw_docs = vs.similarity_search_by_vector(query_vector, k=k, filter=search_filter)

        # Scope post-filter (None = legacy records, treated as valid)
        docs = [d for d in raw_docs
                if d.metadata.get('scope') in ('tenant', 'platform', None)]

        label_results = []
        for doc in docs:
            suggestion = {'docid': doc.id}
            for field in vs_config.get('metadata', []):
                suggestion[field] = doc.metadata.get(field, 'N/A')
            label_results.append(suggestion)

        results[label] = label_results

    return results

def get_chain_result(agentid,agent_config,agent_data):
    prompt = get_prompt_template(agent_config)
    inputs={}
    for field in agent_config['prompt_template']['input']:
        inputs[field['name']]=utils.get_var(agent_data,field['key'])
    # Initialize LLM and chain
    filled_prompt = prompt.format(**inputs) 
    llm = get_llm(agent_config)
    #chain = prompt | llm
    message_content =[{
        "type": "text",
        "text": filled_prompt
    }]
    # Check if 'data' block is provided in model_config
    if 'data' in agent_config['model_config']:
        # Clone the data config
        additional_content = dict(agent_config['model_config']['data'])

        # Fill in actual binary or base64 data
        additional_content["data"] = utils.get_var(agent_data, agent_config['model_config']['data_key'])

        # Append to message content
        message_content.append(additional_content)

    # Final message
    message = {
        "role": agent_config['model_config']['role'],
        "content": message_content
    }
    logger.debug(f'message:{message}')
    # Combine into a single query for the LLM
    response = llm.invoke([message])
    return response

def get_requisites(agentid,req_config,agent_data):
    switcher = {
            'DB':utils.dbdata,
            'ROCKS':utils.rocksdata,
            'API': utils.apidata,
            'REDIS':utils.redisdata,
            'MEM': utils.memdata           
        } 
    
    data_prep={}
    
    for datastore in req_config: ##TO Change to Datastore
        try:
            func = switcher.get((datastore['type']).upper(), lambda: 'Invalid Store type')      
            if ('section' in  datastore) : 
                if datastore['section'] not in data_prep:                    
                    data_prep[datastore['section']]={}
                data_prep[datastore['section']][datastore['name']]=func( agent_data,datastore)
            else:    
                data_prep[datastore['name']]=func( agent_data,datastore)
            logger.debug(f'get_requisites:{data_prep}')    
        except Exception as ex:  
            logger.error(f"{agentid}-get_prequisites:error while processing, : {datastore['name']}", exc_info=ex)
       
    return data_prep    

@app.post("/agent")
async def agent_ai(request: DynamicRequest,username: str = Depends(get_current_username)):
    agentid = request.agentid
    
    agent_config = validate_agent_config_and_vector_store(agentid)
    logger.debug(f'Loaded agent config:{agent_config}')
    agent_data={}
    agent_data['input_data']=request.data
    
    agent_data['prerequisites']=get_requisites(agentid,agent_config['prerequisites'],agent_data)
    logger.debug(f'After prerequisites:{agent_data}')
    if has_vectorstore(agent_config):
        itenantid = utils.get_var(agent_data, 'input_data.itenantid')
        raw_sims  = get_similarities(agentid, agent_config, agent_data, itenantid)
        # Backward-compat shim: single-vectorstore agents get a flat list so
        # existing prompt templates that iterate similarities work unchanged.
        if list(raw_sims.keys()) == ['default']:
            agent_data['similarities'] = raw_sims['default']
        else:
            agent_data['similarities'] = raw_sims
        logger.debug(f'After similarities:{agent_data}')
        agent_data['postrequisites']=get_requisites(agentid,agent_config['postrequisites'],agent_data["similarities"])
        logger.debug(f'After postrequisites:{agent_data}')
    response=get_chain_result(agentid,agent_config,agent_data)
    logger.debug(response)
    if agent_config.get('response_type','json')=='json':
        json_response=json.loads(response.content)
    else: json_response =response    
    return json_response    


 
@app.post("/add_to_vectorstore")
async def add_to_vectorstore(request: DynamicRequest,username: str = Depends(get_current_username)):
    agentid = request.agentid

    data={}
    data['input_data']=request.data

    # SEC-A5: detective log for suspicious tenant values
    itenantid_val = utils.get_var(data, 'input_data.itenantid')
    warn_suspicious_tenant(agentid, itenantid_val, '/add_to_vectorstore')

    agent_config = validate_agent_config_and_vector_store(agentid)
    
    inputs = agent_config["vectorstore"]["data"]
    metadata_keys = [f'input_data.{key}' for key in agent_config["vectorstore"]["metadata"]]
    
    id_field = f'input_data.{agent_config["doctype"]}'
    
    validate_input_fields(data, inputs + metadata_keys + [id_field])
    logger.debug(data)
    # Prepare document content and base metadata
    page_content = ", ".join([f"{key}: {utils.get_var(data,key)}" for key in inputs])
    metadata = {key: utils.get_var(data,key) for key in metadata_keys}
    doc_id = utils.get_var(data, id_field)

    # Resolve doctype chunking config and split content into chunks
    doctype_config = get_doctype_config(agent_config["doctype"])
    chunks = utils.apply_chunking(page_content, doctype_config['chunking'])

    # Build one Document per chunk; single-strategy yields exactly one chunk
    # so behaviour is identical to the previous single-document insert.
    documents = []
    ids = []
    for i, chunk_text in enumerate(chunks):
        chunk_metadata = {**metadata, 'chunk_index': i}
        documents.append(Document(page_content=chunk_text, metadata=chunk_metadata))
        ids.append(f"{doc_id}_{i}")

    vector_store[agentid].add_documents(documents, ids=ids)
    return {"message": "Case added successfully!"}

@app.post("/suggest_action")
async def suggest_action(request: DynamicRequest,username: str = Depends(get_current_username)):
    agentid = request.agentid
    data={}
    data['input_data']=request.data
    #data = request.data

    agent_config = validate_agent_config_and_vector_store(agentid)

    inputs = agent_config["input_data"]
    metadata_fields = [f'input_data.{key}' for key in agent_config["metadata"]]
   

    validate_input_fields(data, inputs)

    query = ", ".join([f"{key}: {utils.get_var(data,key)}" for key in inputs])
    query_vector = embeddings.embed_query(query)

    results = vector_store[agentid].similarity_search_by_vector(query_vector, k=3)

    if not results:
        raise HTTPException(status_code=404, detail="No similar cases found.")
    suggestions=[]
    for result in results:
        suggestion={}
        suggestion["context"]=result.page_content
        for field in metadata_fields:
            suggestion[field]= result.metadata.get(field, "N/A") 
        suggestions.append(suggestion)    

    return {"similar_cases": suggestions}

def get_policy_from_db(agent_id):
    
    query="SELECT policy_text FROM agents.policy_documents WHERE agent_id = :agentid ORDER BY created_at DESC LIMIT 1"
        
    result = db.get_data1(query,{'agentid':agent_id})
    
    if not result.empty:
        return result.iloc[0]["policy_text"]  # policy_text from the RealDictCursor
    else:
        raise ValueError(f"No policy found for agent_id: {agent_id}")
    
@app.post("/recommend_action")
async def recommend_action(request: DynamicRequest,username: str = Depends(get_current_username)):
    agentid = request.agentid
    data={}
    data['input_data']=request.data


    agent_config = validate_agent_config_and_vector_store(agentid)

    # Format case attributes using config inputs
    case_attributes = ", ".join([f"{key}: {utils.get_var(data,key)}" for key in agent_config["input_data"]])

    # Policy-based suggestion
    #policy_suggestion = policy_suggest_action(data)

    # Vector similarity suggestion
    query = case_attributes
    query_vector = embeddings.embed_query(query)
    results = vector_store[agentid].similarity_search_by_vector(query_vector, k=3)

    if  results:
        # Format similar cases
        similar_cases = "\n".join(
        [
            f"- ID: {result.metadata[agent_config['id']]}, " +
            ", ".join([f"{field}: {result.metadata[field]}" for field in agent_config["metadata"]]) +
            f", Context: {result.page_content}"
            for result in results
        ]
        )

    prompt = get_prompt_template(agent_config)
    policy = get_policy_from_db(agentid)
    # Initialize LLM and chain
    llm = get_llm(agent_config)
    chain = prompt | llm
    inputs = {
        "policy": policy,  # Assuming `policy_document` is loaded dynamically
        "case_attributes": case_attributes,
        "similar_cases": similar_cases
    }
    # Combine into a single query for the LLM
    response = chain.invoke(inputs)
    json_response=json.loads(response)
    return json_response

# ─── DMS-003 endpoints ────────────────────────────────────────────────────────

@app.post("/ingest_document", response_model=IngestResponse)
async def ingest_document(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    username: str = Depends(get_current_username),
):
    """
    Accepts an ingest request, returns {status: "queued"} immediately,
    and runs the full pipeline in a background task.
    """
    # SEC-A5: detective log for suspicious tenant values
    warn_suspicious_tenant('ingest_document', request.itenantid, '/ingest_document')

    # Validate inputs before accepting
    if not request.storage_url and not request.raw_text and not request.file_content:
        raise HTTPException(400, "One of storage_url, raw_text, or file_content is required.")
    if request.storage_url:
        validate_storage_url(request.storage_url)   # SSRF check only when URL is used

    # Fail fast if doctype is unknown
    doctype_config = get_doctype_config(request.doctype)

    # SEC-A4: validate chunking_override bounds before queuing
    if request.chunking_override:
        co       = request.chunking_override
        strategy = co.get('strategy', '')
        cs       = co.get('chunk_size')
        overlap  = co.get('overlap', 0)
        if strategy in ('token', 'character'):
            if cs is None:
                raise HTTPException(
                    400, f"chunking_override with strategy '{strategy}' requires chunk_size.")
            if not (MIN_CHUNK_SIZE <= cs <= MAX_CHUNK_SIZE):
                raise HTTPException(
                    400, f"chunk_size {cs} is outside allowed range "
                         f"[{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}].")
            if overlap >= cs:
                raise HTTPException(
                    400, f"overlap ({overlap}) must be less than chunk_size ({cs}).")

    # Pre-validate chunk count for raw_text + override combinations (AC-4.6)
    if request.raw_text and request.chunking_override:
        estimated_chunks = utils.apply_chunking(request.raw_text, request.chunking_override)
        if len(estimated_chunks) > MAX_CHUNKS_PER_DOCUMENT:
            raise HTTPException(
                400,
                f"Document produces {len(estimated_chunks)} chunks, "
                f"exceeds limit of {MAX_CHUNKS_PER_DOCUMENT}."
            )

    update_version_status(str(request.version_id), 'queued')
    background_tasks.add_task(run_ingest_pipeline, request, doctype_config)

    return IngestResponse(
        document_id=str(request.document_id),
        version_id=str(request.version_id),
        status='queued',
    )


@app.get("/document_status/{document_id}", response_model=DocumentStatusResponse)
async def document_status(
    document_id: str,
    username: str = Depends(get_current_username),
):
    """Returns the current processing status for the most recent version of a document."""
    row = get_version_status_from_db(document_id)
    if not row:
        raise HTTPException(
            404, f"No version record found for document_id '{document_id}'.")
    return DocumentStatusResponse(
        document_id=document_id,
        version_id=str(row['version_id']),
        status=row['version_status'],
        chunk_count=row.get('chunk_count') or None,
        error_message=row.get('error_message'),
    )


@app.post("/check_similarity")
async def check_similarity(
    request: SimilarityCheckRequest,
    username: str = Depends(get_current_username),
):
    """
    Near-duplicate detection: embeds the supplied text and searches for similar
    active vectors in the doctype's collection.
    """
    doctype_config = get_doctype_config(request.category)
    threshold = float(doctype_config.get('similarity_threshold', 0.85))

    vs = vector_store.get(request.category) or vector_store.get(
        doctype_config.get('collection'))
    if vs is None:
        raise HTTPException(
            404, f"No vector store loaded for category '{request.category}'.")

    results = vs.similarity_search_with_score(
        request.raw_text[:5000],
        k=3,
        filter={'itenantid': request.itenantid, 'is_active': True},
    )

    matches = []
    for doc, score in results:
        if float(score) >= threshold:
            matches.append({
                'document_id':    doc.metadata.get('document_id'),
                'document_name':  doc.metadata.get('document_name', ''),
                'version_number': doc.metadata.get('version_number'),
                'similarity_score': round(float(score), 4),
            })

    return {
        'has_near_duplicates': len(matches) > 0,
        'threshold': threshold,
        'matches': matches,
    }


@app.post("/archive_version")
async def archive_version(
    request: ArchiveVersionRequest,
    username: str = Depends(get_current_username),
):
    """
    Soft-deletes all PGVector records for the given version_id (sets
    is_active=false).  Called by Spring Boot during Maker-Checker approval.
    """
    version = get_version_metadata(request.version_id)
    if not version:
        raise HTTPException(404, f"Version '{request.version_id}' not found.")

    doc = get_document_metadata(str(version['document_id']))
    if not doc or doc.get('itenantid') != request.itenantid:
        raise HTTPException(403, "Version does not belong to this tenant.")

    soft_delete_vectors_for_version(request.version_id)
    return {'version_id': request.version_id, 'status': 'archived'}


# ─── SEC-A6 — Tenant offboarding (internal only) ──────────────────────────────

@app.delete("/internal/tenant_vectors/{itenantid}")
async def delete_tenant_vectors(
    itenantid: int,
    x_internal_secret: Optional[str] = Header(None),
):
    """
    Hard-deletes all PGVector records for a deactivated tenant.
    Protected by the INTERNAL_API_SECRET header — not for external exposure.
    This is a HARD DELETE. Only call after formal tenant offboarding with
    confirmed data-retention obligations met.  All calls are logged at WARNING.
    """
    if not INTERNAL_SECRET or x_internal_secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden.")

    collections = list({d['collection'] for d in globals.config.get('doctypes', [])})
    deleted_counts: dict = {}

    with Session(globals.engine) as session:
        for collection in collections:
            result = session.execute(sa_text("""
                DELETE FROM langchain_pg_embedding
                WHERE collection_id = (
                    SELECT uuid FROM langchain_pg_collections
                    WHERE name = :col
                )
                AND cmetadata->>'itenantid' = :tid
            """), {'col': collection, 'tid': str(itenantid)})
            deleted_counts[collection] = result.rowcount
        session.commit()

    total = sum(deleted_counts.values())
    logger.warning(
        "[SEC-A6] Tenant offboarding: hard-deleted %d vectors for itenantid=%d. "
        "Per collection: %s", total, itenantid, deleted_counts
    )
    return {"itenantid": itenantid, "total_deleted": total, "by_collection": deleted_counts}


# ──────────────────────────────────────────────────────────────────────────────

@app.post('/reloadconfig')
def reload_config(username: str = Depends(get_current_username),agentname: str = Query(default=None)):
    
    db.add_config('DIA',globals.config['appname'])
    initialize_vector_stores(agentname)
    return "Done"

