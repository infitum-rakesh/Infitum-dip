"""
invoice_erp_service.py — idempotent ERP posting + retry + circuit breaker
(INV9). Real HTTP calls run through connectors/application/oracle_erp.py
(via connection_service.build_connector(), reused unmodified — an ERP
connection is a Connection like any other) — this module owns only the
posting WORKFLOW around that call: pre-call validation, idempotency,
retry/backoff classification, and a small in-process per-connection
circuit breaker. Disclosed, same as oracle_erp.py itself: never live-
verified against a real Oracle Fusion ERP Cloud tenant (none available in
this environment) — the idempotency/retry/circuit-breaker logic itself
IS fully real and independently verifiable against a local mock HTTP
server standing in for the documented contract (see INV9 verification).

Idempotency (Technical Design §5 — "never create a duplicate ERP
invoice"): idempotency_key is DETERMINISTIC per (invoice, erp_connection)
— `ap-invoice-{id}-erp-{connection_id}` — and stays the SAME across every
attempt row for that pair, whether the attempt came from this module's
own automatic retry loop or a later manual re-trigger (POST .../erp/post
called again after a prior DEAD_LETTER). That is what actually enforces
"never repeat the ERP call once one attempt SUCCEEDED": the pre-call
check below short-circuits on invoice.erp_status == SUCCEEDED before any
HTTP call is made, and the SAME key is also sent as the ERP's own
Idempotency-Key header so a genuinely idempotency-aware ERP endpoint
de-dupes on its own side too — belt and suspenders, never assumed to be
guaranteed by the platform. correlation_id is a fresh id per POST call
(one per "user action" / one per background retry loop it kicks off) —
distinct from idempotency_key, purely for tracing which attempt rows
belong to the same button-press.

Retry classification: transient (network unreachable/timeout, or the
ERP's own real HTTP status 408/429/5xx) -> exponential backoff with
jitter, up to MAX_ATTEMPTS. Permanent (any other 4xx — the ERP's genuine
business-rule rejection — or a pre-call validation failure) -> no retry,
immediate terminal FAILED. A transient error that's still failing after
MAX_ATTEMPTS lands as DEAD_LETTER (distinct from FAILED) — signals "this
needs a human, not another retry."

Circuit breaker: small in-process per-erp_connection_id state (same
spirit as sql_workspace_service.py's active-run registry — see that
module) — CLOSED (normal) -> OPEN after CIRCUIT_FAILURE_THRESHOLD
consecutive RETRYABLE (transient/infrastructure) failures for that
connection (protects a genuinely-down ERP from being hammered by every
queued invoice) -> HALF_OPEN after a cooldown lets exactly one attempt
through as a probe; that attempt's own success/failure decides CLOSED or
back to OPEN. A permanent business-validation rejection (4xx that isn't
408/429) never counts toward this — it says nothing about whether the
ERP is reachable, so a batch of genuinely-invalid invoices must never
trip the breaker and block healthy ones behind them. Never persisted —
restarts cold on process restart, matching every other in-process
registry in this codebase.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_engine_registry
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceLine, ApInvoiceStatus
from app.models.invoice_intelligence.erp import ApInvoiceErpStatus, ApInvoiceErpTransaction
from app.models.invoice_intelligence.learning import ApInvoiceAuditEventType
from app.services import connection_service
from app.services.invoice import invoice_erp_mapping_service
from app.services.invoice.invoice_erp_mapping_service import InvoiceErpValidationError

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 60.0
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 60.0

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class InvoiceErpError(Exception):
    """Raised for a genuine caller-facing precondition failure — invoice
    already posted, no ERP connection, etc. Never used for a transient
    ERP-side failure (those are recorded as transaction rows, not raised)."""


# ── Circuit breaker — in-process, per erp_connection_id ────────────

_circuit_state: dict[int, dict] = {}


def _breaker_allows(erp_connection_id: int) -> bool:
    st = _circuit_state.get(erp_connection_id)
    if st is None or st["state"] == "CLOSED":
        return True
    if st["state"] == "OPEN":
        if time.monotonic() - st["opened_at"] >= CIRCUIT_COOLDOWN_SECONDS:
            st["state"] = "HALF_OPEN"
            st["probing"] = True  # exactly one probe attempt admitted per HALF_OPEN cycle
            return True
        return False
    # HALF_OPEN — admit exactly the single in-flight probe; every other
    # caller that lands here while it's still resolving is blocked, same
    # as OPEN, rather than letting an unbounded number of concurrent
    # invoices all sneak through as "the one probe."
    if st.get("probing"):
        return False
    st["probing"] = True
    return True


def _breaker_record_success(erp_connection_id: int) -> None:
    _circuit_state[erp_connection_id] = {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False}


def _breaker_record_failure(erp_connection_id: int) -> None:
    st = _circuit_state.setdefault(erp_connection_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False})
    st["failures"] += 1
    st["probing"] = False
    if st["state"] == "HALF_OPEN" or st["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        st["state"] = "OPEN"
        st["opened_at"] = time.monotonic()


def _breaker_note_reachable(erp_connection_id: int) -> None:
    """A non-retryable (permanent business) error still proves the ERP
    itself is reachable and responding — if this was resolving a
    HALF_OPEN probe, that's real evidence the outage is over, so close
    the breaker even though this particular invoice's post still failed
    for business reasons. Never invents recovery for anything already
    OPEN/CLOSED; only resolves an actual in-flight probe."""
    st = _circuit_state.get(erp_connection_id)
    if st and st["state"] == "HALF_OPEN":
        _breaker_record_success(erp_connection_id)


def circuit_status(erp_connection_id: int) -> dict:
    st = _circuit_state.get(erp_connection_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0})
    return dict(st)


# ── Error classification ────────────────────────────────────────────

def _classify_error(exc: Exception) -> tuple[bool, str, str]:
    """Returns (is_retryable, error_code, message)."""
    message = str(exc)
    if isinstance(exc, InvoiceErpValidationError):
        return False, "VALIDATION", message
    m = _HTTP_STATUS_RE.search(message)
    if m:
        status = int(m.group(1))
        retryable = status in (408, 429) or status >= 500
        return retryable, str(status), message
    if "could not reach" in message.lower() or "timeout" in message.lower() or "timed out" in message.lower():
        return True, "NETWORK", message
    return False, "UNKNOWN", message


def _idempotency_key(invoice_id: int, erp_connection_id: int) -> str:
    return f"ap-invoice-{invoice_id}-erp-{erp_connection_id}"


async def _next_attempt_number(db: AsyncSession, idempotency_key: str) -> int:
    result = await db.execute(select(func.max(ApInvoiceErpTransaction.attempt_number)).where(ApInvoiceErpTransaction.idempotency_key == idempotency_key))
    return (result.scalar_one() or 0) + 1


# ── Public entry point ──────────────────────────────────────────────

async def post_invoice(db: AsyncSession, invoice: ApInvoice, *, erp_connection_id: int) -> ApInvoiceErpTransaction:
    """Pre-call validation + idempotent short-circuit, then hands off to a
    background retry loop (same non-blocking convention as
    document_service.start_processing() — a real ERP round trip plus
    backoff can genuinely take well over the request timeout). Returns
    the first (PENDING) transaction row immediately; poll GET
    .../erp/transactions or the invoice's erp_status for the outcome."""
    if invoice.erp_status == ApInvoiceErpStatus.SUCCEEDED:
        raise InvoiceErpError("This invoice has already been posted to the ERP successfully — post again only after a genuine correction.")
    if invoice.final_decision == ApInvoiceDecision.REJECT:
        raise InvoiceErpError("This invoice was rejected by validation (e.g. a blocked duplicate) — it cannot be posted to the ERP.")
    if invoice.erp_status in (ApInvoiceErpStatus.PENDING, ApInvoiceErpStatus.RETRYING):
        # INV18 hardening — found live while adding the Idempotency-Key
        # preview: without this guard, two near-simultaneous POST .../erp/post
        # calls (a double-click, or a frontend retry after a slow response)
        # would both pass the SUCCEEDED check above before either one's
        # background loop finishes, each opening its own retry loop against
        # the SAME idempotency_key. The ERP's own Idempotency-Key dedup
        # (documented as "belt and suspenders" in this module's docstring)
        # would likely absorb it, but this stops the duplicate attempt at
        # our own layer instead of relying on that.
        raise InvoiceErpError("This invoice already has an ERP post in progress — wait for it to finish (or fail) before posting again.")

    conn = await connection_service.get_connection(db, erp_connection_id, invoice.organization_id)
    if conn is None:
        raise InvoiceErpError("ERP connection not found.")

    await invoice_erp_mapping_service.seed_default_mapping(db, invoice.organization_id, erp_connection_id)

    idempotency_key = _idempotency_key(invoice.id, erp_connection_id)
    correlation_id = uuid.uuid4().hex
    attempt_number = await _next_attempt_number(db, idempotency_key)

    invoice.erp_connection_id = erp_connection_id
    invoice.erp_status = ApInvoiceErpStatus.PENDING
    invoice.status = ApInvoiceStatus.POSTING
    await db.commit()

    txn = ApInvoiceErpTransaction(
        organization_id=invoice.organization_id, invoice_id=invoice.id, erp_connection_id=erp_connection_id,
        correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt_number,
        status=ApInvoiceErpStatus.PENDING,
    )
    db.add(txn)
    await db.commit()
    await db.refresh(txn)

    await _record_lifecycle_event(
        db, invoice_id=invoice.id, organization_id=invoice.organization_id,
        event_type=ApInvoiceAuditEventType.ERP_SUBMITTED,
        message=f"Submitted to ERP connection {erp_connection_id} (correlation {correlation_id}).",
        details={"erp_connection_id": erp_connection_id, "correlation_id": correlation_id, "idempotency_key": idempotency_key},
    )

    asyncio.create_task(_post_in_background(
        invoice.id, invoice.organization_id, erp_connection_id, correlation_id, idempotency_key, attempt_number,
    ))
    return txn


async def _record_transaction(
    db: AsyncSession, *, organization_id: int | None, invoice_id: int, erp_connection_id: int,
    correlation_id: str, idempotency_key: str, attempt_number: int, status: ApInvoiceErpStatus,
    request_json: dict | None = None, response_json: dict | None = None,
    error_code: str | None = None, error_message: str | None = None, is_retryable: bool = False,
    erp_transaction_id: str | None = None,
) -> ApInvoiceErpTransaction:
    """Upsert by (idempotency_key, attempt_number) — the model's own
    UNIQUE constraint. post_invoice() already inserts a PENDING row at
    (idempotency_key, start_attempt) before handing off to the background
    loop, so THAT attempt's own outcome must update the same row, never
    insert a second one at an identical key (which the UNIQUE constraint
    would reject outright). Every later attempt_number in a retry chain
    genuinely has no existing row yet, so this naturally falls through to
    a plain insert for those."""
    result = await db.execute(
        select(ApInvoiceErpTransaction).where(
            ApInvoiceErpTransaction.idempotency_key == idempotency_key, ApInvoiceErpTransaction.attempt_number == attempt_number,
        )
    )
    txn = result.scalar_one_or_none()
    if txn is None:
        txn = ApInvoiceErpTransaction(
            organization_id=organization_id, invoice_id=invoice_id, erp_connection_id=erp_connection_id,
            correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt_number,
        )
        db.add(txn)
    txn.status = status
    txn.erp_transaction_id = erp_transaction_id
    txn.request_json = request_json
    txn.response_json = response_json
    txn.error_code = error_code
    txn.error_message = error_message[:2000] if error_message else None
    txn.is_retryable = is_retryable
    await db.commit()
    await db.refresh(txn)
    return txn


async def _finalize_invoice(db: AsyncSession, invoice_id: int, erp_status: ApInvoiceErpStatus, erp_transaction_id: str | None) -> None:
    invoice = await db.get(ApInvoice, invoice_id)
    if invoice is None:
        return
    invoice.erp_status = erp_status
    if erp_transaction_id:
        invoice.erp_transaction_id = erp_transaction_id
    invoice.status = ApInvoiceStatus.POSTED if erp_status == ApInvoiceErpStatus.SUCCEEDED else ApInvoiceStatus.EXCEPTION
    await db.commit()


async def _record_lifecycle_event(
    db: AsyncSession, *, invoice_id: int, organization_id: int | None,
    event_type: ApInvoiceAuditEventType, message: str | None, details: dict,
) -> None:
    """INV13 — best-effort narrow lifecycle event, never fails posting."""
    try:
        from app.services.invoice import invoice_learning_service
        await invoice_learning_service.record_event(
            db, invoice_id=invoice_id, organization_id=organization_id, event_type=event_type, message=message, details=details,
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        pass


async def _report_dead_letter_incident(db: AsyncSession, *, organization_id: int | None, invoice_id: int, reason: str) -> None:
    """INV12 — best-effort Incident + Alert on a DEAD_LETTER outcome
    (retries exhausted, or the circuit breaker blocked the attempt
    outright). Same convention as anomaly_service.run_job()'s own
    post-run incident/alert hook: never pipeline-scoped (no pipeline_id
    in ctx, so only org-wide AlertRules match), never allowed to affect
    the real posting outcome it's reporting on, and any Incident created
    gets linked onto the AlertEvents fired in the same call so "View
    Incident" is reachable from the alert."""
    try:
        from app.models.incident import IncidentSeverity, IncidentSource
        from app.monitoring import alerts
        from app.services import incident_service

        invoice = await db.get(ApInvoice, invoice_id)
        label = invoice.invoice_number if invoice and invoice.invoice_number else f"invoice #{invoice_id}"
        incident = await incident_service.report_incident(
            db, organization_id=organization_id, source=IncidentSource.INVOICE_ERP_POSTING.value,
            title=f"ERP posting dead-lettered — {label}",
            description=reason, severity=IncidentSeverity.HIGH.value,
            dataset_name=label,
        )
        fired = await alerts.evaluate_rules(db, organization_id=organization_id, ctx={
            "event": "INVOICE_ERP_POSTING_FAILED",
            "message": f"Invoice {label} — ERP posting dead-lettered: {reason}",
        })
        if fired:
            for event in fired:
                if event.incident_id is None:
                    event.incident_id = incident.id
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - monitoring/incident wiring must never fail a real posting outcome
        import logging
        logging.getLogger(__name__).error("invoice_erp_service dead-letter incident hook failed for invoice %s: %s", invoice_id, exc)


async def _post_in_background(
    invoice_id: int, organization_id: int | None, erp_connection_id: int,
    correlation_id: str, idempotency_key: str, start_attempt: int,
) -> None:
    """Runs detached from the request — needs its own session, same
    convention as document_service._process_in_background(). The FIRST
    transaction row (PENDING) was already created by post_invoice() above
    at start_attempt; this function's first HTTP attempt updates/records
    against that same attempt_number rather than creating a duplicate
    PENDING row for it."""
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        attempt = start_attempt
        while attempt < start_attempt + MAX_ATTEMPTS:
            if not _breaker_allows(erp_connection_id):
                await _record_transaction(
                    db, organization_id=organization_id, invoice_id=invoice_id, erp_connection_id=erp_connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=ApInvoiceErpStatus.DEAD_LETTER, error_code="CIRCUIT_OPEN",
                    error_message="ERP connection's circuit breaker is open — too many recent failures. Try again later or after fixing the connection.",
                    is_retryable=True,
                )
                await _finalize_invoice(db, invoice_id, ApInvoiceErpStatus.FAILED, None)
                await _report_dead_letter_incident(
                    db, organization_id=organization_id, invoice_id=invoice_id,
                    reason="Blocked by an open circuit breaker — too many recent ERP failures on this connection.",
                )
                return

            invoice = await db.get(ApInvoice, invoice_id)
            if invoice is None:
                return
            lines_result = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice_id).order_by(ApInvoiceLine.line_number.asc()))
            lines = list(lines_result.scalars().all())
            conn = await connection_service.get_connection(db, erp_connection_id, organization_id)
            if conn is None:
                await _record_transaction(
                    db, organization_id=organization_id, invoice_id=invoice_id, erp_connection_id=erp_connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=ApInvoiceErpStatus.FAILED, error_code="NO_CONNECTION", error_message="ERP connection no longer exists.",
                )
                await _finalize_invoice(db, invoice_id, ApInvoiceErpStatus.FAILED, None)
                return

            payload: dict | None = None
            connector = None
            try:
                mapping_rows = await invoice_erp_mapping_service.get_current_mapping(db, organization_id, erp_connection_id)
                payload = invoice_erp_mapping_service.build_erp_payload(invoice, lines, mapping_rows)
                connector = connection_service.build_connector(conn)
                result = await asyncio.to_thread(connector.submit_invoice, payload, idempotency_key=idempotency_key)
            except Exception as exc:  # noqa: BLE001 - classified below; every branch is a real, recorded outcome, never silently swallowed
                retryable, error_code, message = _classify_error(exc)
                scrubbed = connector._scrub(message) if connector is not None else message  # noqa: SLF001 - documented public convention, see ApInvoiceErpTransaction's own docstring
                # Only a RETRYABLE (transient/infrastructure) failure counts
                # toward the breaker — a permanent business-validation
                # rejection (bad BusinessUnit, wrong amount format, ...)
                # says nothing about whether the ERP itself is reachable,
                # so a batch of genuinely-invalid invoices must never trip
                # the breaker and block healthy ones behind them.
                if retryable:
                    _breaker_record_failure(erp_connection_id)
                elif error_code not in ("VALIDATION", "NETWORK", "UNKNOWN"):
                    # A genuine non-retryable HTTP response (a real 4xx the
                    # ERP itself returned) proves the connection IS up —
                    # if a HALF_OPEN probe landed here, that's real
                    # evidence the outage is over. A pre-call
                    # InvoiceErpValidationError never reached the ERP at
                    # all (error_code stays "VALIDATION") and proves
                    # nothing about reachability, so it's excluded here.
                    _breaker_note_reachable(erp_connection_id)
                will_retry = retryable and (attempt + 1) < start_attempt + MAX_ATTEMPTS
                await _record_transaction(
                    db, organization_id=organization_id, invoice_id=invoice_id, erp_connection_id=erp_connection_id,
                    correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                    status=ApInvoiceErpStatus.RETRYING if will_retry else (ApInvoiceErpStatus.DEAD_LETTER if retryable else ApInvoiceErpStatus.FAILED),
                    error_code=error_code, error_message=scrubbed, is_retryable=retryable,
                    request_json=payload,
                )
                if not will_retry:
                    final = ApInvoiceErpStatus.DEAD_LETTER if retryable else ApInvoiceErpStatus.FAILED
                    await _finalize_invoice(db, invoice_id, final, None)
                    if final == ApInvoiceErpStatus.DEAD_LETTER:
                        await _report_dead_letter_incident(
                            db, organization_id=organization_id, invoice_id=invoice_id,
                            reason=f"Retries exhausted after {attempt - start_attempt + 1} attempt(s) — last error: {scrubbed}",
                        )
                    await _record_lifecycle_event(
                        db, invoice_id=invoice_id, organization_id=organization_id,
                        event_type=ApInvoiceAuditEventType.ERP_DEAD_LETTERED if final == ApInvoiceErpStatus.DEAD_LETTER else ApInvoiceAuditEventType.ERP_FAILED,
                        message=scrubbed, details={"error_code": error_code, "attempt_number": attempt},
                    )
                    return
                await _record_lifecycle_event(
                    db, invoice_id=invoice_id, organization_id=organization_id,
                    event_type=ApInvoiceAuditEventType.ERP_RETRIED,
                    message=scrubbed, details={"error_code": error_code, "attempt_number": attempt},
                )
                delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** (attempt - start_attempt))) * (1.0 + random.random() * 0.25)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            # Success — real ERP response, never a synthesized shape.
            _breaker_record_success(erp_connection_id)
            erp_invoice_id = result.get("InvoiceId") or result.get("invoiceId") or result.get("InvoiceNumber")
            scrubbed_response = {k: (connector._scrub(str(v)) if isinstance(v, str) else v) for k, v in result.items()}  # noqa: SLF001
            await _record_transaction(
                db, organization_id=organization_id, invoice_id=invoice_id, erp_connection_id=erp_connection_id,
                correlation_id=correlation_id, idempotency_key=idempotency_key, attempt_number=attempt,
                status=ApInvoiceErpStatus.SUCCEEDED, request_json=payload, response_json=scrubbed_response,
                erp_transaction_id=str(erp_invoice_id) if erp_invoice_id else None,
            )
            await _finalize_invoice(db, invoice_id, ApInvoiceErpStatus.SUCCEEDED, str(erp_invoice_id) if erp_invoice_id else None)
            await _record_lifecycle_event(
                db, invoice_id=invoice_id, organization_id=organization_id,
                event_type=ApInvoiceAuditEventType.ERP_SUCCEEDED,
                message=f"ERP transaction {erp_invoice_id}.", details={"erp_transaction_id": str(erp_invoice_id) if erp_invoice_id else None, "attempt_number": attempt},
            )

            # Auto-reconciliation (INV10) — real GET-and-compare right
            # after a successful post. Never allowed to undo or fail the
            # posting outcome itself: a reconciliation bug must never
            # make an otherwise-successful ERP post look broken.
            try:
                from app.services.invoice import invoice_reconciliation_service
                # populate_existing=True is required here, not optional:
                # _finalize_invoice() just committed on this same session,
                # which expires every attribute of every object already in
                # its identity map (including this same invoice row from
                # earlier in the loop). A plain db.get() returns that same
                # expired instance from the identity map without re-querying
                # it; build_erp_payload()'s getattr()-based column read then
                # tries to lazy-load an expired attribute from plain sync
                # code, which AsyncSession can't do outside an awaited call
                # and raises (previously swallowed by the bare except below,
                # silently no-op'ing every automatic post-success
                # reconciliation — found live during INV10 verification).
                invoice = await db.get(ApInvoice, invoice_id, populate_existing=True)
                if invoice is not None:
                    await invoice_reconciliation_service.reconcile_invoice(db, invoice)
            except Exception:  # noqa: BLE001 - see comment above
                pass
            return


# ── Preview / simulation mode (INV18) ───────────────────────────────

async def preview_erp_payload(db: AsyncSession, invoice: ApInvoice, *, erp_connection_id: int) -> dict:
    """Real dry run — builds the EXACT payload the current org/connection
    mapping would produce for this invoice, using the same
    invoice_erp_mapping_service.build_erp_payload() the live background
    posting loop calls, but never opens a network connection to the ERP,
    never writes an ApInvoiceErpTransaction row, and never touches
    invoice.erp_status. Lets an admin verify a mapping (or a just-published
    new version of one) against a real invoice before actually posting —
    genuinely new for INV18, distinct from INV19's MockERPAdapter (which
    stands in for the ERP itself in automated tests; this instead skips
    calling any ERP, mock or real). Surfaces the SAME idempotency_key a
    real post would use, so what's shown here is exactly what would be
    sent, not an approximation."""
    conn = await connection_service.get_connection(db, erp_connection_id, invoice.organization_id)
    if conn is None:
        raise InvoiceErpError("ERP connection not found.")

    await invoice_erp_mapping_service.seed_default_mapping(db, invoice.organization_id, erp_connection_id)
    mapping_rows = await invoice_erp_mapping_service.get_current_mapping(db, invoice.organization_id, erp_connection_id)
    lines_result = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id).order_by(ApInvoiceLine.line_number.asc()))
    lines = list(lines_result.scalars().all())

    try:
        payload = invoice_erp_mapping_service.build_erp_payload(invoice, lines, mapping_rows)
    except InvoiceErpValidationError as exc:
        # Mirrors exactly what a real post_invoice() attempt would hit as
        # its own pre-call VALIDATION failure — surfaced here instead of
        # only discovering it after clicking the real "Post to ERP" button.
        raise InvoiceErpError(str(exc)) from exc

    return {
        "erp_connection_id": erp_connection_id,
        "erp_connection_name": conn.name,
        "idempotency_key": _idempotency_key(invoice.id, erp_connection_id),
        "endpoint_host": (conn.config or {}).get("host"),
        "mapping_version": mapping_rows[0].version if mapping_rows else None,
        "payload": payload,
    }


# ── Status / cancel / history ───────────────────────────────────────

async def list_transactions(db: AsyncSession, invoice_id: int) -> list[ApInvoiceErpTransaction]:
    result = await db.execute(
        select(ApInvoiceErpTransaction).where(ApInvoiceErpTransaction.invoice_id == invoice_id).order_by(ApInvoiceErpTransaction.created_at.desc())
    )
    return list(result.scalars().all())


async def get_live_status(db: AsyncSession, invoice: ApInvoice) -> dict:
    """Real GET against the ERP's own status fields — used ahead of
    reconciliation (INV10). Raises InvoiceErpError if this invoice was
    never actually posted (nothing to check)."""
    if not invoice.erp_transaction_id or not invoice.erp_connection_id:
        raise InvoiceErpError("This invoice has no ERP transaction id yet — it hasn't been successfully posted.")
    conn = await connection_service.get_connection(db, invoice.erp_connection_id, invoice.organization_id)
    if conn is None:
        raise InvoiceErpError("ERP connection not found.")
    connector = connection_service.build_connector(conn)
    try:
        return await asyncio.to_thread(connector.get_invoice_status, invoice.erp_transaction_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a real, scrubbed error, not swallowed
        raise InvoiceErpError(connector._scrub(str(exc))) from exc  # noqa: SLF001


async def cancel_posted_invoice(db: AsyncSession, invoice: ApInvoice) -> dict:
    if not invoice.erp_transaction_id or not invoice.erp_connection_id:
        raise InvoiceErpError("This invoice has no ERP transaction id yet — nothing to cancel.")
    conn = await connection_service.get_connection(db, invoice.erp_connection_id, invoice.organization_id)
    if conn is None:
        raise InvoiceErpError("ERP connection not found.")
    connector = connection_service.build_connector(conn)
    try:
        result = await asyncio.to_thread(connector.cancel_invoice, invoice.erp_transaction_id)
    except Exception as exc:  # noqa: BLE001
        raise InvoiceErpError(connector._scrub(str(exc))) from exc  # noqa: SLF001
    invoice.erp_status = ApInvoiceErpStatus.NOT_SUBMITTED
    invoice.status = ApInvoiceStatus.EXCEPTION
    await db.commit()
    return result
