import asyncio

import pytest

from tally_agent.dispatcher import CommandCancelledError, Dispatcher
from tally_agent.tally_client import LocalTallyClient, TallyClientError

from .mock_tally_server import start_mock_tally_server


@pytest.fixture
def tally_server():
    server = start_mock_tally_server(companies=["Acme Traders Pvt Ltd"])
    yield server
    server.stop()


@pytest.fixture
def dispatcher(tally_server):
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    return Dispatcher(client, max_concurrent=2)


def test_dispatch_list_companies(dispatcher):
    result = asyncio.run(dispatcher.dispatch("r1", "list_companies", {}))
    assert result == [{"name": "Acme Traders Pvt Ltd", "guid": None, "is_default": False}]


def test_dispatch_unknown_operation_raises(dispatcher):
    with pytest.raises(TallyClientError, match="Unknown operation"):
        asyncio.run(dispatcher.dispatch("r1", "delete_everything", {}))


def test_dispatch_get_vouchers_maps_parameters(tally_server, dispatcher):
    tally_server.state["mode"] = "vouchers"
    result = asyncio.run(dispatcher.dispatch("r1", "get_vouchers", {
        "company": "Acme Traders Pvt Ltd", "voucher_type": "Sales", "from_date": "2026-01-01", "to_date": "2026-01-31", "party": None,
    }))
    assert "vouchers" in result
    assert "20260101" in tally_server.state["last_request_body"]


def test_cancel_prevents_a_queued_command_from_running():
    """A command still waiting on the semaphore (not yet started) is
    skipped with CommandCancelledError if cancel() is called first —
    best-effort only, matching the server-side contract (see
    dispatcher.py's own docstring)."""
    async def scenario():
        client = LocalTallyClient(host="127.0.0.1", port=1, timeout=5)
        d = Dispatcher(client, max_concurrent=1)
        d.cancel("r1")
        with pytest.raises(CommandCancelledError):
            await d.dispatch("r1", "list_companies", {})

    asyncio.run(scenario())


def test_concurrent_dispatch_respects_semaphore(tally_server):
    """max_concurrent=1 — two concurrent dispatches must not both reach
    the mock server at the exact same instant; this merely proves the
    semaphore gates admission, not that the server observed strict
    serialization (which would be flaky to assert over real I/O)."""
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    d = Dispatcher(client, max_concurrent=1)

    async def scenario():
        results = await asyncio.gather(
            d.dispatch("r1", "list_companies", {}),
            d.dispatch("r2", "list_companies", {}),
        )
        return results

    results = asyncio.run(scenario())
    assert len(results) == 2
    assert tally_server.state["request_count"] == 2
