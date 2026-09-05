"""
MockErpAdapter — a real in-process test double for connectors/application/
oracle_erp.py's OracleErpConnector (INV19), used only by this test suite.
Implements the exact same public methods invoice_erp_service.py actually
calls (test_connection/submit_invoice/get_invoice_status/cancel_invoice)
so the REAL retry-classification, exponential-backoff, circuit-breaker,
and idempotency-key logic in invoice_erp_service.py runs unmodified
against this double — nothing about the code under test is mocked, only
the network boundary to a real Oracle Fusion tenant (which this build has
never had access to — see oracle_erp.py's own docstring).

Deliberately NOT shipped under app/ — a mock ERP has no business existing
in production code, only in test fixtures. Behavior is controlled by
InvoiceNumber prefix, the same convention this project's INV9 live
verification used against a real standalone mock_oracle_erp.py HTTP
server (reused here so anyone who worked with that manual verification
recognizes the same conventions in the automated suite):

  INV-HAPPY-*      -> succeeds on the very first attempt.
  INV-RETRY-*      -> fails with a transient 503 twice, then succeeds on
                      the 3rd attempt (well within MAX_ATTEMPTS=5).
  INV-DEADLETTER-* -> always fails with a transient 503 (exhausts
                      MAX_ATTEMPTS -> DEAD_LETTER).
  INV-REJECT-*     -> always fails with a permanent, non-retryable 400
                      business-rule rejection.

Every call is recorded in `.calls` for test assertions (payload sent,
idempotency_key used). Successful submissions are kept in `._by_erp_id`
so get_invoice_status() can echo the exact posted fields back — the
"real ERP that agrees with itself" case a reconciliation test needs
by default; `.status_overrides[erp_id]` lets a test deliberately make a
field disagree to exercise the MISMATCH path.
"""
from __future__ import annotations

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError


class MockErpAdapter(BaseConnector):
    connection_type = "ORACLE_ERP_CLOUD"

    def __init__(self, config: dict | None = None, secrets: dict | None = None, cache_key: str | None = None):
        super().__init__(config or {}, secrets or {}, cache_key)
        self.calls: list[dict] = []
        self.status_overrides: dict[str, dict] = {}
        self._retry_counts: dict[str, int] = {}
        self._by_erp_id: dict[str, dict] = {}

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(True, "Mock Oracle ERP reachable.", 4.2, server_version="mock-1.0")

    def submit_invoice(self, payload: dict, *, idempotency_key: str | None = None) -> dict:
        self.calls.append({"payload": dict(payload), "idempotency_key": idempotency_key})
        invoice_number = str(payload.get("InvoiceNumber") or "")

        if invoice_number.startswith("INV-REJECT-"):
            raise ConnectorError("Oracle ERP Cloud rejected the invoice: HTTP 400 Business rule violation — duplicate PO reference.")
        if invoice_number.startswith("INV-DEADLETTER-"):
            raise ConnectorError("Server error reaching Oracle ERP Cloud: HTTP 503 Service Unavailable.")
        if invoice_number.startswith("INV-RETRY-"):
            n = self._retry_counts.get(idempotency_key, 0)
            self._retry_counts[idempotency_key] = n + 1
            if n < 2:
                raise ConnectorError("Server error reaching Oracle ERP Cloud: HTTP 503 Service Unavailable.")

        erp_id = f"mock-{abs(hash(idempotency_key or invoice_number)) % 10_000_000}"
        response = {**payload, "InvoiceId": erp_id, "ValidationStatus": "VALIDATED", "ApprovalStatus": "PENDING", "PaymentStatus": "UNPAID"}
        self._by_erp_id[erp_id] = response
        return response

    def get_invoice_status(self, erp_invoice_id: str) -> dict:
        base = self._by_erp_id.get(erp_invoice_id)
        if base is None:
            raise ConnectorError(f"Oracle ERP Cloud has no invoice with id {erp_invoice_id}.")
        result = dict(base)
        result.update(self.status_overrides.get(erp_invoice_id, {}))
        return result

    def cancel_invoice(self, erp_invoice_id: str) -> dict:
        return {"status": "CANCELLED", "invoice_id": erp_invoice_id}
