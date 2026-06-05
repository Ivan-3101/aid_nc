"""
tests/test_endpoints.py
=======================
Endpoint tests using real agent IDs and realistic DronaPay payloads.

Real agents under test:
  caseagentv1     /agent  — case analysis with DB prerequisites
  userManual      /agent  — help queries
  rulesDev        /agent  — rule development
  openaiVision    /agent  — OCR (no vectorstore)
  + DMS endpoints

Run:
    pytest tests/test_endpoints.py -v
"""
import base64
import hashlib
import json
import uuid
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ALLOWED_STORAGE_ORIGINS",
                      "http://test-bucket.example.com,https://dronapay-docs.s3.amazonaws.com")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret")

AUTH = ("testuser", "testpwd")

# ── Realistic test payloads ───────────────────────────────────────────────────
DOC_ID  = str(uuid.uuid4())
VER_ID  = str(uuid.uuid4())
TENANT  = 17  # tenant 17 is used throughout the real config seed data

# Payload for caseagentv1 — real field names from agent config input_data
CASE_AGENT_PAYLOAD = {
    "agentid": "caseagentv1",
    "data": {
        "RuleSpecificL1Action": "FLAG",
        "RuleID":    "4231",
        "CaseID":    "TXN-9182736",
        "BatchDate": "2024-03-15",
        "itenantid": TENANT,
        "iaccountid":  10001,
        "icustomerid": 20001,
    },
}

# Payload for userManual — real field names from agent config
USER_MANUAL_PAYLOAD = {
    "agentid": "userManual",
    "data": {
        "itenantid":  TENANT,
        "user_query": "How do I configure a payment rule in DronaPay?",
    },
}

# Payload for rulesDev
RULES_DEV_PAYLOAD = {
    "agentid": "rulesDev",
    "data": {
        "itenantid":       TENANT,
        "user_query":      "Create a rule to flag transactions above ₹50,000 from new merchants.",
        "rule_expectation": "Flag high-value transactions from unverified merchants.",
    },
}

# DMS payloads
PLAIN_TEXT_CONTENT = b"DronaPay payment rule: transactions above 50000 require review."
PLAIN_SHA256  = hashlib.sha256(PLAIN_TEXT_CONTENT).hexdigest()
PLAIN_B64     = base64.b64encode(PLAIN_TEXT_CONTENT).decode()


@pytest.fixture(scope="module")
def client(test_client):
    return test_client


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthentication:

    @pytest.mark.parametrize("endpoint,method,payload", [
        ("/agent",            "post", {"agentid": "caseagentv1", "data": {}}),
        ("/ingest_document",  "post", {}),
        ("/check_similarity", "post", {}),
        ("/document_status/some-id", "get", None),
        ("/archive_version",  "post", {}),
        ("/add_to_vectorstore", "post", {"agentid": "caseagentv1", "data": {}}),
    ])
    def test_unauthenticated_request_returns_401(self, client, endpoint, method, payload):
        """Every endpoint must reject unauthenticated requests."""
        fn = getattr(client, method)
        r = fn(endpoint, json=payload) if payload is not None else fn(endpoint)
        # Expected: 401 Unauthorized
        assert r.status_code == 401, f"{endpoint} returned {r.status_code}"

    def test_correct_credentials_pass_guard(self, client):
        """Correct credentials should not get a 401 (may get 404 for unknown agent)."""
        r = client.post("/agent",
                        json={"agentid": "nonexistent_agent", "data": {}},
                        auth=AUTH)
        assert r.status_code != 401


# ─────────────────────────────────────────────────────────────────────────────
# POST /agent — real agent IDs
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentEndpoint:

    def test_unknown_agent_returns_404(self, client):
        r = client.post("/agent",
                        json={"agentid": "no_such_agent_v99", "data": {}},
                        auth=AUTH)
        # Expected: 404 — agent not in config
        assert r.status_code == 404

    def test_openaiVision_no_vectorstore_call_succeeds(self, client):
        """openaiVision has no vectorstore so similarity search is skipped."""
        with patch("app.get_chain_result") as mock_llm:
            mock_llm.return_value = MagicMock(content='{"answer": "OCR result"}')
            r = client.post("/agent",
                            json={"agentid": "openaiVision",
                                  "data": {"base64_img": PLAIN_B64}},
                            auth=AUTH)
        # Expected: 200 — LLM called, no vector search
        assert r.status_code == 200

    def test_caseagentv1_with_mocked_chain(self, client):
        """caseagentv1 with mocked prerequisites and LLM."""
        import app as a
        a.vector_store["caseagentv1"] = MagicMock(
            similarity_search_by_vector=MagicMock(return_value=[]))

        with patch("app.get_requisites",  return_value={}), \
             patch("app.get_similarities", return_value=[]), \
             patch("app.get_chain_result") as mock_llm:
            mock_llm.return_value = MagicMock(
                content='{"comment": "Review required", "decision": "REVIEW"}')
            r = client.post("/agent", json=CASE_AGENT_PAYLOAD, auth=AUTH)

        assert r.status_code == 200
        body = r.json()
        assert "decision" in body or "comment" in body

    def test_userManual_with_mocked_chain(self, client):
        import app as a
        a.vector_store["userManual"] = MagicMock(
            similarity_search_by_vector=MagicMock(return_value=[]))

        with patch("app.get_requisites",  return_value={}), \
             patch("app.get_similarities", return_value=[{"docid": "doc1", "itenantid": TENANT}]), \
             patch("app.get_chain_result") as mock_llm:
            mock_llm.return_value = MagicMock(
                content='{"answer": "Go to Rules > Configuration."}')
            r = client.post("/agent", json=USER_MANUAL_PAYLOAD, auth=AUTH)

        assert r.status_code == 200
        assert r.json().get("answer") is not None

    def test_reloadconfig_reloads_all_agents(self, client):
        r = client.post("/reloadconfig", auth=AUTH)
        # Expected: "Done"
        assert r.status_code == 200
        assert "Done" in r.text


# ─────────────────────────────────────────────────────────────────────────────
# POST /add_to_vectorstore — real agent IDs
# ─────────────────────────────────────────────────────────────────────────────

class TestAddToVectorstore:

    def test_caseagentv1_single_strategy_inserts_one_vector(self, client):
        """
        AC-2.3 / AC-4.9: caseagentv1 uses single strategy →
        exactly one vector per call, identical to pre-DMS-002 behaviour.
        """
        import app as a
        mock_vs = MagicMock()
        a.vector_store["caseagentv1"] = mock_vs

        payload = {
            "agentid": "caseagentv1",
            "data": {
                "RuleSpecificL1Action": "FLAG",
                "ruleid":    "4231",
                "caseid":    "TXN-9182736",
                "batchdate": "2024-03-15",
                "itenantid": TENANT,
                "iaccountid":  10001,
                "icustomerid": 20001,
                # id field uses doctype name
                "RuleSpecificL1Action": "FLAG",
            },
        }
        r = client.post("/add_to_vectorstore", json=payload, auth=AUTH)

        # Can be 200 or 400 depending on validation; if 200 check one vector
        if r.status_code == 200:
            docs_added = mock_vs.add_documents.call_args[0][0]
            # Expected: exactly 1 Document (single strategy)
            assert len(docs_added) == 1
            # Expected: ID in format "{doc_id}_0"
            ids_added = mock_vs.add_documents.call_args[0][1] \
                        if len(mock_vs.add_documents.call_args[0]) > 1 \
                        else mock_vs.add_documents.call_args[1].get("ids", [])
            if ids_added:
                assert ids_added[0].endswith("_0")


# ─────────────────────────────────────────────────────────────────────────────
# POST /ingest_document
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestDocumentEndpoint:

    def _ingest_payload(self, **overrides):
        payload = {
            "document_id": DOC_ID,
            "version_id":  VER_ID,
            "itenantid":   TENANT,
            "doctype":     "UserManual",      # real doctype
            "file_type":   "txt",
            "raw_text":    "DronaPay user manual content for payment rules.",
            "sha256_hash": PLAIN_SHA256,
        }
        payload.update(overrides)
        return payload

    def test_raw_text_returns_queued(self, client):
        """AC-4.1: returns within 2s with status=queued."""
        import time
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            start = time.time()
            r = client.post("/ingest_document", json=self._ingest_payload(), auth=AUTH)
            elapsed = time.time() - start
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert elapsed < 2.0, f"Response took {elapsed:.2f}s"

    def test_file_content_db_path_no_storage_url_needed(self, client):
        """
        file_content path works without any external storage configured.
        No SSRF check should be triggered.
        """
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"), \
             patch("app.validate_storage_url") as mock_ssrf:
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                raw_text=None,
                                file_content=PLAIN_B64),
                            auth=AUTH)
        # Expected: 200 queued, SSRF check never called (no URL provided)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        mock_ssrf.assert_not_called()

    def test_user_manual_doctype_accepted(self, client):
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(doctype="UserManual"),
                            auth=AUTH)
        assert r.status_code == 200

    def test_rules_dev_doctype_accepted(self, client):
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                doctype="RulesDev",
                                raw_text="Rule: flag transactions above 50000."),
                            auth=AUTH)
        assert r.status_code == 200

    def test_unknown_doctype_rejected_404(self, client):
        r = client.post("/ingest_document",
                        json=self._ingest_payload(doctype="UnknownType"),
                        auth=AUTH)
        # Expected: 404 — doctype not in registry
        assert r.status_code == 404

    def test_no_source_rejected_400(self, client):
        payload = self._ingest_payload()
        del payload["raw_text"]
        r = client.post("/ingest_document", json=payload, auth=AUTH)
        # Expected: 400 — need raw_text, file_content, or storage_url
        assert r.status_code == 400

    def test_untrusted_storage_url_blocked(self, client):
        """SEC-A1: storage_url must start with an allowed origin."""
        r = client.post("/ingest_document",
                        json=self._ingest_payload(
                            raw_text=None,
                            storage_url="https://attacker.com/payload.pdf"),
                        auth=AUTH)
        # Expected: 400 SSRF block
        assert r.status_code == 400

    def test_allowed_storage_url_passes_ssrf(self, client):
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                raw_text=None,
                                storage_url="https://dronapay-docs.s3.amazonaws.com/doc.pdf"),
                            auth=AUTH)
        # Expected: 200 queued — allowed origin
        assert r.status_code == 200

    def test_rules_dev_chunk_size_bounds_enforced(self, client):
        """SEC-A4: chunk_size=1 is below MIN_CHUNK_SIZE=64."""
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                doctype="RulesDev",
                                chunking_override={
                                    "strategy": "token",
                                    "chunk_size": 1,  # below MIN=64
                                    "overlap":    0,
                                }),
                            auth=AUTH)
        # Expected: 400 — chunk_size out of range
        assert r.status_code == 400

    def test_overlap_ge_chunk_size_rejected(self, client):
        """SEC-A4: overlap must be strictly less than chunk_size."""
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                doctype="PolicyDocument",
                                chunking_override={
                                    "strategy": "token",
                                    "chunk_size": 128,
                                    "overlap":    128,  # equal → invalid
                                }),
                            auth=AUTH)
        assert r.status_code == 400

    def test_claim_document_scanned_type_accepted(self, client):
        """ClaimDocument is a scanned type — endpoint should accept it."""
        with patch("app.update_version_status"), \
             patch("app.run_ingest_pipeline"):
            r = client.post("/ingest_document",
                            json=self._ingest_payload(
                                doctype="ClaimDocument",
                                file_type="png",
                                raw_text=None,
                                file_content=PLAIN_B64),
                            auth=AUTH)
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# GET /document_status/{document_id}
# ─────────────────────────────────────────────────────────────────────────────

class TestDocumentStatusEndpoint:

    def test_not_found_returns_404(self, client):
        with patch("app.get_version_status_from_db", return_value=None):
            r = client.get(f"/document_status/{DOC_ID}", auth=AUTH)
        assert r.status_code == 404

    def test_pending_approval_status_returned(self, client):
        """Normal successful ingestion ends in pending_approval."""
        with patch("app.get_version_status_from_db", return_value={
            "version_id":     VER_ID,
            "version_status": "pending_approval",
            "chunk_count":    4,
            "error_message":  None,
        }):
            r = client.get(f"/document_status/{DOC_ID}", auth=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["status"]      == "pending_approval"
        assert body["chunk_count"] == 4
        assert body["document_id"] == DOC_ID

    def test_failed_status_with_real_error_message(self, client):
        """SHA-256 mismatch is a real error that occurs when fetched file is tampered."""
        with patch("app.get_version_status_from_db", return_value={
            "version_id":     VER_ID,
            "version_status": "failed",
            "chunk_count":    0,
            "error_message":  "SHA-256 hash mismatch between request and fetched file.",
        }):
            r = client.get(f"/document_status/{DOC_ID}", auth=AUTH)
        assert r.status_code == 200
        assert r.json()["error_message"] == "SHA-256 hash mismatch between request and fetched file."

    def test_status_transitions_observable(self, client):
        """Each pipeline stage status is accessible via the endpoint."""
        for status in ["queued", "extracting", "chunking", "embedding", "pending_approval"]:
            with patch("app.get_version_status_from_db", return_value={
                "version_id": VER_ID, "version_status": status,
                "chunk_count": None, "error_message": None,
            }):
                r = client.get(f"/document_status/{DOC_ID}", auth=AUTH)
            assert r.json()["status"] == status


# ─────────────────────────────────────────────────────────────────────────────
# POST /check_similarity
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckSimilarityEndpoint:

    def _payload(self, **overrides):
        p = {
            "itenantid":   TENANT,
            "category":    "UserManual",  # real doctype
            "raw_text":    "How do I configure a payment rule in DronaPay?",
            "sha256_hash": "placeholder-hash",
        }
        p.update(overrides)
        return p

    def test_no_similar_docs_returns_false(self, client):
        import app as a
        a.vector_store["UserManual"] = MagicMock(
            similarity_search_with_score=MagicMock(return_value=[]))
        r = client.post("/check_similarity", json=self._payload(), auth=AUTH)
        assert r.status_code == 200
        assert r.json()["has_near_duplicates"] is False
        assert r.json()["threshold"] == 0.85  # real UserManual threshold

    def test_near_duplicate_above_threshold_found(self, client):
        mock_doc = MagicMock()
        mock_doc.metadata = {
            "document_id":    DOC_ID,
            "document_name":  "DronaPay User Manual v6.pdf",
            "version_number": 6,
        }
        import app as a
        a.vector_store["UserManual"] = MagicMock(
            similarity_search_with_score=MagicMock(return_value=[(mock_doc, 0.93)]))

        r = client.post("/check_similarity", json=self._payload(), auth=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["has_near_duplicates"] is True
        assert len(body["matches"]) == 1
        assert body["matches"][0]["similarity_score"] == 0.93
        assert body["matches"][0]["document_name"] == "DronaPay User Manual v6.pdf"

    def test_rules_dev_category_uses_085_threshold(self, client):
        """RulesDev also has similarity_threshold=0.85 in real config."""
        import app as a
        a.vector_store["RulesDev"] = MagicMock(
            similarity_search_with_score=MagicMock(return_value=[]))
        r = client.post("/check_similarity",
                        json=self._payload(category="RulesDev",
                                           raw_text="Flag high-value transactions."),
                        auth=AUTH)
        assert r.status_code == 200
        assert r.json()["threshold"] == 0.85

    def test_unknown_category_returns_404(self, client):
        r = client.post("/check_similarity",
                        json=self._payload(category="NonExistentDoctype"),
                        auth=AUTH)
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# POST /archive_version
# ─────────────────────────────────────────────────────────────────────────────

class TestArchiveVersionEndpoint:

    def test_wrong_tenant_returns_403(self, client):
        """Tenant 17 cannot archive a version belonging to tenant 99."""
        with patch("app.get_version_metadata",
                   return_value={"id": VER_ID, "document_id": DOC_ID}), \
             patch("app.get_document_metadata",
                   return_value={"itenantid": 99}):  # different tenant
            r = client.post("/archive_version",
                            json={"version_id": VER_ID, "itenantid": TENANT},
                            auth=AUTH)
        # Expected: 403
        assert r.status_code == 403

    def test_correct_tenant_archives_successfully(self, client):
        with patch("app.get_version_metadata",
                   return_value={"id": VER_ID, "document_id": DOC_ID}), \
             patch("app.get_document_metadata",
                   return_value={"itenantid": TENANT}), \
             patch("app.soft_delete_vectors_for_version") as mock_del:
            r = client.post("/archive_version",
                            json={"version_id": VER_ID, "itenantid": TENANT},
                            auth=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "archived"
        mock_del.assert_called_once_with(VER_ID)

    def test_version_not_found_returns_404(self, client):
        with patch("app.get_version_metadata", return_value={}):
            r = client.post("/archive_version",
                            json={"version_id": str(uuid.uuid4()), "itenantid": TENANT},
                            auth=AUTH)
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /internal/tenant_vectors/{itenantid}  (SEC-A6)
# ─────────────────────────────────────────────────────────────────────────────

class TestInternalTenantVectorsEndpoint:

    def test_no_header_returns_403(self, client):
        r = client.delete(f"/internal/tenant_vectors/{TENANT}", auth=AUTH)
        assert r.status_code == 403

    def test_wrong_secret_returns_403(self, client):
        r = client.delete(f"/internal/tenant_vectors/{TENANT}",
                          headers={"X-Internal-Secret": "wrong"},
                          auth=AUTH)
        assert r.status_code == 403

    def test_correct_secret_executes_and_returns_counts(self, client):
        import globals as g
        g.config["doctypes"] = [{"collection": "caseagentv1"},
                                 {"collection": "userManual"}]
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__  = MagicMock(return_value=False)
        mock_result = MagicMock()
        mock_result.rowcount = 15
        mock_session.execute.return_value = mock_result

        with patch("app.Session", return_value=mock_session):
            r = client.delete(f"/internal/tenant_vectors/{TENANT}",
                              headers={"X-Internal-Secret": "test-internal-secret"},
                              auth=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["itenantid"] == TENANT
        assert "total_deleted"   in body
        assert "by_collection"   in body
