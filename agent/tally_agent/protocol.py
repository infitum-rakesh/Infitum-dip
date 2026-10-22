"""
protocol.py — pure build/parse functions for every message this agent
exchanges with the server's WebSocket gateway. No network code here (see
ws_client.py for that) — every function in this file takes/returns plain
dicts, so it's fully unit-testable without a live socket.

Mirrors backend/app/api/ws_tally_agent.py's own protocol docstring
exactly — that file is this one's only real "spec", since the two were
built together as one protocol:

  agent -> server : auth (handshake, must be the first message)
  server -> agent : auth_ack
  agent -> server : heartbeat (every ~30s)
  server -> agent : heartbeat_ack
  server -> agent : command
  agent -> server : command_result
  server -> agent : job_cancel
  either direction: error (protocol-level only — a command-scoped
                            failure lives in command_result.error instead)
"""
from __future__ import annotations

import json
from typing import Any


class ProtocolError(Exception):
    """Raised by parse_message() for a frame that isn't valid JSON or
    isn't a JSON object with a 'type' field — the same two checks
    ws_tally_agent.py's _receive_json() applies server-side."""


def parse_message(raw_text: str) -> dict:
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"Malformed JSON frame: {exc}") from exc
    if not isinstance(payload, dict) or "type" not in payload:
        raise ProtocolError("Every message must be a JSON object with a 'type' field.")
    return payload


def _dumps(payload: dict) -> str:
    return json.dumps(payload)


def build_auth(*, credential_id: str, credential_secret: str, agent_version: str, hostname: str, os_version: str) -> str:
    return _dumps({
        "type": "auth",
        "credential_id": credential_id,
        "credential_secret": credential_secret,
        "agent_version": agent_version,
        "hostname": hostname,
        "os_version": os_version,
    })


def build_heartbeat() -> str:
    return _dumps({"type": "heartbeat"})


def build_command_result(*, request_id: str, success: bool, data: dict | None = None, error: str | None = None) -> str:
    return _dumps({"type": "command_result", "request_id": request_id, "success": success, "data": data, "error": error})


def is_auth_ack(message: dict) -> bool:
    return message.get("type") == "auth_ack"


def is_heartbeat_ack(message: dict) -> bool:
    return message.get("type") == "heartbeat_ack"


def is_command(message: dict) -> bool:
    return message.get("type") == "command"


def is_job_cancel(message: dict) -> bool:
    return message.get("type") == "job_cancel"


def is_error(message: dict) -> bool:
    return message.get("type") == "error"


def command_fields(message: dict) -> tuple[str, str, dict[str, Any]]:
    """Returns (request_id, operation, parameters) from a 'command'
    message — raises ProtocolError if either required field is missing,
    since a command this agent can't even identify can't be actioned or
    cleanly NAK'd with a command_result (there's no request_id to echo)."""
    request_id = message.get("request_id")
    operation = message.get("operation")
    if not request_id or not operation:
        raise ProtocolError("A 'command' message requires both request_id and operation.")
    return request_id, operation, message.get("parameters") or {}
