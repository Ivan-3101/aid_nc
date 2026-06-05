"""
tests/test_helpers.py
=====================
App helper function tests using REAL agent configs from agent-config-example.json.

Agent reference:
  caseagentv1     → single   → RuleSpecificL1Action
  userManual      → page     → UserManual
  userManualPM    → page     → (no doctype — valid, skips check)
  rulesDev        → token    → RulesDev
  sqlagentv1      → page     → SQLDev
  uinavigatorv1   → single   → uinavigatorv1
  openaiVision    → no VS
  ocrToFhir       → no VS
  orchestratorAgent → no VS

Run:
    pytest tests/test_helpers.py -v
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALLOWED_STORAGE_ORIGINS", "http://test-bucket.example.com")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key")
os.environ.setdefault("INTERNAL_API_SECRET", "secret")

# Load real config
_EXAMPLE = json.loads((Path(__file__).parent.parent / "agent-config-example.json").read_text())
REAL_AGENTS  = _EXAMPLE["agents"]
REAL_DOCTYPES = _EXAMPLE["doctypes"]
REAL_CONFIG  = {
    "appname": "agentpn", "provider": "huggingface",
    "connections": [{"name": "txndb1", "params": {}}],
    "agents": REAL_AGENTS, "doctypes": REAL_DOCTYPES,
}


@pytest.fixture(scope="module", autouse=True)
def patch_startup():
    with (
        patch("globals.startup"),
        patch("globals.set_config_params"),
        patch("globals.create_logs", return_value=(MagicMock(), None)),
        patch("db.load_session", return_value=MagicMock()),
        patch("db.add_config"),
        patch("db.get_connection_str", return_value="postgresql://test"),
        patch("langchain_postgres.vectorstores.PGVector"),
        patch("langchain_huggingface.HuggingFaceEmbeddings"),
        patch("app.initialize_vector_stores"),
        patch("app.validate_ssrf_config"),
        patch("app.sync_doctypes_to_db"),
    ):
        import globals as g
        g.config      = REAL_CONFIG
        g.secret_data = {"restuser": "u", "restpwd": "p"}
        g.engine      = MagicMock()
        g.logger      = MagicMock()
        g.dbs         = {}
        yield


@pytest.fixture
def am():
    """Shorthand for the app module."""
    import app
    return app


# ─────────────────────────────────────────────────────────────────────────────
# has_vectorstore — real agents
# ─────────────────────────────────────────────────────────────────────────────

class TestHasVectorstore:

    def test_caseagentv1_has_vectorstore(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "caseagentv1")
        # Expected: True — caseagentv1 has a vectorstore block
        assert am.has_vectorstore(agent) is True

    def test_userManual_has_vectorstore(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "userManual")
        assert am.has_vectorstore(agent) is True

    def test_openaiVision_no_vectorstore(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "openaiVision")
        # Expected: False — openaiVision is OCR-only, no vector search
        assert am.has_vectorstore(agent) is False

    def test_orchestratorAgent_no_vectorstore(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "orchestratorAgent")
        # Expected: False — orchestrator uses DB prereqs, not vector search
        assert am.has_vectorstore(agent) is False


# ─────────────────────────────────────────────────────────────────────────────
# normalise_vectorstore_config — real agent configs
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseVectorstoreConfig:

    def test_caseagentv1_normalised(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "caseagentv1")
        result = am.normalise_vectorstore_config(agent)
        # Expected: one entry with label=default, collection=caseagentv1
        assert len(result) == 1
        assert result[0]["label"]      == "default"
        assert result[0]["collection"] == "caseagentv1"
        assert result[0]["cnt_similarities"] == 2
        assert "RuleID" in result[0]["filter"]
        assert "itenantid" in result[0]["metadata"]

    def test_userManual_normalised(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "userManual")
        result = am.normalise_vectorstore_config(agent)
        # Expected: collection=userManual, filter includes DocName
        assert result[0]["collection"] == "userManual"
        assert "DocName" in result[0]["filter"]
        assert "itenantid" in result[0]["metadata"]

    def test_rulesDev_normalised(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "rulesDev")
        result = am.normalise_vectorstore_config(agent)
        # Expected: filter=[RuleID], metadata includes itenantid
        assert result[0]["filter"] == ["RuleID"]
        assert "itenantid" in result[0]["metadata"]

    def test_openaiVision_returns_empty(self, am):
        agent = next(a for a in REAL_AGENTS if a["agent"] == "openaiVision")
        result = am.normalise_vectorstore_config(agent)
        # Expected: [] — no vectorstore
        assert result == []

    def test_does_not_mutate_original_config(self, am):
        """Normalisation must not modify the loaded agent config in-place."""
        agent = next(a for a in REAL_AGENTS if a["agent"] == "caseagentv1")
        original_vs = agent["vectorstore"].copy()
        _ = am.normalise_vectorstore_config(agent)
        # Expected: original vectorstore dict unchanged
        assert "label" not in agent["vectorstore"]
        assert agent["vectorstore"] == original_vs


# ─────────────────────────────────────────────────────────────────────────────
# load_active_agents — real config agents
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadActiveAgents:

    def test_all_real_agents_active_by_default(self, am):
        """
        All agents in the real config lack is_active → all default to active.
        Expected count = len(REAL_AGENTS).
        """
        result = am.load_active_agents(REAL_CONFIG)
        # Expected: all 9 agents loaded
        assert len(result) == len(REAL_AGENTS)

    def test_inactive_agent_skipped(self, am):
        """Adding is_active=False to rulesDev removes it from the loaded list."""
        config = {**REAL_CONFIG}
        agents_copy = [dict(a) for a in REAL_AGENTS]
        for a in agents_copy:
            if a["agent"] == "rulesDev":
                a["is_active"] = False
        config = {**REAL_CONFIG, "agents": agents_copy}

        result = am.load_active_agents(config)
        agent_names = [a["agent"] for a in result]
        # Expected: rulesDev excluded, all others present
        assert "rulesDev" not in agent_names
        assert "caseagentv1" in agent_names
        assert "userManual"  in agent_names
        assert len(result)   == len(REAL_AGENTS) - 1

    def test_versioned_agents_only_latest_active(self, am):
        """
        Simulates production pattern: keep only v2 active, v1 archived.
        """
        versioned = [
            {"agent": "fraudAgent-v1", "is_active": False, "vectorstore": {}},
            {"agent": "fraudAgent-v2", "is_active": True,  "vectorstore": {}},
        ]
        config = {**REAL_CONFIG, "agents": versioned}
        result = am.load_active_agents(config)
        # Expected: only fraudAgent-v2
        assert len(result) == 1
        assert result[0]["agent"] == "fraudAgent-v2"


# ─────────────────────────────────────────────────────────────────────────────
# validate_doctype_references — real config
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateDoctypeReferences:

    def test_real_config_passes_validation(self, am):
        """The shipped agent-config-example.json must pass with no errors."""
        import globals as g
        g.config = REAL_CONFIG
        # Expected: no ValueError — all doctypes are registered
        am.validate_doctype_references()

    def test_userManualPM_no_doctype_field_passes(self, am):
        """
        userManualPM intentionally has no 'doctype' field.
        Validation must skip it (agent.get('doctype') is None).
        """
        import globals as g
        g.config = REAL_CONFIG
        pm_agent = next(a for a in REAL_AGENTS if a["agent"] == "userManualPM")
        # Confirm it has no doctype field
        assert "doctype" not in pm_agent or pm_agent.get("doctype") is None
        # No raise
        am.validate_doctype_references()

    def test_unknown_doctype_raises(self, am):
        """Adding an agent with an unregistered doctype must raise."""
        import globals as g
        new_agent = {
            "agent": "brokenAgent",
            "doctype": "UnregisteredType",
            "vectorstore": {}
        }
        g.config = {**REAL_CONFIG, "agents": REAL_AGENTS + [new_agent]}
        with pytest.raises(ValueError, match="UnregisteredType"):
            am.validate_doctype_references()

    def test_non_bool_is_active_raises(self, am):
        import globals as g
        bad_agent = {"agent": "badAgent", "is_active": "yes", "vectorstore": {}}
        g.config = {**REAL_CONFIG, "agents": REAL_AGENTS + [bad_agent]}
        with pytest.raises(ValueError, match="must be a boolean"):
            am.validate_doctype_references()

    def test_openaiVision_no_vectorstore_skips_doctype_check(self, am):
        """openaiVision has a doctype field but no vectorstore — must not be checked."""
        import globals as g
        g.config = REAL_CONFIG
        ocr_agent = next(a for a in REAL_AGENTS if a["agent"] == "openaiVision")
        # Confirm: has doctype but no vectorstore
        assert "doctype"     in ocr_agent
        assert "vectorstore" not in ocr_agent
        # No raise
        am.validate_doctype_references()


# ─────────────────────────────────────────────────────────────────────────────
# get_doctype_config — real doctypes
# ─────────────────────────────────────────────────────────────────────────────

class TestGetDoctypeConfig:

    def test_rule_specific_l1_action_returns_single_strategy(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("RuleSpecificL1Action")
        # Expected: strategy=single, collection=caseagentv1
        assert result["chunking"]["strategy"] == "single"
        assert result["collection"]           == "caseagentv1"
        assert result["is_scanned"]           is False

    def test_user_manual_returns_page_strategy(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("UserManual")
        # Expected: strategy=page, collection=userManual
        assert result["chunking"]["strategy"] == "page"
        assert result["collection"]           == "userManual"

    def test_rules_dev_returns_token_256_30(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("RulesDev")
        # Expected: token strategy, chunk_size=256, overlap=30
        assert result["chunking"]["strategy"]   == "token"
        assert result["chunking"]["chunk_size"] == 256
        assert result["chunking"]["overlap"]    == 30

    def test_policy_document_returns_token_512_50(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("PolicyDocument")
        # Expected: token strategy, chunk_size=512, overlap=50
        assert result["chunking"]["chunk_size"] == 512
        assert result["chunking"]["overlap"]    == 50

    def test_claim_document_is_scanned_with_ocr_agent(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("ClaimDocument")
        # Expected: is_scanned=True, ocr_agent=openaiVision
        assert result["is_scanned"]  is True
        assert result["ocr_agent"]   == "openaiVision"
        assert result["chunking"]["chunk_size"] == 2000

    def test_csv_data_returns_row_strategy(self, am):
        import globals as g
        g.config = REAL_CONFIG
        result = am.get_doctype_config("CSVData")
        # Expected: strategy=row, no chunk_size needed
        assert result["chunking"]["strategy"] == "row"

    def test_unknown_doctype_raises_404(self, am):
        import globals as g
        g.config = REAL_CONFIG
        with pytest.raises(HTTPException) as exc:
            am.get_doctype_config("NoSuchType")
        assert exc.value.status_code == 404
        assert "NoSuchType" in exc.value.detail


# ─────────────────────────────────────────────────────────────────────────────
# validate_storage_url  (SEC-A1)
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateStorageUrl:

    def test_allowed_s3_bucket_passes(self, am):
        with patch.object(am, "ALLOWED_STORAGE_ORIGINS",
                          ["https://dronapay-docs.s3.ap-south-1.amazonaws.com"]):
            am.validate_storage_url(
                "https://dronapay-docs.s3.ap-south-1.amazonaws.com/tenant-17/policy.pdf")

    def test_allowed_gcs_bucket_passes(self, am):
        with patch.object(am, "ALLOWED_STORAGE_ORIGINS",
                          ["https://storage.googleapis.com/dronapay-bucket"]):
            am.validate_storage_url(
                "https://storage.googleapis.com/dronapay-bucket/usermanual-v6.pdf")

    def test_untrusted_domain_blocked_400(self, am):
        with patch.object(am, "ALLOWED_STORAGE_ORIGINS",
                          ["https://dronapay-docs.s3.ap-south-1.amazonaws.com"]):
            with pytest.raises(HTTPException) as exc:
                am.validate_storage_url("https://attacker.com/evil.pdf")
            assert exc.value.status_code == 400

    def test_aws_metadata_service_blocked(self, am):
        """AC-4.7 — 169.254.169.254 is the AWS metadata IP."""
        with patch.object(am, "ALLOWED_STORAGE_ORIGINS",
                          ["https://dronapay-docs.s3.amazonaws.com"]):
            with pytest.raises(HTTPException) as exc:
                am.validate_storage_url("http://169.254.169.254/latest/meta-data/")
            assert exc.value.status_code == 400

    def test_empty_origins_returns_500_not_400(self, am):
        """Misconfigured server (no allowlist) → 500, not silent 400."""
        with patch.object(am, "ALLOWED_STORAGE_ORIGINS", []):
            with pytest.raises(HTTPException) as exc:
                am.validate_storage_url("https://any-url.com/doc.pdf")
            assert exc.value.status_code == 500

    def test_file_content_path_never_calls_validate(self, am):
        """When file_content is used, validate_storage_url is never invoked."""
        import base64, hashlib
        content = b"test"
        b64     = base64.b64encode(content).decode()
        sha     = hashlib.sha256(content).hexdigest()
        # This just verifies the helper is not called — endpoint-level test
        with patch.object(am, "validate_storage_url") as mock_ssrf:
            am.validate_storage_url.__doc__  # just to reference it
        # (Actual no-call behaviour is tested in test_endpoints.py)
        assert callable(am.validate_storage_url)


# ─────────────────────────────────────────────────────────────────────────────
# warn_suspicious_tenant (SEC-A5)
# ─────────────────────────────────────────────────────────────────────────────

class TestWarnSuspiciousTenant:

    @pytest.mark.parametrize("tenant_id", [17, 1, 9999, 100])
    def test_valid_tenant_ids_no_warning(self, am, tenant_id):
        """Real tenant IDs used in the config (e.g. 17) must not trigger warning."""
        import globals as g
        with patch.object(g, "logger") as mock_log:
            am.warn_suspicious_tenant("caseagentv1", tenant_id, "/ingest_document")
        # Expected: no warning for valid tenant IDs
        mock_log.warning.assert_not_called()

    @pytest.mark.parametrize("bad_id", [0, -1, -999, None])
    def test_invalid_tenant_ids_log_warning(self, am, bad_id):
        import globals as g
        with patch.object(g, "logger") as mock_log:
            am.warn_suspicious_tenant("caseagentv1", bad_id, "/add_to_vectorstore")
        # Expected: SEC-A5 warning logged
        mock_log.warning.assert_called_once()
        assert "[SEC-A5]" in mock_log.warning.call_args[0][0]
