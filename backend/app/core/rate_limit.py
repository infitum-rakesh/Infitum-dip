"""
rate_limit.py — a small in-process sliding-window rate limiter.

No existing rate-limiting infrastructure exists anywhere in this
codebase (confirmed by search) — this is a minimal, dependency-free
primitive for protecting unauthenticated/pre-auth endpoints (first used
by api/tally_agents.py's POST /tally-agents/enroll). Deliberately
in-process only: this app runs as a single process with no --workers/
replicas (see Dockerfile/railway.json), so a module-level dict is a
complete, correct limiter for this deployment — the disclosed tradeoff
is that it resets on restart and doesn't span multiple processes, which
doesn't apply here but would need revisiting if that ever changes.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

_buckets: dict[str, deque[float]] = defaultdict(deque)


def check_rate_limit(key: str, *, max_attempts: int, window_seconds: float) -> bool:
    """Returns True if `key` is still within its allowance (and records
    this attempt), False if it has exceeded max_attempts within the
    trailing window_seconds. Call once per attempt, including failed ones
    — the caller decides whether a rejected attempt still counts (it
    should, for anything brute-force-relevant)."""
    now = time.monotonic()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= max_attempts:
        return False
    bucket.append(now)
    return True
