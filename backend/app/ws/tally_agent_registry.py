"""
tally_agent_registry.py — the in-memory registry of currently-connected
Tally agents. A module-level dict is correct and sufficient here because
this app runs as a single process with no --workers/replicas (confirmed
via Dockerfile/railway.json — plain `uvicorn app.main:app`) — one
process means one event loop means one source of truth for "which
agents are connected right now." A DB-backed "live session" table would
just be a second, harder-to-keep-consistent copy of what this registry
already knows (see models/tally_agent.py's own docstring).

set_main_loop()/get_main_loop() capture the process's one event loop
once at startup (from main.py's lifespan()) so that
AgentAwareTallyConnector (Phase Group 3) — whose methods run inside a
worker thread via asyncio.to_thread() — can schedule work back onto the
loop that actually owns the live WebSocket connections and their
pending-request futures, via asyncio.run_coroutine_threadsafe().
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class AgentDisconnectedError(Exception):
    """Raised against any still-pending command Future when its agent's
    connection goes away (clean or unclean) — deliberately not a
    connectors.base.ConnectorError subclass, so this module has no
    dependency on the connector layer; Phase Group 3's dispatch code
    catches this and translates it."""


@dataclass
class AgentConnectionState:
    websocket: WebSocket
    agent_installation_id: int
    organization_id: int | None
    connected_at: datetime
    last_heartbeat_at: datetime
    pending_requests: dict[str, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_connections: dict[int, AgentConnectionState] = {}
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop:
    if _main_loop is None:
        raise RuntimeError("The Tally Agent WebSocket gateway's main loop was never captured — lifespan() must call set_main_loop() at startup.")
    return _main_loop


async def register(installation_id: int, state: AgentConnectionState) -> None:
    """Supersedes any existing connection for this installation — "last
    writer wins." The old socket (if any) is closed with 4409 before the
    new one takes its place; its pending requests are failed just like
    any other disconnect (see unregister())."""
    existing = _connections.get(installation_id)
    if existing is not None:
        _fail_pending(existing, AgentDisconnectedError("Superseded by a new connection for the same agent installation."))
        try:
            await existing.websocket.close(code=4409, reason="Superseded by a new connection")
        except Exception:  # noqa: BLE001 - best-effort; the old socket may already be gone
            pass
    _connections[installation_id] = state


def unregister(installation_id: int) -> None:
    """Called from the WS endpoint's `finally` block — covers BOTH a
    graceful close and an unclean drop (the caller's own try/finally
    structure is what guarantees this runs either way). Fails every
    still-pending Future immediately rather than letting a blocked
    worker thread (see AgentAwareTallyConnector, Phase Group 3) wait out
    its full timeout for an agent that's already gone."""
    state = _connections.pop(installation_id, None)
    if state is not None:
        _fail_pending(state, AgentDisconnectedError("Agent disconnected."))


def _fail_pending(state: AgentConnectionState, exc: Exception) -> None:
    for request_id, fut in list(state.pending_requests.items()):
        if not fut.done():
            fut.set_exception(exc)
    state.pending_requests.clear()


def get(installation_id: int) -> AgentConnectionState | None:
    return _connections.get(installation_id)


def is_connected(installation_id: int) -> bool:
    return installation_id in _connections


async def force_disconnect(installation_id: int, *, code: int = 4403, reason: str = "") -> bool:
    """Called from api/tally_agents.py's rotate/revoke routes (Phase
    Group 4) — a credential that's just been rotated/revoked is no
    longer ACTIVE, so a currently-connected agent using it must be
    disconnected immediately rather than left talking on a session whose
    credential the server would now reject on any fresh auth attempt.
    Deliberately does NOT call unregister() itself — closing the socket
    here makes the WS endpoint's own receive loop observe the disconnect
    and run its existing try/finally cleanup (unregister + AuditLog +
    last_disconnected_at), the same single code path every other
    disconnect (graceful or unclean) already goes through. Returns True
    if an agent was actually connected to disconnect."""
    state = _connections.get(installation_id)
    if state is None:
        return False
    try:
        await state.websocket.close(code=code, reason=reason[:120])
    except Exception:  # noqa: BLE001 - best-effort; the socket may already be closing
        pass
    return True


def connected_installation_ids() -> set[int]:
    return set(_connections.keys())
