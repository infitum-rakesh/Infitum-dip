"""
Bulk Object Migration — connection-level concurrency bounding (BM4).

Genuinely new code, not a rename of something that already existed:
connectors/database/pool_manager.py bounds the number of PHYSICAL DB
connections held per Connection.id via SQLAlchemy's QueuePool
(pool_size=5, max_overflow=2) — a resource ceiling at the driver level.
Nothing else in this codebase bounds "how many concurrent migration TASKS
may use connection X at once" at the orchestration level; that's what this
module adds.

A lazy per-connection asyncio.Semaphore, shared process-wide (module-level
dict) — this backend runs every background job as an asyncio task in one
process (no multi-worker/multi-process deployment exists anywhere else in
this codebase either; see models/bulk_migration_run.py's docstring on the
portable atomic task-claim mechanism for the identical disclosure). If
this backend is ever horizontally scaled across processes, this in-memory
bound would need to move to a DB-backed counter — disclosed, not silently
assumed away.

effective_workers for a run = min(pipeline.max_workers,
GLOBAL_SYSTEM_WORKER_LIMIT) — resolved once at plan time by
bulk_migration_planner_service.py. Source/target connection-level bounding
happens HERE, additionally, at claim/dispatch time: a claimed task must
also acquire its source AND target connection's semaphore before it
actually starts extracting/loading, so effective per-connection
concurrency is min(run.effective_workers, source_connection_limit,
target_connection_limit) even when many objects in the run share one
connection.
"""
from __future__ import annotations

import asyncio

DEFAULT_PER_CONNECTION_LIMIT = 5  # matches pool_manager.py's own pool_size default — no benefit queuing more concurrent tasks on one connection than the pool can actually serve without overflow

_connection_semaphores: dict[int, asyncio.Semaphore] = {}
_connection_limits: dict[int, int] = {}


def get_connection_semaphore(connection_id: int, limit: int | None = None) -> asyncio.Semaphore:
    effective_limit = limit or DEFAULT_PER_CONNECTION_LIMIT
    existing = _connection_semaphores.get(connection_id)
    if existing is not None and _connection_limits.get(connection_id) == effective_limit:
        return existing
    sem = asyncio.Semaphore(effective_limit)
    _connection_semaphores[connection_id] = sem
    _connection_limits[connection_id] = effective_limit
    return sem


def reset_for_tests() -> None:
    """Test-only — clears cached semaphores so a fresh event loop (a new
    asyncio.run() in a different test) doesn't reuse a Semaphore bound to
    a closed loop."""
    _connection_semaphores.clear()
    _connection_limits.clear()
