"""
dispatcher.py — routes an incoming 'command' message's operation string
to the matching LocalTallyClient method, bounded by a semaphore so a
burst of commands can't open unlimited concurrent connections to the
local TallyPrime instance.

The operation-name -> method mapping and parameter shapes here are the
other half of backend/app/connectors/application/tallyprime_agent.py's
_dispatch() calls — read directly from that file, not guessed, so a
command this agent receives always matches a parameter shape
LocalTallyClient actually accepts.
"""
from __future__ import annotations

import asyncio
import logging

from .tally_client import LocalTallyClient, TallyClientError

logger = logging.getLogger(__name__)


class CommandCancelledError(Exception):
    """Raised (cooperatively, best-effort) when a job_cancel arrives for
    a request_id that's still running — see Dispatcher.cancel()."""


class Dispatcher:
    """operation string -> LocalTallyClient method, run in a worker
    thread (LocalTallyClient's httpx calls are synchronous, matching the
    exact asyncio.to_thread() convention the backend's own services use
    for TallyPrimeConnector) and capped by a semaphore so at most
    `max_concurrent` local-Tally HTTP calls are ever in flight at once."""

    def __init__(self, tally_client: LocalTallyClient, *, max_concurrent: int = 4):
        self._client = tally_client
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cancelled: set[str] = set()

    _OPERATIONS = {
        "list_companies": lambda c, p: c.list_companies(),
        "post_voucher": lambda c, p: c.post_voucher(p["voucher_xml"]),
        "get_vouchers": lambda c, p: c.get_vouchers(
            company=p["company"], voucher_type=p.get("voucher_type"),
            from_date=p.get("from_date"), to_date=p.get("to_date"), party=p.get("party"),
        ),
        "get_party_master": lambda c, p: c.get_party_master(company=p["company"], party_type=p.get("party_type")),
        "get_ledger": lambda c, p: c.get_ledger(
            company=p["company"], party=p["party"], from_date=p.get("from_date"), to_date=p.get("to_date"),
        ),
        "get_payments": lambda c, p: c.get_payments(
            company=p["company"], from_date=p.get("from_date"), to_date=p.get("to_date"), party=p.get("party"),
        ),
        "get_bill_allocations": lambda c, p: c.get_bill_allocations(company=p["company"], party=p.get("party")),
        "get_outstanding": lambda c, p: c.get_outstanding(company=p["company"], party=p.get("party")),
    }

    async def dispatch(self, request_id: str, operation: str, parameters: dict):
        handler = self._OPERATIONS.get(operation)
        if handler is None:
            raise TallyClientError(f"Unknown operation '{operation}'.")
        async with self._semaphore:
            if request_id in self._cancelled:
                self._cancelled.discard(request_id)
                raise CommandCancelledError(f"Command {request_id} was cancelled before it started.")
            logger.info("Dispatching %s (request_id=%s)", operation, request_id)
            try:
                return await asyncio.to_thread(handler, self._client, parameters)
            finally:
                self._cancelled.discard(request_id)

    def cancel(self, request_id: str) -> None:
        """Best-effort only — a command already running inside
        asyncio.to_thread() cannot be interrupted mid-HTTP-call; this
        only prevents a not-yet-started queued command (waiting on the
        semaphore) from running at all, matching the same best-effort
        contract backend/app/ws/tally_agent_dispatch.cancel_command()
        documents on the server side."""
        self._cancelled.add(request_id)
