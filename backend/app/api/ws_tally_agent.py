"""
ws_tally_agent.py — the WebSocket gateway a Windows Tally agent connects
to (Multi-Client TallyPrime Integration Platform, Phase Group 2). The
first WebSocket endpoint in this codebase — see app/ws/tally_agent_
registry.py's own docstring for the in-memory connection-registry
design this builds on.

Protocol (JSON text frames, one message type per frame via a "type"
key):
  agent -> server : auth (handshake, must be the first message)
  server -> agent : auth_ack
  agent -> server : heartbeat (every ~30s)
  server -> agent : heartbeat_ack
  server -> agent : command            (Phase Group 3 — not sent yet)
  agent -> server : command_result
  server -> agent : job_cancel         (Phase Group 3 — not sent yet)
  either direction: error              (protocol-level only — a
                                         command-scoped failure lives in
                                         command_result.error instead)

Close codes used: 4400 (protocol violation — bad/missing auth, message
before auth, oversized frame), 4401 (auth rejected), 4403 (installation
revoked/disabled, or a forced disconnect from rotate/revoke), 4409
(superseded by a newer connection for the same installation).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database.connection import AsyncSessionLocal
from app.models.audit_log import AuditStatus, record_event
from app.models.tally_agent import AgentInstallation
from app.services import tally_agent_service as svc
from app.ws.tally_agent_registry import AgentConnectionState, get, register, unregister

logger = logging.getLogger(__name__)

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 30
RECEIVE_TIMEOUT_SECONDS = HEARTBEAT_INTERVAL_SECONDS * 3  # catches a silently NAT-dropped connection that never sends a close frame
MAX_FRAME_BYTES = 10 * 1024 * 1024  # comfortably covers one extraction chunk; a caller needing more should shrink its chunk


async def _close_protocol_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_text(json.dumps({"type": "error", "code": "PROTOCOL_ERROR", "message": message}))
    except Exception:  # noqa: BLE001 - best-effort, the socket may already be unusable
        pass
    await websocket.close(code=4400, reason=message[:120])


def _frame_size(raw: dict) -> int:
    if "text" in raw and raw["text"] is not None:
        return len(raw["text"].encode("utf-8"))
    if "bytes" in raw and raw["bytes"] is not None:
        return len(raw["bytes"])
    return 0


async def _receive_json(websocket: WebSocket, *, timeout: float | None = None) -> dict | None:
    """Returns the parsed message, or None if the connection closed
    normally. Raises WebSocketDisconnect on an unclean drop, or closes
    the socket itself (returning None) on an oversized/malformed frame
    or a receive timeout — all three are "this connection is over" from
    the caller's point of view, just with different diagnostics already
    sent/logged here."""
    import asyncio
    try:
        raw = await asyncio.wait_for(websocket.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        await _close_protocol_error(websocket, f"No message received within {timeout}s.")
        return None
    if raw.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(raw.get("code", 1000))
    if _frame_size(raw) > MAX_FRAME_BYTES:
        await _close_protocol_error(websocket, f"Message exceeds the {MAX_FRAME_BYTES} byte limit.")
        return None
    text = raw.get("text")
    if text is None:
        await _close_protocol_error(websocket, "Binary frames are not supported — send JSON text frames only.")
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        await _close_protocol_error(websocket, "Malformed JSON.")
        return None
    if not isinstance(payload, dict) or "type" not in payload:
        await _close_protocol_error(websocket, "Every message must be a JSON object with a 'type' field.")
        return None
    return payload


@router.websocket("/tally-agent/ws")
async def tally_agent_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else None

    first = await _receive_json(websocket, timeout=AUTH_TIMEOUT_SECONDS)
    if first is None:
        return
    if first.get("type") != "auth":
        await _close_protocol_error(websocket, "The first message must be of type 'auth'.")
        return

    credential_id = first.get("credential_id")
    credential_secret = first.get("credential_secret")
    if not credential_id or not credential_secret:
        await _close_protocol_error(websocket, "'auth' requires credential_id and credential_secret.")
        return

    async with AsyncSessionLocal() as db:
        result = await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret=credential_secret, source_ip=client_ip)
        if result is None:
            try:
                await websocket.send_text(json.dumps({"type": "error", "code": "AUTH_FAILED", "message": "Authentication rejected."}))
            except Exception:  # noqa: BLE001
                pass
            await websocket.close(code=4401, reason="Authentication rejected")
            return
        installation, credential = result
        await record_event(
            db, organization_id=installation.organization_id, actor_id=None, action="Agent Connected",
            module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
            status=AuditStatus.SUCCESS, after_value={"agent_version": first.get("agent_version"), "hostname": first.get("hostname")},
        )
        installation.hostname = first.get("hostname") or installation.hostname
        installation.agent_version = first.get("agent_version") or installation.agent_version
        installation.os_version = first.get("os_version") or installation.os_version
        await db.commit()

    now = datetime.now(timezone.utc)
    state = AgentConnectionState(
        websocket=websocket, agent_installation_id=installation.id, organization_id=installation.organization_id,
        connected_at=now, last_heartbeat_at=now,
    )
    await register(installation.id, state)
    await websocket.send_text(json.dumps({
        "type": "auth_ack", "status": "ok", "installation_id": installation.id,
        "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
    }))
    logger.info("Tally agent connected: installation_id=%s hostname=%s", installation.id, installation.hostname)

    try:
        while True:
            message = await _receive_json(websocket, timeout=RECEIVE_TIMEOUT_SECONDS)
            if message is None:
                return
            msg_type = message.get("type")

            if msg_type == "heartbeat":
                state.last_heartbeat_at = datetime.now(timezone.utc)
                await websocket.send_text(json.dumps({"type": "heartbeat_ack", "server_time": state.last_heartbeat_at.isoformat()}))
                async with AsyncSessionLocal() as db:
                    row = await db.get(AgentInstallation, installation.id)
                    if row is not None:
                        row.last_heartbeat_at = state.last_heartbeat_at
                        await db.commit()
                continue

            if msg_type == "command_result":
                request_id = message.get("request_id")
                fut = state.pending_requests.pop(request_id, None) if request_id else None
                if fut is None:
                    logger.debug("Discarding command_result for unknown/late request_id=%s (installation_id=%s)", request_id, installation.id)
                    continue
                if not fut.done():
                    fut.set_result(message)
                continue

            if msg_type == "auth":
                await _close_protocol_error(websocket, "Already authenticated — a second 'auth' message is a protocol violation.")
                return

            logger.debug("Ignoring unrecognized message type %r from installation_id=%s", msg_type, installation.id)
    except WebSocketDisconnect:
        pass
    finally:
        unregister(installation.id)
        async with AsyncSessionLocal() as db:
            row = await db.get(AgentInstallation, installation.id)
            if row is not None:
                row.last_disconnected_at = datetime.now(timezone.utc)
                await db.commit()
            await record_event(
                db, organization_id=installation.organization_id, actor_id=None, action="Agent Disconnected",
                module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
                status=AuditStatus.SUCCESS,
            )
        logger.info("Tally agent disconnected: installation_id=%s", installation.id)
