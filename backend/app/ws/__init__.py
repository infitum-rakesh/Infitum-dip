"""
app/ws/ — WebSocket primitives for the Multi-Client TallyPrime
Integration Platform (Agent Relay). The first WebSocket gateway in this
codebase — see tally_agent_registry.py's own docstring for why an
in-memory connection registry is correct and sufficient here (this app
runs as a single process, no --workers/replicas).
"""
