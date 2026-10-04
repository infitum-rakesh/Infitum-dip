"""
tally_writeback_service.py — TALLY-I: the actual write, gated behind
TALLY-H's approval workflow. Mirrors invoice_erp_service.py's own
idempotent-posting + retry + circuit-breaker + auto-verification shape
almost exactly (see that module's own docstring for the full rationale —
repeated only where Tally-specific behavior differs):

- Idempotency: preview.idempotency_key (`ap-invoice-{id}-tally-{connection}`,
  set once at build_preview() time — TALLY-D) is reused unchanged as the
  UNIQUE(idempotency_key, attempt_number) key on every
  TallyVoucherWriteBackTransaction row, exactly like
  ApInvoiceErpTransaction. The pre-call check below short-circuits on
  preview.write_status == SUCCEEDED before ever building a request.
- Retry/circuit breaker: same shape as invoice_erp_service.py, scoped
  smaller (MAX_ATTEMPTS=3, not 5) — a TallyPrime HTTP gateway is
  configured host/port reachable on the SAME local network as this
  platform (see connectors/application/tallyprime.py's own docstring: no
  TLS, no public internet exposure), so a genuine outage is far more
  likely to be "Tally isn't running right now" than a transient blip
  worth chasing for as long as a cross-internet ERP call.
- Post-write verification: real GET-and-compare via
  TallyPrimeConnector.get_vouchers() (party+voucher_type+date-filtered),
  matched by REFERENCE == invoice_number when available. Tally's own
  Import response only confirms it ACCEPTED the envelope (CREATED=1); it
  does not return the created voucher's GUID/voucher number, so those
  fields on TallyVoucherPreview are only ever populated by a SUCCESSFUL
  verification read-back, never invented from the write response alone.

Disclosed, same as every other module in this integration: real code,
never live-verified against an actual licensed TallyPrime instance — see
TALLY-I7's own live-verification report for what WAS verified (a local
mock Tally XML/HTTP gateway).
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.database import tenant_engine_registry
from app.models.invoice_intelligence.core import ApInvoice
from app.models.tally_mapping import TallyVoucherApprovalStatus, TallyVoucherPreview, TallyVoucherPreviewStatus
from app.models.tally_writeback import (
    TallyVoucherWriteBackTransaction,
    TallyVoucherWriteBackVerification,
    TallyWriteBackStatus,
    TallyWriteBackVerificationStatus,
)
from app.services import connection_service

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 20.0
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 60.0

_COMPARE_TOLERANCE = 0.01


class TallyWriteBackError(Exception):
    """Genuine caller precondition failure — not approved, already
    written, already in progress, etc. Never used for a transient
    Tally-side failure (those are recorded as transaction rows)."""


# ── Circuit breaker — in-process, per connection_id (same shape as
# invoice_erp_service.py's own; see that module's docstring) ───────────

_circuit_state: dict[int, dict] = {}


def _breaker_allows(connection_id: int) -> bool:
    st = _circuit_state.get(connection_id)
    if st is None or st["state"] == "CLOSED":
        return True
    if st["state"] == "OPEN":
        if time.monotonic() - st["opened_at"] >= CIRCUIT_COOLDOWN_SECONDS:
            st["state"] = "HALF_OPEN"
            st["probing"] = True
            return True
        return False
    if st.get("probing"):
        return False
    st["probing"] = True
    return True


def _breaker_record_success(connection_id: int) -> None:
    _circuit_state[connection_id] = {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False}


def _breaker_record_failure(connection_id: int) -> None:
    st = _circuit_state.setdefault(connection_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False})
    st["failures"] += 1
    st["probing"] = False
    if st["state"] == "HALF_OPEN" or st["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        st["state"] = "OPEN"
        st["opened_at"] = time.monotonic()


def circuit_status(connection_id: int) -> dict:
    st = _circuit_state.get(connection_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0})
    return dict(st)


async def _next_attempt_number(db: AsyncSession, idempotency_key: str) -> int:
    result = await db.execute(
        select(func.max(TallyVoucherWriteBackTransaction.attempt_number)).where(TallyVoucherWriteBackTransaction.idempotency_key == idempotency_key)
    )
    return (result.scalar_one() or 0) + 1


# ── Public entry point ──────────────────────────────────────────────

async def post_voucher(db: AsyncSession, preview: TallyVoucherPreview, *, actor_user_id: int | None) -> TallyVoucherWriteBackTransaction:
    """Pre-call validation + idempotent short-circuit, then hands off to a
    background retry loop — same non-blocking convention as
    invoice_erp_service.post_invoice(). Returns the first (PENDING)
    transaction row immediately; poll GET .../write-back/transactions or
    preview.write_status for the outcome."""
    if preview.status != TallyVoucherPreviewStatus.READY:
        raise TallyWriteBackError("Only a READY voucher preview (all deterministic pre-write checks passed) can be written to Tally.")
    if preview.approval_status != TallyVoucherApprovalStatus.APPROVED:
        raise TallyWriteBackError("This voucher has not been approved for write-back yet — submit it for approval first.")
    if preview.write_status == TallyWriteBackStatus.SUCCEEDED:
        raise TallyWriteBackError("This voucher has already been written to Tally successfully — write back again only after a genuine correction and rebuild.")
    if preview.write_status in (TallyWriteBackStatus.PENDING, TallyWriteBackStatus.RETRYING):
        raise TallyWriteBackError("This voucher already has a write-back in progress — wait for it to finish (or fail) before trying again.")
    if not preview.voucher_xml:
        raise TallyWriteBackError("This preview has no voucher XML to send — rebuild the preview first.")

    idempotency_key = preview.idempotency_key or f"ap-invoice-{preview.invoice_id}-tally-{preview.connection_id}"
    correlation_id = uuid.uuid4().hex
    attempt_number = await _next_attempt_number(db, idempotency_key)

    preview.write_status = TallyWriteBackStatus.PENDING
    await db.commit()

    txn = TallyVoucherWriteBackTransaction(
        organization_id=preview.organization_id, preview_id=preview.id, connection_id=preview.connection_id,
        correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt_number,
        status=TallyWriteBackStatus.PENDING, request_xml=preview.voucher_xml,
    )
    db.add(txn)
    await db.commit()
    await db.refresh(txn)

    asyncio.create_task(_post_in_background(
        preview.id, preview.organization_id, preview.connection_id, correlation_id, idempotency_key, attempt_number,
    ))
    return txn


async def _record_transaction(
    db: AsyncSession, *, organization_id: int | None, preview_id: int, connection_id: int | None,
    correlation_id: str, idempotency_key: str, attempt_number: int, status: TallyWriteBackStatus,
    request_xml: str | None = None, response_json: dict | None = None,
    error_code: str | None = None, error_message: str | None = None, is_retryable: bool = False,
    tally_master_id: str | None = None,
) -> TallyVoucherWriteBackTransaction:
    """Upsert by (idempotency_key, attempt_number) — same reasoning as
    invoice_erp_service._record_transaction(): post_voucher() already
    inserted a PENDING row at this exact key before handing off to the
    background loop, so this attempt's own outcome must update that same
    row, never insert a duplicate the UNIQUE constraint would reject."""
    result = await db.execute(
        select(TallyVoucherWriteBackTransaction).where(
            TallyVoucherWriteBackTransaction.idempotency_key == idempotency_key,
            TallyVoucherWriteBackTransaction.attempt_number == attempt_number,
        )
    )
    txn = result.scalar_one_or_none()
    if txn is None:
        txn = TallyVoucherWriteBackTransaction(
            organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
            correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt_number,
        )
        db.add(txn)
    txn.status = status
    txn.request_xml = request_xml
    txn.response_json = response_json
    txn.error_code = error_code
    txn.error_message = error_message[:2000] if error_message else None
    txn.is_retryable = is_retryable
    txn.tally_master_id = tally_master_id
    await db.commit()
    await db.refresh(txn)
    return txn


async def _report_writeback_dead_letter_incident(
    db: AsyncSession, *, organization_id: int | None, preview_id: int, reason: str,
) -> None:
    """TALLY-J — best-effort Incident + Alert on a DEAD_LETTER outcome,
    same convention as invoice_erp_service._report_dead_letter_incident()
    (see that function's own docstring): never pipeline-scoped, never
    allowed to affect the real write-back outcome it's reporting on, and
    any Incident created gets linked onto the AlertEvents fired in the
    same call so "View Incident" is reachable from the alert."""
    try:
        from app.models.incident import IncidentSeverity, IncidentSource
        from app.monitoring import alerts
        from app.services import incident_service

        preview = await db.get(TallyVoucherPreview, preview_id)
        label = f"invoice #{preview.invoice_id}" if preview else f"preview #{preview_id}"
        incident = await incident_service.report_incident(
            db, organization_id=organization_id, source=IncidentSource.TALLY_WRITEBACK.value,
            title=f"Tally write-back dead-lettered — {label}",
            description=reason, severity=IncidentSeverity.HIGH.value,
            dataset_name=label,
        )
        fired = await alerts.evaluate_rules(db, organization_id=organization_id, ctx={
            "event": "TALLY_WRITEBACK_FAILED",
            "message": f"{label} — Tally write-back dead-lettered: {reason}",
        })
        if fired:
            for event in fired:
                if event.incident_id is None:
                    event.incident_id = incident.id
            await db.commit()
    except Exception:  # noqa: BLE001 - reporting must never break the real write-back outcome
        pass


async def _post_in_background(
    preview_id: int, organization_id: int | None, connection_id: int | None,
    correlation_id: str, idempotency_key: str, start_attempt: int,
) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        attempt = start_attempt
        while attempt < start_attempt + MAX_ATTEMPTS:
            preview = await db.get(TallyVoucherPreview, preview_id)
            if preview is None:
                return

            if connection_id is not None and not _breaker_allows(connection_id):
                await _record_transaction(
                    db, organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=TallyWriteBackStatus.DEAD_LETTER, error_code="CIRCUIT_OPEN",
                    error_message="This Tally connection's circuit breaker is open — too many recent failures. Try again later or after fixing the connection.",
                    is_retryable=True, request_xml=preview.voucher_xml,
                )
                preview.write_status = TallyWriteBackStatus.DEAD_LETTER
                await db.commit()
                await _report_writeback_dead_letter_incident(
                    db, organization_id=organization_id, preview_id=preview_id,
                    reason="Blocked by an open circuit breaker — too many recent failures on this Tally connection.",
                )
                return

            conn = await connection_service.get_connection(db, connection_id, organization_id) if connection_id is not None else None
            if conn is None:
                await _record_transaction(
                    db, organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=TallyWriteBackStatus.FAILED, error_code="NO_CONNECTION", error_message="Tally connection no longer exists.",
                    request_xml=preview.voucher_xml,
                )
                preview.write_status = TallyWriteBackStatus.FAILED
                await db.commit()
                return

            connector = connection_service.build_connector(conn)
            try:
                result = await asyncio.to_thread(connector.post_voucher, preview.voucher_xml)
            except ConnectorError as exc:
                message = connector._scrub(str(exc))  # noqa: SLF001 - documented public convention
                retryable = "could not reach" in message.lower() or "timeout" in message.lower() or "timed out" in message.lower()
                if retryable and connection_id is not None:
                    _breaker_record_failure(connection_id)
                will_retry = retryable and (attempt + 1) < start_attempt + MAX_ATTEMPTS
                await _record_transaction(
                    db, organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=TallyWriteBackStatus.RETRYING if will_retry else (TallyWriteBackStatus.DEAD_LETTER if retryable else TallyWriteBackStatus.FAILED),
                    error_code="NETWORK" if retryable else "REJECTED", error_message=message, is_retryable=retryable,
                    request_xml=preview.voucher_xml,
                )
                if not will_retry:
                    preview.write_status = TallyWriteBackStatus.DEAD_LETTER if retryable else TallyWriteBackStatus.FAILED
                    await db.commit()
                    if retryable:
                        await _report_writeback_dead_letter_incident(
                            db, organization_id=organization_id, preview_id=preview_id,
                            reason=f"Retries exhausted after {attempt - start_attempt + 1} attempt(s): {message}",
                        )
                    return
                delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** (attempt - start_attempt))) * (1.0 + random.random() * 0.25)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            except Exception as exc:  # noqa: BLE001 - any other unexpected failure is still a real, recorded outcome
                await _record_transaction(
                    db, organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=TallyWriteBackStatus.FAILED, error_code="UNKNOWN", error_message=str(exc),
                    request_xml=preview.voucher_xml,
                )
                preview.write_status = TallyWriteBackStatus.FAILED
                await db.commit()
                return

            # Success — real Tally Import response, never a synthesized shape.
            if connection_id is not None:
                _breaker_record_success(connection_id)
            txn = await _record_transaction(
                db, organization_id=organization_id, preview_id=preview_id, connection_id=connection_id,
                correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                status=TallyWriteBackStatus.SUCCEEDED, request_xml=preview.voucher_xml, response_json=result,
                tally_master_id=str(result.get("last_master_id")) if result.get("last_master_id") else None,
            )
            preview.write_status = TallyWriteBackStatus.SUCCEEDED
            preview.tally_master_id = txn.tally_master_id
            preview.written_at = datetime.now(timezone.utc)
            await db.commit()

            # Post-write read-back verification (Phase 15) — never allowed
            # to undo or fail the write outcome itself, same convention as
            # invoice_erp_service's auto-reconciliation hook.
            try:
                await verify_write(db, await db.get(TallyVoucherPreview, preview_id, populate_existing=True), transaction_id=txn.id)
            except Exception:  # noqa: BLE001
                pass
            return


# ── Post-write verification ─────────────────────────────────────────

def _voucher_total(ledger_entries: list[dict]) -> float:
    return round(sum(float(e.get("amount") or 0) for e in ledger_entries if (e.get("is_deemed_positive") or "").lower() == "yes"), 2)


async def verify_write(db: AsyncSession, preview: TallyVoucherPreview, *, transaction_id: int | None = None) -> TallyVoucherWriteBackVerification:
    """Real GET-and-compare against Tally — never inferred from the write
    response. Raises nothing for a genuine "not found" or "fields don't
    match" outcome (both are real, recorded verdicts, same "every branch
    is a real result" convention as invoice_reconciliation_service.py) —
    only a caller precondition failure (never written yet) raises."""
    if preview.write_status != TallyWriteBackStatus.SUCCEEDED:
        raise TallyWriteBackError("This voucher has not been successfully written to Tally yet — nothing to verify.")

    conn = await connection_service.get_connection(db, preview.connection_id, preview.organization_id)
    if conn is None:
        verification = TallyVoucherWriteBackVerification(
            organization_id=preview.organization_id, preview_id=preview.id, transaction_id=transaction_id,
            status=TallyWriteBackVerificationStatus.FAILED, error_message="Tally connection no longer exists.",
        )
        db.add(verification)
        await db.commit()
        await db.refresh(verification)
        return verification

    invoice = await db.get(ApInvoice, preview.invoice_id)
    connector = connection_service.build_connector(conn)
    try:
        found = await asyncio.to_thread(
            connector.get_vouchers, company=preview.company_name, voucher_type=preview.voucher_type,
            party=preview.party_ledger_name, from_date=preview.voucher_date, to_date=preview.voucher_date,
        )
        vouchers = found.get("vouchers", [])
    except Exception as exc:  # noqa: BLE001 - a real GET failure is a real FAILED verdict, not an exception to the caller
        verification = TallyVoucherWriteBackVerification(
            organization_id=preview.organization_id, preview_id=preview.id, transaction_id=transaction_id,
            status=TallyWriteBackVerificationStatus.FAILED, error_message=connector._scrub(str(exc)),  # noqa: SLF001
        )
        db.add(verification)
        await db.commit()
        await db.refresh(verification)
        return verification

    reference = invoice.invoice_number if invoice else None
    match = None
    if reference:
        match = next((v for v in vouchers if v.get("reference") == reference), None)
    if match is None and len(vouchers) == 1:
        # No reference to key off (or it didn't match) but exactly one
        # voucher exists for this party/type/date — the same disambiguation
        # convention TALLY-E's reconciliation engine already uses when a
        # unique candidate exists without a hard key match.
        match = vouchers[0]

    if match is None:
        verification = TallyVoucherWriteBackVerification(
            organization_id=preview.organization_id, preview_id=preview.id, transaction_id=transaction_id,
            status=TallyWriteBackVerificationStatus.NOT_FOUND,
            field_comparisons=[{"field": "reference", "platform_value": reference, "tally_value": None, "result": "MISMATCH"}],
        )
        db.add(verification)
        await db.commit()
        await db.refresh(verification)
        return verification

    expected_total = _voucher_total(preview.ledger_entries or [])
    actual_total = _voucher_total(match.get("ledger_entries") or [])
    comparisons = [
        {"field": "party_ledger_name", "platform_value": preview.party_ledger_name, "tally_value": match.get("party_ledger_name"),
         "result": "MATCH" if (match.get("party_ledger_name") or "").strip() == (preview.party_ledger_name or "").strip() else "MISMATCH"},
        {"field": "voucher_type", "platform_value": preview.voucher_type, "tally_value": match.get("voucher_type"),
         "result": "MATCH" if (match.get("voucher_type") or "").strip() == (preview.voucher_type or "").strip() else "MISMATCH"},
        {"field": "total_amount", "platform_value": expected_total, "tally_value": actual_total,
         "result": "MATCH" if abs(expected_total - actual_total) < _COMPARE_TOLERANCE else "MISMATCH"},
    ]
    overall = TallyWriteBackVerificationStatus.VERIFIED if all(c["result"] == "MATCH" for c in comparisons) else TallyWriteBackVerificationStatus.MISMATCH

    verification = TallyVoucherWriteBackVerification(
        organization_id=preview.organization_id, preview_id=preview.id, transaction_id=transaction_id,
        status=overall, field_comparisons=comparisons,
    )
    db.add(verification)

    if overall == TallyWriteBackVerificationStatus.VERIFIED:
        preview.tally_voucher_guid = match.get("guid")
        preview.tally_voucher_number = match.get("voucher_number")

    await db.commit()
    await db.refresh(verification)
    return verification


async def list_transactions(db: AsyncSession, preview_id: int) -> list[TallyVoucherWriteBackTransaction]:
    """populate_existing=True is required, not optional: post_voucher()
    creates the first PENDING attempt row on the CALLER's session (e.g. an
    HTTP request handler's session, or a test's own fixture session), and
    every subsequent update to that same row happens on the background
    task's OWN session (_post_in_background — see its docstring on why it
    needs a fresh session at all). Without this flag, a caller session
    that already loaded that row into its identity map (any caller of
    post_voucher() that later also calls list_transactions() — which is
    exactly the browser polling flow this endpoint serves) gets back the
    STALE Python object from its own earlier read instead of the
    background task's committed update. Same real bug class as the one
    documented on invoice_erp_service.py's own auto-reconciliation
    populate_existing=True call — found here by reasoning about the same
    cross-session pattern before it ever caused a wrong status to render."""
    result = await db.execute(
        select(TallyVoucherWriteBackTransaction)
        .where(TallyVoucherWriteBackTransaction.preview_id == preview_id)
        .order_by(TallyVoucherWriteBackTransaction.created_at.desc())
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def list_verifications(db: AsyncSession, preview_id: int) -> list[TallyVoucherWriteBackVerification]:
    result = await db.execute(
        select(TallyVoucherWriteBackVerification).where(TallyVoucherWriteBackVerification.preview_id == preview_id).order_by(TallyVoucherWriteBackVerification.created_at.desc())
    )
    return list(result.scalars().all())
