"""
Pool manager — one SQLAlchemy Engine (with its own QueuePool) cached per
distinct target, so repeated Test Connection calls and, in later phases,
repeated dataset reads against the same source reuse pooled DBAPI
connections instead of paying a fresh TCP + auth handshake every time.

Cache keys are the Connection's own database id (see
SqlAlchemyDatabaseConnector._pool_cache_key) — never a password or token,
so nothing secret ends up living in this module's dict keys or in any log
line that includes them. Keying by id rather than by host/port/db/user is
deliberate: two different Connection rows can point at the same physical
target with different credentials (e.g. a "bad password" test fixture next
to a working one), and reusing a pool across them would silently test the
wrong credential. See evict() for the credential-rotation/delete case this
alone doesn't cover.

Engines idle longer than IDLE_TTL_SECONDS are disposed and evicted so an
edited or deleted Connection doesn't leak a stale pool forever.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

IDLE_TTL_SECONDS = 15 * 60

_lock = threading.Lock()
_engines: dict[str, tuple[Engine, float]] = {}


def get_pooled_engine(*, cache_key: str, url, connect_args: dict, pool_size: int = 5, pool_timeout: int = 30) -> Engine:
    _evict_idle()
    with _lock:
        cached = _engines.get(cache_key)
        if cached is not None:
            engine, _ = cached
            _engines[cache_key] = (engine, time.monotonic())
            return engine

        engine = create_engine(
            url,
            connect_args=connect_args,
            pool_size=pool_size,
            max_overflow=2,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,   # a dead pooled connection is discarded and replaced, never handed out silently
            pool_recycle=1800,
        )
        _engines[cache_key] = (engine, time.monotonic())
        return engine


def evict(cache_key: str) -> None:
    """Force-dispose a single cached engine — called whenever a
    Connection's secrets are rotated or the Connection is deleted, so a
    stale pool never keeps authenticating with a credential that's no
    longer the one stored (or no longer exists at all)."""
    with _lock:
        cached = _engines.pop(cache_key, None)
    if cached is not None:
        cached[0].dispose()


def _evict_idle() -> None:
    now = time.monotonic()
    with _lock:
        stale = [k for k, (_, last_used) in _engines.items() if now - last_used > IDLE_TTL_SECONDS]
        for k in stale:
            engine, _ = _engines.pop(k)
            engine.dispose()


def dispose_all() -> None:
    """App-shutdown / test-teardown hook."""
    with _lock:
        for engine, _ in _engines.values():
            engine.dispose()
        _engines.clear()
