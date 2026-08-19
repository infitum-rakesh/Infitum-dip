"""
retry — exponential-backoff retry for transient extraction errors.

Permanent errors (ConnectorUnsupportedError — this connector/type isn't
wired up at all; ConnectorValidationError — the request itself is invalid,
e.g. non-SELECT SQL) are never retried: retrying "this will never work"
just delays the same inevitable failure. Everything else (a dropped
connection, a timeout, a deadlock) is treated as transient.
"""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from app.connectors.base import ConnectorUnsupportedError, ConnectorValidationError

T = TypeVar("T")

PERMANENT_ERRORS = (ConnectorUnsupportedError, ConnectorValidationError)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return await fn()
        except PERMANENT_ERRORS:
            raise
        except Exception as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            # Full jitter — spreads out concurrent partition workers'
            # retries instead of every worker hammering the source at the
            # exact same moment (attempt 1: ~5s, attempt 2: ~15s, attempt 3: ~45s).
            delay = min(max_delay, base_delay * (2.5 ** (attempt - 1)))
            delay *= 0.7 + 0.6 * random.random()
            if on_retry:
                on_retry(attempt, delay, exc)
            await asyncio.sleep(delay)
