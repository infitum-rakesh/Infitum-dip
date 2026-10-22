"""
Automated tests for the India Verification module (Compliance &
Verification) — Phase Group 5, IV28. Covers the plan's acceptance
checklist: format validation, provider-confirmed outcomes (VERIFIED/
MISMATCH/UNAVAILABLE), MCA ambiguous match (never auto-picked), Udyam
unavailable, MOA incompleteness, freshness-cache retry-without-
duplication, rule-aware case status, tenant isolation, identifier
masking, partial batch-failure isolation, and scheduled-job crash
recovery.

A small in-process HTTP server stands in for the verification provider
— same self-contained convention as test_rest_connector.py/
test_tallyprime_connector.py, never a dependency on the dev-only
scripts/mock_verification_gateway.py process actually being up.
"""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.india_verification import (
    OverallVerificationStatus,
    VerificationCase,
    VerificationCheckType,
    VerificationRequest,
    VerificationResult,
)
from app.models.india_verification_jobs import VerificationJobItem, VerificationJobItemStatus, VerificationJobRun, VerificationJobRunStatus
from app.services.compliance import (
    company_verification_service,
    identity_verification_service,
    moa_verification_service,
    tax_verification_service,
    udyam_verification_service,
    verification_job_service,
    verification_orchestrator as orch,
    verification_rules_service,
)
from app.services.compliance.verification_status import derive_case_overall_status, derive_result_overall_status


# ── In-process mock provider (self-contained, no external process) ──

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"status": "ok"})
        return self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        responses = self.server.responses
        resp = responses.get(self.path)
        if resp is None:
            return self._send_json(404, {"detail": f"no canned response for {self.path}"})
        if callable(resp):
            resp = resp(body)
        return self._send_json(200, resp)


@pytest.fixture
def gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.responses = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


async def _make_connection(db, org, gateway) -> Connection:
    conn = Connection(
        organization_id=org.id, name="Test Verification Gateway", category=ConnectionCategory.VERIFICATION_PROVIDER,
        connection_type=ConnectionType.MOCK_VERIFICATION_PROVIDER,
        config={"base_url": f"http://127.0.0.1:{gateway.server_address[1]}"},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


NOW_ISO = datetime.now(timezone.utc).isoformat()


# ── Format validation ─────────────────────────────────────────────

def test_pan_format_rejects_malformed_value():
    info = identity_verification_service.validate_pan_format("NOTAPAN")
    assert info["format_status"] == "FORMAT_INVALID"


def test_pan_format_accepts_well_formed_value():
    info = identity_verification_service.validate_pan_format("AAACI1681G")
    assert info["format_status"] == "FORMAT_VALID"


def test_gstin_format_rejects_bad_checksum():
    # Same shape as a real GSTIN but with the check digit flipped.
    info = tax_verification_service.validate_gstin_format("27AAACI1681G1ZQ")
    assert info["format_status"] == "FORMAT_INVALID"


def test_gstin_format_accepts_real_verified_checksum():
    # 27AAACR5055K1Z7 — a real, independently-sourced GSTIN (see
    # tax_verification_service.py's own history: verified against a real
    # example rather than a self-fabricated one).
    info = tax_verification_service.validate_gstin_format("27AAACR5055K1Z7")
    assert info["format_status"] == "FORMAT_VALID"


# ── Masking never leaks the raw identifier ──────────────────────────

def test_mask_pan_never_returns_the_raw_value():
    masked = identity_verification_service.mask_pan("AAACI1681G")
    assert masked != "AAACI1681G"
    assert masked.endswith("681G") or masked[-4:] == "681G"


def test_mask_aadhaar_shows_only_last_four():
    masked = identity_verification_service.mask_aadhaar("234567890123")
    assert masked != "234567890123"
    assert "234567" not in masked  # none of the first 8 digits survive
    assert masked.endswith("0123")


# ── Provider-confirmed outcomes (real HTTP call, real connector code) ──

async def test_pan_verified_outcome_persists_as_verified(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/pan"] = {"status": "VERIFIED", "holder_name": "Infitum Technologies Pvt Ltd", "entity_type": "COMPANY", "reference_id": "ref-1", "verified_at": NOW_ISO}

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", requested_by=None)
    result = await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id)
    assert result.overall_status == OverallVerificationStatus.VERIFIED
    assert result.provider_status_code == "VERIFIED"


async def test_gstin_name_mismatch_outcome_persists_as_mismatch(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/gstin"] = {"status": "NAME_MISMATCH", "legal_name": "Someone Else Pvt Ltd", "registration_status": "Active", "reference_id": "ref-2", "verified_at": NOW_ISO}

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", requested_by=None)
    result = await orch.run_gstin_check(db, case, gstin="27AAACR5055K1Z7", submitted_name="Totally Different Name", connection_id=conn.id)
    assert result.overall_status == OverallVerificationStatus.MISMATCH


async def test_udyam_unavailable_outcome_is_never_silently_pass_or_fail(db, make_org, gateway):
    """Spec requirement: a provider outage must never be equated with
    either a successful or failed verification."""
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/udyam"] = {"status": "UNAVAILABLE", "enterprise_name": None, "classification": None, "reference_id": None, "verified_at": None}

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", requested_by=None)
    result = await orch.run_udyam_check(db, case, udyam_number="UDYAM-MH-01-0001234", connection_id=conn.id)
    assert result.overall_status == OverallVerificationStatus.UNAVAILABLE
    assert result.overall_status not in (OverallVerificationStatus.VERIFIED, OverallVerificationStatus.FAILED)


async def test_mca_ambiguous_match_is_never_auto_picked(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/search/mca"] = {
        "status": "FOUND",
        "matches": [
            {"cin": "U72200MH2015PTC268234", "legal_name": "Infitum Technologies Pvt Ltd", "entity_type": "Private Limited", "status": "Active", "incorporation_date": "2015-01-01", "registered_office": "Mumbai", "verified_at": NOW_ISO},
            {"cin": "U72200MH2016PTC268235", "legal_name": "Infitum Technologies India Pvt Ltd", "entity_type": "Private Limited", "status": "Active", "incorporation_date": "2016-01-01", "registered_office": "Pune", "verified_at": NOW_ISO},
        ],
    }
    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", requested_by=None)
    result = await orch.run_mca_check(db, case, legal_name="Infitum", connection_id=conn.id)
    assert result.provider_status_code == "AMBIGUOUS_MATCH"
    assert result.overall_status == OverallVerificationStatus.REQUIRES_REVIEW  # never MISMATCH, never VERIFIED
    assert len(result.normalized_result["matches"]) == 2  # both candidates preserved for a human to pick


# ── Freshness cache: retry without duplicating a provider call ──────

async def test_fresh_result_short_circuits_without_a_new_request(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    call_count = {"n": 0}

    def _counting_response(_body):
        call_count["n"] += 1
        return {"status": "VERIFIED", "holder_name": "Infitum Technologies Pvt Ltd", "entity_type": "COMPANY", "reference_id": "ref-1", "verified_at": NOW_ISO}
    gateway.responses["/verify/pan"] = _counting_response

    rule = await verification_rules_service.publish_rule(
        db, org.id, name="Freshness Rule", description=None, business_process="TEST_FRESHNESS", subject_type=None,
        required_check_types=["PAN"], freshness_days=90, priority=100, effective_from=None, effective_to=None, actor_id=None,
    )
    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", business_process="TEST_FRESHNESS", requested_by=None)

    first = await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id)
    assert first.expires_at is not None  # set because the matched rule gave PAN a freshness policy
    assert call_count["n"] == 1

    second = await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id)
    assert call_count["n"] == 1  # no new provider call — served from cache
    assert second.id == first.id
    assert second.verified_at == first.verified_at

    requests = (await db.execute(select(VerificationRequest).where(VerificationRequest.case_id == case.id, VerificationRequest.check_type == VerificationCheckType.PAN))).scalars().all()
    assert len(requests) == 1  # no duplicate VerificationRequest row from the cached call

    third = await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id, force_refresh=True)
    assert call_count["n"] == 2  # force_refresh bypasses the cache
    assert third.id == first.id  # same result row, upserted in place


# ── Rule-aware case status (PARTIALLY_VERIFIED) ──────────────────────

async def test_case_reads_partially_verified_when_a_required_check_was_never_run(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/pan"] = {"status": "VERIFIED", "holder_name": "Infitum Technologies Pvt Ltd", "entity_type": "COMPANY", "reference_id": "ref-1", "verified_at": NOW_ISO}

    await verification_rules_service.publish_rule(
        db, org.id, name="Two-Check Rule", description=None, business_process="TEST_PARTIAL", subject_type=None,
        required_check_types=["PAN", "GSTIN"], freshness_days=None, priority=100, effective_from=None, effective_to=None, actor_id=None,
    )
    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", business_process="TEST_PARTIAL", requested_by=None)
    await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id)
    await db.refresh(case)

    # Only PAN (of the two required checks) has ever been run — the case
    # must not read as fully VERIFIED.
    assert case.overall_status == OverallVerificationStatus.PARTIALLY_VERIFIED

    compliance = await verification_rules_service.evaluate_case_compliance(db, case)
    assert compliance.compliant is False
    assert compliance.missing_check_types == ["GSTIN"]


def test_derive_case_overall_status_rule_free_default_is_unchanged():
    """No required_check_types passed (the Phase 1-3 call shape) must
    behave exactly as before rules existed."""
    status = derive_case_overall_status([OverallVerificationStatus.VERIFIED])
    assert status == OverallVerificationStatus.VERIFIED


def test_derive_result_overall_status_never_equates_not_configured_with_verified():
    assert derive_result_overall_status(format_status="FORMAT_VALID", provider_status_code="NOT_CONFIGURED") == OverallVerificationStatus.NOT_CONFIGURED
    assert derive_result_overall_status(format_status="FORMAT_VALID", provider_status_code="NOT_CONFIGURED") != OverallVerificationStatus.VERIFIED


# ── MOA incompleteness ────────────────────────────────────────────

async def test_moa_missing_required_clauses_requires_manual_review(db, make_org):
    from app.models.document import Document

    org = await make_org()
    doc = Document(
        organization_id=org.id, filename="incomplete-moa.pdf", storage_path="/tmp/incomplete-moa.pdf",
        ocr_result={"full_text": "MEMORANDUM OF ASSOCIATION OF Test Co", "quality_flags": []},
        extraction_result={"fields": [
            {"name": "document_title", "value": "Memorandum of Association", "source_page": 1},
            {"name": "company_name", "value": "Test Co", "source_page": 1},
            # objects_clause_text / liability_clause_text / capital_clause_text / registered_office_address deliberately missing
        ]},
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Test Co", requested_by=None)
    result = await moa_verification_service.verify_moa(db, doc, case)
    assert result.provider_status_code == "REQUIRES_MANUAL_REVIEW"
    assert set(result.normalized_result["missing_fields"]) == {"registered_office_address", "objects_clause_text", "liability_clause_text", "capital_clause_text"}
    assert result.overall_status == OverallVerificationStatus.REQUIRES_REVIEW


async def test_moa_text_not_confirmed_is_authenticity_not_established(db, make_org):
    from app.models.document import Document

    org = await make_org()
    doc = Document(
        organization_id=org.id, filename="not-a-moa.pdf", storage_path="/tmp/not-a-moa.pdf",
        ocr_result={"full_text": "INVOICE #4521 Total: Rs 10,000", "quality_flags": []},
        extraction_result={"fields": [{"name": "company_name", "value": "Test Co", "source_page": 1}]},
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Test Co", requested_by=None)
    result = await moa_verification_service.verify_moa(db, doc, case)
    assert result.provider_status_code == "AUTHENTICITY_NOT_ESTABLISHED"


# ── Tenant isolation ───────────────────────────────────────────────

async def test_verification_rule_from_one_org_does_not_apply_to_another(db, make_org):
    org_a = await make_org()
    org_b = await make_org()
    await verification_rules_service.publish_rule(
        db, org_a.id, name="Org A Only", description=None, business_process="TEST_ISOLATION", subject_type=None,
        required_check_types=["PAN"], freshness_days=None, priority=100, effective_from=None, effective_to=None, actor_id=None,
    )
    rule_for_a = await verification_rules_service.resolve_applicable_rule(db, org_a.id, business_process="TEST_ISOLATION", subject_type=None)
    rule_for_b = await verification_rules_service.resolve_applicable_rule(db, org_b.id, business_process="TEST_ISOLATION", subject_type=None)
    assert rule_for_a is not None
    assert rule_for_b is None  # org B never sees org A's org-scoped rule


async def test_case_lookup_rejects_cross_org_access(db, make_org):
    org_a = await make_org()
    org_b = await make_org()
    case = await orch.get_or_create_case(db, organization_id=org_a.id, subject_type="VENDOR", subject_name="Org A Vendor", requested_by=None)
    found_same_org = await orch.get_case(db, organization_id=org_a.id, case_id=case.id)
    found_other_org = await orch.get_case(db, organization_id=org_b.id, case_id=case.id)
    assert found_same_org is not None
    assert found_other_org is None


# ── Batch partial-failure isolation ──────────────────────────────────

async def test_batch_isolates_one_malformed_subject_from_the_rest(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/gstin"] = {"status": "VERIFIED", "legal_name": "Infitum Technologies Pvt Ltd", "trade_name": None, "registration_status": "Active", "reference_id": "ref-1", "verified_at": NOW_ISO}

    job = await verification_job_service.create_manual_job(
        db, org.id, business_process=None, check_types=["GSTIN"], connection_id=conn.id,
        subjects=[
            {"subject_type": "VENDOR", "subject_name": "Good Subject", "identifiers": {"GSTIN": "27AAACR5055K1Z7"}},
            {"subject_type": "VENDOR", "subject_name": "Bad Subject", "identifiers": {"GSTIN": "NOT-A-GSTIN"}},
        ],
        created_by=None,
    )
    items = (await db.execute(select(VerificationJobItem).where(VerificationJobItem.run_id == job.id))).scalars().all()
    for item in items:
        await verification_job_service._run_one_item(db, job, item)

    items = (await db.execute(select(VerificationJobItem).where(VerificationJobItem.run_id == job.id))).scalars().all()
    good = next(i for i in items if i.subject_name == "Good Subject")
    bad = next(i for i in items if i.subject_name == "Bad Subject")
    assert good.status == VerificationJobItemStatus.SUCCESS
    assert bad.status == VerificationJobItemStatus.FAILED
    assert good.identifiers == {}  # scrubbed immediately on success — see IV24
    assert bad.identifiers != {}  # retained to support "Retry Failed"


# ── Scheduled-job crash recovery ─────────────────────────────────────

async def test_recover_stuck_verification_jobs_requeues_running_items(db, make_org):
    org = await make_org()
    run = VerificationJobRun(organization_id=org.id, trigger_type="MANUAL", status=VerificationJobRunStatus.RUNNING, check_types=["PAN"], total_items=1)
    db.add(run)
    await db.flush()
    item = VerificationJobItem(organization_id=org.id, run_id=run.id, subject_type="VENDOR", subject_name="Crashed Mid-Run", status=VerificationJobItemStatus.RUNNING, worker_id="w0", claimed_at=datetime.now(timezone.utc))
    db.add(item)
    await db.commit()

    stats = await verification_job_service.recover_stuck_verification_jobs()
    assert stats["recovered_runs"] >= 1
    assert stats["requeued_items"] >= 1

    await db.refresh(item)
    assert item.status == VerificationJobItemStatus.PENDING
    assert item.worker_id is None


# ── Stale-result refresh advisory (never a fabricated re-verify) ─────

async def test_expired_result_produces_an_advisory_not_a_fake_reverify(db, make_org, gateway):
    org = await make_org()
    conn = await _make_connection(db, org, gateway)
    gateway.responses["/verify/pan"] = {"status": "VERIFIED", "holder_name": "Infitum Technologies Pvt Ltd", "entity_type": "COMPANY", "reference_id": "ref-1", "verified_at": NOW_ISO}

    case = await orch.get_or_create_case(db, organization_id=org.id, subject_type="VENDOR", subject_name="Infitum", business_process="TEST_STALE", requested_by=None)
    result = await orch.run_pan_check(db, case, pan="AAACI1681G", connection_id=conn.id)
    result.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db.commit()

    advisory = await verification_job_service.create_refresh_job_for_expired(db, org.id, business_process="TEST_STALE", created_by=None)
    assert advisory is not None
    assert advisory.status == VerificationJobRunStatus.COMPLETED
    items = (await db.execute(select(VerificationJobItem).where(VerificationJobItem.run_id == advisory.id))).scalars().all()
    assert len(items) == 1
    assert items[0].status == VerificationJobItemStatus.NEEDS_ATTENTION
    assert items[0].identifiers == {}  # never carries a re-derived/fabricated identifier


async def test_no_expired_results_produces_no_advisory_job(db, make_org):
    org = await make_org()
    result = await verification_job_service.create_refresh_job_for_expired(db, org.id, business_process="TEST_NOTHING_EXPIRED", created_by=None)
    assert result is None
