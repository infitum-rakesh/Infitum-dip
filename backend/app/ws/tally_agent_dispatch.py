"""
tally_agent_dispatch.py — the async half of the connector bridge (Phase
Group 3). Every function here is a coroutine that ONLY ever runs on the
main event loop (the one app/ws/tally_agent_registry.set_main_loop()
captured at startup) — see connectors/application/tallyprime_agent.py's
AgentAwareTallyConnector for the worker-thread side of this bridge,
which schedules these coroutines onto the main loop via
asyncio.run_coroutine_threadsafe() and blocks on the result.

_submit_command() is deliberately NOT called directly by anything
running in a worker thread — only AgentAwareTallyConnector._dispatch()
does that, and only via run_coroutine_threadsafe. Calling it directly
from a coroutine already on the main loop (e.g. a future async-native
caller) is equally correct, just unused today.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.connectors.base import ConnectorError
from app.database.connection import AsyncSessionLocal
from app.models.tally_agent import AgentCommand, AgentCommandSource, AgentCommandStatus, AgentInstallation
from app.ws import tally_agent_registry
from app.ws.tally_agent_registry import AgentDisconnectedError

logger = logging.getLogger(__name__)


def _summarize_result(data) -> dict | None:
    """A SMALL diagnostic summary only — never the full payload (see
    AgentCommand's own docstring). Generic rather than per-operation:
    most of this connector's return shapes are {"<noun>": [...]}."""
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if isinstance(value, list):
            return {f"{key}_count": len(value)}
    return {"keys": list(data.keys())} if data else {}


async def _finalize_command(
    command_id: int, status: AgentCommandStatus, *, dispatched_at: datetime | None = None,
    error_message: str | None = None, result_summary: dict | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        cmd = await db.get(AgentCommand, command_id)
        if cmd is None:
            return
        completed_at = datetime.now(timezone.utc)
        cmd.status = status
        cmd.completed_at = completed_at
        if dispatched_at is not None:
            ref = cmd.dispatched_at if cmd.dispatched_at.tzinfo else cmd.dispatched_at.replace(tzinfo=timezone.utc)
            cmd.latency_ms = (completed_at - ref).total_seconds() * 1000
        if error_message is not None:
            cmd.error_message = error_message[:500]
        if result_summary is not None:
            cmd.result_summary = result_summary
        await db.commit()


async def submit_command(
    installation_id: int, request_id: str, operation: str, parameters: dict, *,
    timeout_seconds: float, source: str = "INTERACTIVE", connection_id: int | None = None,
    actor_user_id: int | None = None,
) -> dict:
    """The one real dispatch path: resolves the live connection, records
    an AgentCommand row, sends the command frame, and awaits the
    correlated response — raising ConnectorError for every failure mode
    (agent offline, send failed, timeout, agent-reported error, agent
    disconnected mid-call) so AgentAwareTallyConnector's callers see
    exactly the same exception type the direct-HTTP connector raises."""
    state = tally_agent_registry.get(installation_id)
    if state is None:
        raise ConnectorError(f"Agent installation #{installation_id} is not currently connected.")

    async with AsyncSessionLocal() as db:
        installation = await db.get(AgentInstallation, installation_id)
        org_id = installation.organization_id if installation else None
        cmd = AgentCommand(
            organization_id=org_id, agent_installation_id=installation_id, connection_id=connection_id,
            request_id=request_id, operation=operation, parameters=parameters,
            status=AgentCommandStatus.SENT, initiated_by=actor_user_id,
            source=AgentCommandSource(source),
        )
        db.add(cmd)
        await db.commit()
        await db.refresh(cmd)
    command_id = cmd.id
    dispatched_at = datetime.now(timezone.utc)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending_requests[request_id] = fut

    try:
        async with state.send_lock:
            await state.websocket.send_text(json.dumps({
                "type": "command", "request_id": request_id, "operation": operation,
                "parameters": parameters, "timeout_seconds": timeout_seconds,
            }))
    except Exception as exc:
        state.pending_requests.pop(request_id, None)
        await _finalize_command(command_id, AgentCommandStatus.FAILED, dispatched_at=dispatched_at, error_message=f"Failed to send command to agent: {exc}")
        raise ConnectorError(f"Failed to send command to agent: {exc}") from exc

    try:
        result = await asyncio.wait_for(fut, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        state.pending_requests.pop(request_id, None)
        await _finalize_command(command_id, AgentCommandStatus.TIMED_OUT, dispatched_at=dispatched_at, error_message=f"Timed out after {timeout_seconds}s waiting for the agent to respond.")
        raise ConnectorError(f"Timed out after {timeout_seconds}s waiting for the agent to respond.")
    except AgentDisconnectedError as exc:
        await _finalize_command(command_id, AgentCommandStatus.FAILED, dispatched_at=dispatched_at, error_message=str(exc))
        raise ConnectorError(str(exc)) from exc

    success = bool(result.get("success"))
    if not success:
        error_message = result.get("error") or "The agent reported a failure with no further detail."
        await _finalize_command(command_id, AgentCommandStatus.FAILED, dispatched_at=dispatched_at, error_message=error_message)
        raise ConnectorError(error_message)

    data = result.get("data") or {}
    await _finalize_command(command_id, AgentCommandStatus.SUCCEEDED, dispatched_at=dispatched_at, result_summary=_summarize_result(data))
    return data


async def cancel_command(installation_id: int, request_id: str) -> None:
    """Best-effort — sent after a timeout so the agent can stop doing
    pointless work for a caller that's already given up; never raises,
    since the caller is already in the middle of raising its own
    ConnectorError for the timeout."""
    state = tally_agent_registry.get(installation_id)
    if state is None:
        return
    state.pending_requests.pop(request_id, None)
    try:
        async with state.send_lock:
            await state.websocket.send_text(json.dumps({"type": "job_cancel", "request_id": request_id}))
    except Exception:  # noqa: BLE001 - best-effort
        logger.debug("Could not send job_cancel for request_id=%s (installation_id=%s) — connection likely already gone.", request_id, installation_id)
