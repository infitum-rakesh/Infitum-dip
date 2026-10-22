"""
tally_agent — the Windows Client Agent half of the Multi-Client
TallyPrime Integration Platform.

This package has no dependency on, and is never imported by, the
`backend/` FastAPI application — it is a genuinely separate deliverable
that ships to a customer's machine and talks to the server only over the
WebSocket protocol documented in protocol.py (mirroring
backend/app/api/ws_tally_agent.py's own protocol docstring). Because of
that separation, tally_client.py deliberately duplicates
backend/app/connectors/application/tallyprime.py's TDL-building and
response-parsing logic rather than importing it — see tally_client.py's
own docstring.

Real code throughout. The pieces that are genuinely Windows-only
(credential_store.py's DPAPI path, windows_service.py, the PyInstaller/
installer scripts under ../installer/) cannot be exercised on this
project's own dev machines (no Windows box, no licensed TallyPrime
install) and are explicitly disclosed as unverified-on-Windows wherever
that applies — never silently assumed to work. Everything else (config,
protocol, ws_client, tally_client against a mock Tally gateway,
dispatcher, cli) is real and has been live-verified as a plain
cross-platform Python process.
"""

__version__ = "0.1.0"
