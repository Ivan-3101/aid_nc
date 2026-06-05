"""
Session-wide fixtures using the REAL agent-config-example.json.
Heavy startup deps (DB, PGVector, HuggingFace) are patched so the
TestClient can be built without a live database.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Env vars must be set before any import of app.py ─────────────────────────
os.environ.setdefault("ALLOWED_STORAGE_ORIGINS",
                      "http://test-bucket.example.com,https://test-bucket.example.com")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-00000000000000000000000000000000")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-abc123")
os.environ.setdefault("MAX_CHUNKS_PER_DOCUMENT", "2000")
os.environ.setdefault("EMBED_BATCH_SIZE", "100")
os.environ.setdefault("MIN_CHUNK_SIZE",  "64")
os.environ.setdefault("MAX_CHUNK_SIZE",  "2048")

# ── Load the real agent config ────────────────────────────────────────────────
_REPO_ROOT  = Path(__file__).parent.parent
_CFG_FILE   = _REPO_ROOT / "agent-config-example.json"
_EXAMPLE    = json.loads(_CFG_FILE.read_text())

# Agents and doctypes exactly as defined in the project config
REAL_AGENTS  = _EXAMPLE["agents"]
REAL_DOCTYPES = _EXAMPLE["doctypes"]

# Collections that need a vector store at startup
# (agents that have a vectorstore section)
VS_AGENTS = [a for a in REAL_AGENTS if "vectorstore" in a or "vectorstores" in a]
VS_COLLECTIONS = list({
    a.get("vectorstore", {}).get("collection", a["agent"])
    for a in VS_AGENTS
})

REAL_GLOBALS_CONFIG = {
    "appname":       "agentpn",
    "provider":      "huggingface",
    "llm_provider":  "openai",
    "ollama_base_url": "https://ollama.example.com",
    "connections":   [{"name": "txndb1", "params": {}}],
    "agents":        REAL_AGENTS,
    "doctypes":      REAL_DOCTYPES,
}

REAL_SECRET_DATA = {
    "restuser":      "testuser",
    "restpwd":       "testpwd",
    "OPENAI_API_KEY": "sk-test-00000000000000000000000000000000",
    "txndb1":        "postgresql+psycopg://test:test@localhost:5432/",
    "agent":         "postgresql+psycopg://test:test@localhost:5432/",
    "DBUSER":        "test",
    "DBPWD":         "test",
}


def _make_mock_vs():
    """Returns a fresh mock PGVector instance."""
    vs = MagicMock()
    vs.similarity_search_by_vector.return_value  = []
    vs.similarity_search_with_score.return_value = []
    vs.add_documents.return_value = None
    return vs


def _make_mock_embedder():
    """Returns a fresh mock embedder."""
    emb = MagicMock()
    emb.embed_query.return_value     = [0.1] * 384
    emb.embed_documents.return_value = [[0.1] * 384]
    return emb


@pytest.fixture(scope="session")
def real_config():
    """The full agent config loaded from agent-config-example.json."""
    return REAL_GLOBALS_CONFIG


@pytest.fixture(scope="session")
def test_client():
    """
    FastAPI TestClient with all heavy deps patched.
    Vector stores and embedders are wired for every real agent collection.
    Session-scoped so the app is imported only once per run.
    """
    mock_engine  = MagicMock()
    mock_vs      = _make_mock_vs()
    mock_embedder = _make_mock_embedder()

    import globals as g_module

    with (
        patch.object(g_module, "startup"),
        patch.object(g_module, "set_config_params"),
        patch.object(g_module, "create_logs", return_value=(MagicMock(), None)),
        patch("db.load_session",        return_value=mock_engine),
        patch("db.add_config"),
        patch("db.get_connection_str",  return_value="postgresql+psycopg://test:test@localhost/testdb"),
        patch("langchain_postgres.vectorstores.PGVector", return_value=mock_vs),
        patch("langchain_huggingface.HuggingFaceEmbeddings", return_value=mock_embedder),
        patch("langchain_openai.OpenAIEmbeddings",           return_value=mock_embedder),
        patch("app.initialize_vector_stores"),
        patch("app.validate_ssrf_config"),
        patch("app.sync_doctypes_to_db"),
    ):
        g_module.config      = REAL_GLOBALS_CONFIG
        g_module.secret_data = REAL_SECRET_DATA
        g_module.engine      = mock_engine
        g_module.logger      = MagicMock()
        g_module.dbs         = {"txndb1": {"engine": mock_engine,
                                            "name": "txndb1", "params": {}}}

        import importlib
        import app as app_module
        importlib.reload(app_module)

        # Wire mock vector stores + embedders for every real agent collection
        for agent in VS_AGENTS:
            aid = agent["agent"]
            col = agent.get("vectorstore", {}).get("collection", aid)
            app_module.vector_store[aid] = mock_vs
            app_module.vector_store[col] = mock_vs
            app_module.embeddings[aid]   = mock_embedder
            app_module.embeddings[col]   = mock_embedder

        # Also wire doctype-keyed entries (DMS-003/005)
        for dt in REAL_DOCTYPES:
            app_module.vector_store[dt["name"]]  = mock_vs
            app_module.embeddings[dt["name"]]    = mock_embedder
            app_module.vector_store[dt["collection"]] = mock_vs
            app_module.embeddings[dt["collection"]]   = mock_embedder

        from fastapi.testclient import TestClient
        client = TestClient(app_module.app, raise_server_exceptions=False)
        client._app_module = app_module
        yield client
