"""
AgentAwareTallyConnector — TallyPrime connectivity via the Agent Relay
(Multi-Client TallyPrime Integration Platform, Phase Group 3). A SECOND
transport alongside connectors/application/tallyprime.py's direct-HTTP
TallyPrimeConnector, for a customer whose TallyPrime machine is behind
NAT/a firewall and not directly reachable from this backend — see that
module's own docstring for the real XML/TDL request shapes this mirrors
(every operation here is relayed to a connected Windows agent, which
issues the identical request against its own local TallyPrime instance
and relays the identical response shape back).

Every method here returns EXACTLY the same shape TallyPrimeConnector's
same-named method does — verified by reading that file directly, not
assumed — so services/tally_extraction_service.py, tally_reconciliation_
service.py, and tally_writeback_service.py need zero changes to work
against either transport (see connection_service.build_connector(),
which is already fully generic over connection_type).

The core mechanism: BaseConnector methods are synchronous by this
codebase's own convention (see base.py's docstring) and are invoked from
async service code via asyncio.to_thread(), which runs them in a worker
thread. But the live WebSocket connection and its pending-request
futures live on the app's one main event loop (captured once at startup
— see app/ws/tally_agent_registry.set_main_loop()). The one real
cross-execution-context hop in this whole feature is therefore
worker-thread -> main-loop, and asyncio.run_coroutine_threadsafe() +
future.result(timeout=...) is the documented, correct primitive for
exactly that — see _dispatch() below.

Real, but NOT live-verified against an actual Windows agent or a real
TallyPrime install (neither exists in this environment) — live-verified
instead against a throwaway/scripted stand-in "agent" (a plain
`websockets` client answering command frames), proving the real
threading bridge, timeout handling, and mid-call-disconnect handling
all work correctly — same disclosed-but-real precedent as
tallyprime.py's own module docstring.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
import uuid
from datetime import date, datetime

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError, ConnectorValidationError

DEFAULT_TIMEOUT = 60.0  # higher than TallyPrimeConnector's 30s — a WS + local-HTTP round trip through the agent adds latency
TIMEOUT_GRACE_SECONDS = 5.0  # outermost safety net only; the real timeout/cleanup logic lives in tally_agent_dispatch.submit_command's own asyncio.wait_for


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class AgentAwareTallyConnector(BaseConnector):
    connection_type = "TALLY_PRIME_AGENT"

    def __init__(self, config: dict, secrets: dict, cache_key: str | None = None):
        super().__init__(config, secrets, cache_key)
        raw_id = (config or {}).get("agent_installation_id")
        if not raw_id:
            raise ConnectorValidationError("This TallyPrime (via Agent) connection has no Agent selected.")
        try:
            self._installation_id = int(raw_id)
        except (TypeError, ValueError):
            raise ConnectorValidationError(f"Invalid agent_installation_id: {raw_id!r}")

    def _timeout(self) -> float:
        try:
            return float(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT

    def _dispatch(self, operation: str, parameters: dict, *, source: str = "INTERACTIVE"):
        """The one real cross-execution-context hop — see module
        docstring. Runs on a worker thread (via asyncio.to_thread() in
        the calling service); blocks until the agent responds, times
        out, or disconnects mid-call."""
        from app.ws import tally_agent_dispatch, tally_agent_registry

        request_id = str(uuid.uuid4())
        timeout_seconds = self._timeout()
        main_loop = tally_agent_registry.get_main_loop()
        coro = tally_agent_dispatch.submit_command(
            self._installation_id, request_id, operation, parameters,
            timeout_seconds=timeout_seconds, source=source,
        )
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        try:
            return future.result(timeout=timeout_seconds + TIMEOUT_GRACE_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            # The inner asyncio.wait_for (submit_command's own timeout)
            # should fire first and already raise ConnectorError through
            # future.result() normally — this outer grace-period timeout
            # only fires if the main loop itself was too overloaded to
            # even run that cleanup in time. Still attempt a best-effort
            # cancel so the agent doesn't keep working for nothing.
            asyncio.run_coroutine_threadsafe(
                tally_agent_dispatch.cancel_command(self._installation_id, request_id), main_loop,
            )
            raise ConnectorError(f"Timed out after {timeout_seconds}s waiting for the agent to respond.") from exc

    # ── Connectivity ─────────────────────────────────────────────────

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            companies = self.list_companies()
        except ConnectorError as exc:
            return ConnectionTestResult(False, str(exc))
        latency_ms = (time.monotonic() - t0) * 1000
        configured = (self.config.get("company_name") or "").strip()
        if not companies:
            return ConnectionTestResult(
                False, "Reached the agent, but TallyPrime reported zero companies — open at least one company in Tally and try again.", latency_ms,
            )
        plural = "y" if len(companies) == 1 else "ies"
        names = [c.get("name") for c in companies]
        if configured and configured not in names:
            return ConnectionTestResult(
                True,
                f"Connected via agent — found {len(companies)} compan{plural}, but the configured company "
                f"'{configured}' is not among them. Use Discover Companies to pick a real one.",
                latency_ms,
            )
        message = f"Connected via agent — {len(companies)} compan{plural} available"
        message += f", targeting '{configured}'." if configured else ". No company selected yet — use Discover Companies."
        return ConnectionTestResult(True, message, latency_ms, server_version=None)

    def list_companies(self) -> list[dict]:
        data = self._dispatch("list_companies", {})
        return data if isinstance(data, list) else []

    def get_company(self, name: str) -> dict:
        for company in self.list_companies():
            if company.get("name") == name:
                return company
        raise ConnectorError(f"TallyPrime has no company named '{name}' on this instance.")

    # ── Controlled write-back ────────────────────────────────────────

    def post_voucher(self, voucher_xml: str) -> dict:
        """The agent is a dumb relay for writes — it ships this already-
        approved XML (built by tally_voucher_builder.build_voucher_xml(),
        the exact XML a human already saw in preview) verbatim to its
        local TallyPrime, preserving the write-safety/approval invariant
        across the extra hop. source="WRITEBACK" for AgentCommand's own
        audit trail."""
        return self._dispatch("post_voucher", {"voucher_xml": voucher_xml}, source="WRITEBACK")

    # ── Read-only extraction ─────────────────────────────────────────

    def get_vouchers(self, *, company: str, voucher_type: str | None = None, from_date=None,
                      to_date=None, party: str | None = None) -> dict:
        return self._dispatch("get_vouchers", {
            "company": company, "voucher_type": voucher_type,
            "from_date": _iso(from_date), "to_date": _iso(to_date), "party": party,
        }, source="EXTRACTION")

    def get_purchase_vouchers(self, *, company: str, **kwargs) -> dict:
        kwargs.setdefault("voucher_type", "Purchase")
        return self.get_vouchers(company=company, **kwargs)

    def get_sales_vouchers(self, *, company: str, **kwargs) -> dict:
        kwargs.setdefault("voucher_type", "Sales")
        return self.get_vouchers(company=company, **kwargs)

    def get_party_master(self, *, company: str, party_type: str | None = None, **kwargs) -> dict:
        return self._dispatch("get_party_master", {"company": company, "party_type": party_type}, source="EXTRACTION")

    def get_ledger(self, *, company: str, party: str, from_date=None, to_date=None) -> dict:
        return self._dispatch("get_ledger", {
            "company": company, "party": party, "from_date": _iso(from_date), "to_date": _iso(to_date),
        }, source="EXTRACTION")

    def get_payments(self, *, company: str, from_date=None, to_date=None, party: str | None = None) -> dict:
        return self._dispatch("get_payments", {
            "company": company, "from_date": _iso(from_date), "to_date": _iso(to_date), "party": party,
        }, source="EXTRACTION")

    def get_bill_allocations(self, *, company: str, party: str | None = None, **kwargs) -> dict:
        return self._dispatch("get_bill_allocations", {"company": company, "party": party}, source="EXTRACTION")

    def get_outstanding(self, *, company: str, party: str | None = None) -> dict:
        return self._dispatch("get_outstanding", {"company": company, "party": party}, source="EXTRACTION")
