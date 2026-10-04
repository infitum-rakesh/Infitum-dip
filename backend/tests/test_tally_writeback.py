"""
TALLY-I — automated tests for the actual write-back: idempotent posting,
retry/circuit-breaker classification, and post-write read-back
verification. Same "real service logic, only the network boundary to
Tally itself is mocked" convention as test_invoice_intelligence.py's own
ERP posting tests (MockErpAdapter) — MockTallyConnector implements the
exact public methods tally_writeback_service.py actually calls
(post_voucher/get_vouchers) so the real idempotency/retry/circuit-breaker/
verification logic in that module runs completely unmodified.

Behavior is selected via connection.config["behavior"]:
  happy      -> succeeds on the first attempt; read-back matches exactly.
  retry      -> fails transiently twice, succeeds on the 3rd (within
                MAX_ATTEMPTS=3).
  deadletter -> always fails transiently (exhausts MAX_ATTEMPTS).
  reject     -> always fails with a permanent, non-retryable rejection.
  mismatch   -> post succeeds; read-back returns a voucher with a
                different total (MISMATCH verdict).
  notfound   -> post succeeds; read-back finds no matching voucher.
"""
from __future__ import annotations

import asyncio

import pytest

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError
from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceSource, ApInvoiceStatus
from app.models.tally_mapping import TallyVoucherApprovalStatus, TallyVoucherPreview, TallyVoucherPreviewStatus
from app.models.tally_writeback import TallyWriteBackStatus, TallyWriteBackVerificationStatus
from app.services import connection_service
from app.services import tally_writeback_service as wb_svc


class MockTallyConnector(BaseConnector):
    connection_type = "TALLY_PRIME"

    def __init__(self, config=None, secrets=None, cache_key=None):
        super().__init__(config or {}, secrets or {}, cache_key)
        self.calls: list[str] = []
        self._attempts = 0

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(True, "Mock TallyPrime reachable.", 3.1, server_version=None)

    def post_voucher(self, voucher_xml: str) -> dict:
        self.calls.append(voucher_xml)
        behavior = self.config.get("behavior")
        if behavior == "reject":
            raise ConnectorError("TallyPrime rejected the request: Unknown Ledger 'Bogus Ledger'.")
        if behavior == "deadletter":
            raise ConnectorError("Could not reach TallyPrime at http://mock: Connection refused")
        if behavior == "retry":
            self._attempts += 1
            if self._attempts < 3:
                raise ConnectorError("Could not reach TallyPrime at http://mock: timed out")
        return {"created": 1, "altered": 0, "errors": 0, "last_voucher_id": "171", "last_master_id": "172"}

    def get_vouchers(self, *, company, voucher_type=None, party=None, from_date=None, to_date=None) -> dict:
        behavior = self.config.get("behavior")
        if behavior == "notfound":
            return {"vouchers": []}
        total = 999.0 if behavior == "mismatch" else 1180.0
        return {"vouchers": [{
            "party_ledger_name": party, "voucher_type": voucher_type, "reference": "INV-TALLY-I-1",
            "ledger_entries": [{"ledger_name": "Direct Expenses", "amount": total, "is_deemed_positive": "Yes"}],
            "guid": "mock-guid-1", "voucher_number": "V-12",
        }]}


async def _make_connection(db, org, *, behavior="happy"):
    conn = Connection(
        organization_id=org.id, name="Verify Tally I", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": 9999, "behavior": behavior},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def _make_invoice(db, org) -> ApInvoice:
    doc = Document(
        organization_id=org.id, filename="tally-i.pdf", storage_path="/tmp/tally-i.pdf",
        doc_type=DocumentType.PDF, status=DocumentStatus.COMPLETED,
    )
    db.add(doc)
    await db.flush()
    invoice = ApInvoice(
        organization_id=org.id, document_id=doc.id, source=ApInvoiceSource.MANUAL_UPLOAD,
        invoice_number="INV-TALLY-I-1", vendor_name="Beta Supplies", currency="INR", total=1180.0,
        status=ApInvoiceStatus.APPROVING,
    )
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def _make_preview(db, org, conn, invoice, *, approval_status=TallyVoucherApprovalStatus.APPROVED) -> TallyVoucherPreview:
    preview = TallyVoucherPreview(
        organization_id=org.id, connection_id=conn.id, invoice_id=invoice.id, company_name="Verify Tally B",
        voucher_type="Purchase", party_ledger_name="Beta Supplies",
        ledger_entries=[{"ledger_name": "Direct Expenses", "amount": 1180.0, "is_deemed_positive": "Yes"}],
        voucher_xml="<ENVELOPE>mock</ENVELOPE>",
        idempotency_key=f"ap-invoice-{invoice.id}-tally-{conn.id}",
        status=TallyVoucherPreviewStatus.READY, approval_status=approval_status,
    )
    db.add(preview)
    await db.commit()
    await db.refresh(preview)
    return preview


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(wb_svc, "BASE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(wb_svc, "MAX_DELAY_SECONDS", 0.05)


async def _settle(db, preview_id: int, timeout: float = 3.0) -> TallyVoucherPreview:
    elapsed, interval = 0.0, 0.02
    while elapsed < timeout:
        preview = await db.get(TallyVoucherPreview, preview_id, populate_existing=True)
        if preview.write_status not in (TallyWriteBackStatus.PENDING, TallyWriteBackStatus.RETRYING):
            return preview
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(f"write-back did not settle within {timeout}s — still {preview.write_status}")


def _patch_connector(monkeypatch, connector: MockTallyConnector):
    monkeypatch.setattr(connection_service, "build_connector", lambda conn: connector)


# ── 1-3. Preconditions ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_voucher_requires_ready_status(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    preview.status = TallyVoucherPreviewStatus.BLOCKED
    await db.commit()

    with pytest.raises(wb_svc.TallyWriteBackError, match="READY"):
        await wb_svc.post_voucher(db, preview, actor_user_id=None)


@pytest.mark.asyncio
async def test_post_voucher_requires_approved(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice, approval_status=TallyVoucherApprovalStatus.PENDING)

    with pytest.raises(wb_svc.TallyWriteBackError, match="approved"):
        await wb_svc.post_voucher(db, preview, actor_user_id=None)


@pytest.mark.asyncio
async def test_post_voucher_already_succeeded_raises(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    preview.write_status = TallyWriteBackStatus.SUCCEEDED
    await db.commit()

    with pytest.raises(wb_svc.TallyWriteBackError, match="already"):
        await wb_svc.post_voucher(db, preview, actor_user_id=None)


# ── 4. Happy path — success + auto-verification ─────────────────────

@pytest.mark.asyncio
async def test_post_voucher_success_flow_updates_preview_and_verifies(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="happy")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    txn = await wb_svc.post_voucher(db, preview, actor_user_id=None)
    assert txn.status == TallyWriteBackStatus.PENDING

    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.SUCCEEDED
    assert settled.tally_master_id == "172"
    assert settled.written_at is not None

    # Auto-verification runs right after success — real GET-and-compare.
    await asyncio.sleep(0.1)
    verifications = await wb_svc.list_verifications(db, preview.id)
    assert len(verifications) == 1
    assert verifications[0].status == TallyWriteBackVerificationStatus.VERIFIED

    await db.refresh(settled)
    assert settled.tally_voucher_guid == "mock-guid-1"
    assert settled.tally_voucher_number == "V-12"

    transactions = await wb_svc.list_transactions(db, preview.id)
    assert len(transactions) == 1
    assert transactions[0].status == TallyWriteBackStatus.SUCCEEDED


# ── 5-7. Retry / circuit classification ──────────────────────────────

@pytest.mark.asyncio
async def test_post_voucher_retries_then_succeeds(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="retry")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.SUCCEEDED

    transactions = await wb_svc.list_transactions(db, preview.id)
    assert len(transactions) == 3  # 2 retryable failures + 1 success, all within MAX_ATTEMPTS=3


@pytest.mark.asyncio
async def test_post_voucher_exhausts_retries_dead_letters(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="deadletter")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.DEAD_LETTER

    transactions = await wb_svc.list_transactions(db, preview.id)
    assert len(transactions) == wb_svc.MAX_ATTEMPTS
    assert all(t.is_retryable for t in transactions)


@pytest.mark.asyncio
async def test_post_voucher_non_retryable_rejection_fails_immediately(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="reject")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.FAILED

    transactions = await wb_svc.list_transactions(db, preview.id)
    assert len(transactions) == 1  # no retry for a permanent rejection
    assert transactions[0].is_retryable is False


# ── 8-9. Post-write verification mismatch / not-found ────────────────

@pytest.mark.asyncio
async def test_verify_write_detects_mismatch(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="mismatch")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.SUCCEEDED

    await asyncio.sleep(0.1)
    verifications = await wb_svc.list_verifications(db, preview.id)
    assert verifications[0].status == TallyWriteBackVerificationStatus.MISMATCH
    total_check = next(c for c in verifications[0].field_comparisons if c["field"] == "total_amount")
    assert total_check["result"] == "MISMATCH"

    # A MISMATCH must never populate the preview's own guid/number fields
    # as if it were a clean verification.
    await db.refresh(settled)
    assert settled.tally_voucher_guid is None


@pytest.mark.asyncio
async def test_verify_write_not_found(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="notfound")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    _patch_connector(monkeypatch, MockTallyConnector(config=conn.config))

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.SUCCEEDED

    await asyncio.sleep(0.1)
    verifications = await wb_svc.list_verifications(db, preview.id)
    assert verifications[0].status == TallyWriteBackVerificationStatus.NOT_FOUND


# ── 10. Resubmission after a terminal failure is allowed ─────────────

@pytest.mark.asyncio
async def test_resubmitting_after_dead_letter_is_allowed(db, make_org, monkeypatch, fast_retries):
    org = await make_org()
    conn = await _make_connection(db, org, behavior="deadletter")
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    connector = MockTallyConnector(config=conn.config)
    _patch_connector(monkeypatch, connector)

    await wb_svc.post_voucher(db, preview, actor_user_id=None)
    settled = await _settle(db, preview.id)
    assert settled.write_status == TallyWriteBackStatus.DEAD_LETTER

    # A DEAD_LETTER preview must be resubmittable (unlike PENDING/RETRYING
    # or SUCCEEDED) — flip the connection to "happy" and retry.
    connector.config = {**conn.config, "behavior": "happy"}
    await wb_svc.post_voucher(db, settled, actor_user_id=None)
    final = await _settle(db, preview.id)
    assert final.write_status == TallyWriteBackStatus.SUCCEEDED
