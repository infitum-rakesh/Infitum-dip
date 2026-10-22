"""
ws_client.py — the agent's side of the WebSocket connection to the
central server. Owns the connect -> auth -> heartbeat -> receive loop ->
reconnect lifecycle; protocol.py supplies the pure message
build/parse functions this module calls, and dispatcher.py supplies
the actual command execution this module hands incoming 'command'
messages to.

Reconnection: exponential backoff with full jitter (the standard
AWS-architecture-blog algorithm — `random.uniform(0, min(cap, base *
2**attempt))` — chosen over fixed/linear backoff specifically because
many agents reconnecting after one server restart hitting a fixed delay
in lockstep would otherwise hammer the server's auth endpoint
simultaneously; full jitter spreads that out). Capped at
MAX_BACKOFF_SECONDS so a long outage still retries at a bounded interval
rather than backing off indefinitely.

Heartbeats are sent on a fixed interval (config.heartbeat_interval_seconds,
default 30s, matching HEARTBEAT_INTERVAL_SECONDS in backend/app/api/
ws_tally_agent.py) via a dedicated asyncio task so a slow in-flight
command dispatch never delays a heartbeat the server is using to judge
liveness.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import random

import websockets
from websockets.exceptions import ConnectionClosed

from . import protocol
from .config import AgentConfig
from .credential_store import AgentCredentials

logger = logging.getLogger(__name__)

AUTH_TIMEOUT_SECONDS = 10
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
BACKOFF_MULTIPLIER = 2.0

AGENT_VERSION = "0.1.0"


class AuthRejectedError(Exception):
    """Raised when the server closes the connection with 4401 (bad
    credential) — distinct from a transient network failure because
    retrying with the SAME credential will only ever fail the same way;
    the caller (cli.py's `run` command) surfaces this loudly rather than
    retrying forever against a credential that's been rotated/revoked."""


def _backoff_delay(attempt: int) -> float:
    cap = min(MAX_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** attempt))
    return random.uniform(0, cap)


class AgentWsClient:
    def __init__(self, config: AgentConfig, credentials: AgentCredentials, dispatcher):
        self._config = config
        self._credentials = credentials
        self._dispatcher = dispatcher  # dispatcher.Dispatcher — see dispatcher.py
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        """The agent's main loop — connects, serves commands, and on any
        disconnect (clean or unclean) waits a backoff interval and
        reconnects, forever, until stop() is called. Never raises for a
        connection-level failure; only AuthRejectedError propagates,
        since no amount of retrying fixes a rejected credential."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
                attempt = 0  # a clean run (we served at least the handshake) resets backoff
            except AuthRejectedError:
                raise
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Connection to server lost (%s) — will reconnect.", exc)
            except Exception as exc:  # noqa: BLE001 - never let an unexpected bug kill the reconnect loop
                logger.exception("Unexpected error in connection loop: %s", exc)

            if self._stop_event.is_set():
                break
            delay = _backoff_delay(attempt)
            attempt += 1
            logger.info("Reconnecting in %.1fs (attempt %d)...", delay, attempt)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass  # normal case — the backoff delay elapsed, loop again

    async def _connect_and_serve(self) -> None:
        url = self._config.ws_url()
        logger.info("Connecting to %s ...", url)
        async with websockets.connect(url, open_timeout=AUTH_TIMEOUT_SECONDS) as ws:
            await ws.send(protocol.build_auth(
                credential_id=self._credentials.credential_id,
                credential_secret=self._credentials.credential_secret,
                agent_version=AGENT_VERSION,
                hostname=platform.node(),
                os_version=f"{platform.system()} {platform.release()}",
            ))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise ConnectionClosed(None, None) from exc
            ack = protocol.parse_message(raw)
            if protocol.is_error(ack):
                logger.error("Server rejected authentication: %s", ack.get("message"))
                raise AuthRejectedError(ack.get("message") or "Authentication rejected.")
            if not protocol.is_auth_ack(ack):
                raise ConnectionClosed(None, None)
            logger.info("Authenticated — installation_id=%s", ack.get("installation_id"))

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                await self._receive_loop(ws)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat_loop(self, ws) -> None:
        interval = self._config.heartbeat_interval_seconds
        while True:
            await asyncio.sleep(interval)
            await ws.send(protocol.build_heartbeat())

    async def _receive_loop(self, ws) -> None:
        while True:
            raw = await ws.recv()
            try:
                message = protocol.parse_message(raw)
            except protocol.ProtocolError as exc:
                logger.warning("Ignoring malformed frame from server: %s", exc)
                continue

            if protocol.is_heartbeat_ack(message):
                continue
            if protocol.is_command(message):
                asyncio.create_task(self._handle_command(ws, message))
                continue
            if protocol.is_job_cancel(message):
                request_id = message.get("request_id")
                if request_id:
                    self._dispatcher.cancel(request_id)
                continue
            if protocol.is_error(message):
                logger.warning("Server protocol error: %s", message.get("message"))
                continue
            logger.debug("Ignoring unrecognized message type %r", message.get("type"))

    async def _handle_command(self, ws, message: dict) -> None:
        try:
            request_id, operation, parameters = protocol.command_fields(message)
        except protocol.ProtocolError as exc:
            logger.warning("Ignoring malformed command: %s", exc)
            return
        try:
            data = await self._dispatcher.dispatch(request_id, operation, parameters)
            result = protocol.build_command_result(request_id=request_id, success=True, data=data)
        except Exception as exc:  # noqa: BLE001 - every dispatch failure must produce a command_result, never an unhandled task exception
            logger.warning("Command %s (%s) failed: %s", operation, request_id, exc)
            result = protocol.build_command_result(request_id=request_id, success=False, error=str(exc))
        try:
            await ws.send(result)
        except ConnectionClosed:
            pass  # the connection already dropped; _receive_loop's own caller will reconnect
