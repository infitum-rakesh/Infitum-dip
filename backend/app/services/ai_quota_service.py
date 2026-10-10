"""
ai_quota_service — real usage accounting + atomic quota enforcement for AI
connections (AIC-P7), the last of the master spec's AI Connection
Subscription bullets. Builds on three things already in place:

  - CustomerModule.max_tokens/token_period/ai_rate_limit/
    ai_concurrency_limit (AIC-P4) — the entitlement LIMITS.
  - AITokenUsageEvent (AIC-P2) — the append-only usage ledger.
  - AIQuotaReservation (AIC-P2) — the atomic reserve-before-call,
    commit/release-after primitive that closes the race condition a
    plain read-then-write against CustomerModule would have under
    concurrency (two simultaneous requests both reading "quota not yet
    exhausted" and both proceeding, together exceeding it).

Scope, matching the ownership split AIC-P3's access service already
established:

  - Seller-managed connections (Connection.organization_id IS NULL) are
    the ONLY ones with a real quota to enforce — that's what the org's
    AI_CONNECTION CustomerModule entitlement row actually IS (a Seller-
    sold allowance). Hard gates (token budget / rate / concurrency) only
    apply here.
  - Customer-owned connections (the org's own API key) have no platform
    quota — the org pays their provider directly. Usage is still
    recorded for observability (the spec's own "per-user own-usage"
    screen), just never gated.

Real, provider-reported usage only (connectors/ai/*.py's own
docstrings) — this module never estimates or fabricates a token count
for the recorded ledger; AIUsageSource.UNAVAILABLE is the honest label
on the rare response that genuinely carries none. The PRE-call
reservation is the one place an estimate is unavoidable (the real
number doesn't exist until after the call) — reserved_tokens is clearly
that: an upper-bound guess used only to hold a provisional slot against
the hard quota until the real figure commits and corrects it.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_connection import AIQuotaReservation, AIQuotaReservationStatus, AITokenUsageEvent, AIUsageSource
from app.models.connection import Connection
from app.models.customer_module import CustomerModule
from app.services.billing.access_service import _entitlement_active, access_service


class AiQuotaExceededError(Exception):
    """Raised by check_and_reserve() — a hard quota (token budget, rate,
    or concurrency) would be exceeded. The caller must not proceed with
    the real AI call."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _find_entitlement(db: AsyncSession, organization_id: int, connection_type) -> CustomerModule | None:
    """Same entitlement-resolution logic as
    ai_connection_access_service._has_active_ai_entitlement(), returning
    the actual row (this module needs its limit columns, not just a
    bool)."""
    active_subs = await access_service.get_active_subscriptions(db, organization_id)
    if not active_subs:
        return None
    active_sub_ids = {s.id for s in active_subs}
    result = await db.execute(
        select(CustomerModule).where(
            CustomerModule.organization_id == organization_id,
            CustomerModule.module == "AI_CONNECTION",
            CustomerModule.subscription_id.in_(active_sub_ids),
        )
    )
    for cm in result.scalars().all():
        if not _entitlement_active(cm):
            continue
        bundle = cm.ai_provider_bundle
        if not bundle or connection_type.value in bundle:
            return cm
    return None


def _period_window_start(cm: CustomerModule) -> datetime | None:
    """None = unbounded (TOTAL, or an unrecognized period treated as
    TOTAL rather than silently gating on nothing)."""
    period = (cm.token_period or "").upper()
    now = _now()
    if period == "DAILY":
        return now - timedelta(hours=24)
    if period == "MONTHLY":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "BILLING_CYCLE":
        return None  # resolved by the caller from the Subscription row instead — see check_and_reserve()
    return None  # TOTAL, or unset


def _estimate_tokens(prompt: str, max_tokens: int) -> int:
    """Pre-call upper-bound guess for the reservation gate ONLY — never
    the figure recorded to the usage ledger (see module docstring). ~4
    chars/token is the standard rough heuristic for English prose;
    deliberately generous (ceiling, not average) since under-reserving
    is what actually lets concurrent requests blow through a hard quota."""
    input_estimate = max(1, len(prompt) // 3)
    return input_estimate + max_tokens


async def get_usage_summary(db: AsyncSession, *, organization_id: int, connection: Connection) -> dict | None:
    """Real, read-only usage snapshot for the "Manage AI Access" screen —
    None for a customer-owned connection (nothing metered — see module
    docstring) or when this org genuinely has no AI_CONNECTION
    entitlement covering it (e.g. it can see the connection via a grant
    but the seller never actually sold this org a subscription — shown
    as "not currently entitled" by the caller rather than fabricated
    zeros). Every number here is a live COUNT against the real ledger/
    reservation tables, not a cached or estimated figure."""
    if connection.organization_id is not None:
        return None
    cm = await _find_entitlement(db, organization_id, connection.connection_type)
    if cm is None:
        return None

    window_start = _period_window_start(cm)
    if (cm.token_period or "").upper() == "BILLING_CYCLE":
        active_subs = await access_service.get_active_subscriptions(db, organization_id)
        sub = next((s for s in active_subs if s.id == cm.subscription_id), None)
        window_start = _aware(sub.current_period_start) if sub else None

    usage_stmt = select(func.coalesce(func.sum(AITokenUsageEvent.total_tokens), 0)).where(
        AITokenUsageEvent.customer_module_id == cm.id,
    )
    reserved_stmt = select(func.coalesce(func.sum(AIQuotaReservation.reserved_tokens), 0)).where(
        AIQuotaReservation.customer_module_id == cm.id, AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
    )
    if window_start is not None:
        usage_stmt = usage_stmt.where(AITokenUsageEvent.created_at >= window_start)
        reserved_stmt = reserved_stmt.where(AIQuotaReservation.created_at >= window_start)
    tokens_used = (await db.execute(usage_stmt)).scalar_one()
    tokens_reserved = (await db.execute(reserved_stmt)).scalar_one()

    in_flight = (await db.execute(
        select(func.count(AIQuotaReservation.id)).where(
            AIQuotaReservation.customer_module_id == cm.id, AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
        )
    )).scalar_one()
    one_minute_ago = _now() - timedelta(minutes=1)
    requests_last_minute = (await db.execute(
        select(func.count(AITokenUsageEvent.id)).where(
            AITokenUsageEvent.customer_module_id == cm.id, AITokenUsageEvent.created_at >= one_minute_ago,
        )
    )).scalar_one()

    return {
        "max_tokens": cm.max_tokens, "token_period": cm.token_period,
        "tokens_used": tokens_used, "tokens_reserved": tokens_reserved,
        "ai_rate_limit": cm.ai_rate_limit, "requests_last_minute": requests_last_minute,
        "ai_concurrency_limit": cm.ai_concurrency_limit, "concurrent_in_flight": in_flight,
    }


async def check_and_reserve(
    db: AsyncSession, *, organization_id: int | None, connection: Connection, user_id: int | None,
    prompt: str, max_tokens: int,
) -> AIQuotaReservation | None:
    """Call BEFORE the real AI generation call. Returns the reservation
    to pass to commit_usage()/release() afterward, or None when this
    connection has no quota to enforce (customer-owned — see module
    docstring) — callers must handle None as "proceed, nothing to
    commit/release"."""
    if organization_id is None or connection.organization_id is not None:
        return None  # platform caller, or a customer-owned connection — no entitlement quota applies

    cm = await _find_entitlement(db, organization_id, connection.connection_type)
    if cm is None:
        # Shouldn't happen — ai_connection_access_service.check_access()
        # already confirmed entitlement before this call site was ever
        # reached. Fail closed rather than silently skip enforcement.
        raise AiQuotaExceededError("Your organization's AI Connection subscription could not be re-verified for this request.")

    estimate = _estimate_tokens(prompt, max_tokens)
    one_minute_ago = _now() - timedelta(minutes=1)
    window_start = _period_window_start(cm)
    if (cm.token_period or "").upper() == "BILLING_CYCLE":
        active_subs = await access_service.get_active_subscriptions(db, organization_id)
        sub = next((s for s in active_subs if s.id == cm.subscription_id), None)
        window_start = _aware(sub.current_period_start) if sub else None

    # Real bug found live (AIC-P9): the original version of this function
    # did a plain Python-level SELECT-then-INSERT for each of the three
    # limits — exactly the race the module docstring above says
    # AIQuotaReservation exists to prevent, except this function never
    # actually closed it. Confirmed live: 6 genuinely concurrent calls
    # against ai_concurrency_limit=2 ALL got a reservation, because every
    # one of them read "0 in flight" before any of the others had
    # committed their INSERT. The fix below folds the check AND the
    # insert into ONE atomic `INSERT ... SELECT ... WHERE <limits still
    # hold>` statement — the database itself (not Python) evaluates the
    # WHERE against its own live state at the instant it attempts the
    # write, so two concurrent statements can no longer both see
    # "quota available" and both succeed. rowcount==0 means the WHERE
    # was false — some limit was (by then) already exhausted.
    conditions = []
    if cm.ai_concurrency_limit is not None:
        in_flight_subq = (
            select(func.count(AIQuotaReservation.id))
            .where(
                AIQuotaReservation.customer_module_id == cm.id,
                AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
            )
            .scalar_subquery()
        )
        conditions.append(in_flight_subq < cm.ai_concurrency_limit)
    if cm.ai_rate_limit is not None:
        rate_subq = (
            select(func.count(AITokenUsageEvent.id))
            .where(AITokenUsageEvent.customer_module_id == cm.id, AITokenUsageEvent.created_at >= one_minute_ago)
            .scalar_subquery()
        )
        conditions.append(rate_subq < cm.ai_rate_limit)
    if cm.max_tokens is not None:
        usage_subq_stmt = select(func.coalesce(func.sum(AITokenUsageEvent.total_tokens), 0)).where(
            AITokenUsageEvent.customer_module_id == cm.id,
        )
        reserved_subq_stmt = select(func.coalesce(func.sum(AIQuotaReservation.reserved_tokens), 0)).where(
            AIQuotaReservation.customer_module_id == cm.id, AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
        )
        if window_start is not None:
            usage_subq_stmt = usage_subq_stmt.where(AITokenUsageEvent.created_at >= window_start)
            reserved_subq_stmt = reserved_subq_stmt.where(AIQuotaReservation.created_at >= window_start)
        conditions.append(
            (usage_subq_stmt.scalar_subquery() + reserved_subq_stmt.scalar_subquery() + estimate) <= cm.max_tokens
        )

    select_values = select(
        literal(organization_id), literal(connection.id), literal(user_id), literal(cm.id),
        literal(estimate), literal(AIQuotaReservationStatus.RESERVED, type_=AIQuotaReservation.__table__.c.status.type),
    )
    if conditions:
        select_values = select_values.where(and_(*conditions))

    insert_stmt = insert(AIQuotaReservation.__table__).from_select(
        ["organization_id", "connection_id", "user_id", "customer_module_id", "reserved_tokens", "status"],
        select_values,
    ).returning(AIQuotaReservation.__table__.c.id)
    inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    await db.commit()

    if inserted_id is not None:
        return await db.get(AIQuotaReservation, inserted_id)

    # Denied — the atomic insert above already made the real enforcement
    # decision; these follow-up reads are ONLY to build a precise, honest
    # error message and may be mildly stale by the time they run (e.g.
    # another request released its slot in between) — that staleness
    # cannot cause an over-admission, only a possibly-conservative message.
    if cm.ai_concurrency_limit is not None:
        in_flight = (await db.execute(
            select(func.count(AIQuotaReservation.id)).where(
                AIQuotaReservation.customer_module_id == cm.id, AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
            )
        )).scalar_one()
        if in_flight >= cm.ai_concurrency_limit:
            raise AiQuotaExceededError(
                f"This AI connection has reached its concurrent-request limit ({cm.ai_concurrency_limit}). Try again shortly."
            )
    if cm.ai_rate_limit is not None:
        recent_requests = (await db.execute(
            select(func.count(AITokenUsageEvent.id)).where(
                AITokenUsageEvent.customer_module_id == cm.id, AITokenUsageEvent.created_at >= one_minute_ago,
            )
        )).scalar_one()
        if recent_requests >= cm.ai_rate_limit:
            raise AiQuotaExceededError(
                f"This AI connection has reached its rate limit ({cm.ai_rate_limit} requests/minute). Try again shortly."
            )
    if cm.max_tokens is not None:
        usage_stmt = select(func.coalesce(func.sum(AITokenUsageEvent.total_tokens), 0)).where(
            AITokenUsageEvent.customer_module_id == cm.id,
        )
        reserved_stmt = select(func.coalesce(func.sum(AIQuotaReservation.reserved_tokens), 0)).where(
            AIQuotaReservation.customer_module_id == cm.id, AIQuotaReservation.status == AIQuotaReservationStatus.RESERVED,
        )
        if window_start is not None:
            usage_stmt = usage_stmt.where(AITokenUsageEvent.created_at >= window_start)
            reserved_stmt = reserved_stmt.where(AIQuotaReservation.created_at >= window_start)
        used = (await db.execute(usage_stmt)).scalar_one()
        reserved = (await db.execute(reserved_stmt)).scalar_one()
        period_label = (cm.token_period or "TOTAL").replace("_", " ").lower()
        raise AiQuotaExceededError(
            f"This would exceed your organization's AI token budget for this {period_label} period "
            f"({used + reserved}/{cm.max_tokens} tokens already used or reserved)."
        )
    # None of the limits individually explain it on re-check (a narrow
    # window where a concurrent release/commit resolved it just after our
    # atomic attempt) — fail closed with a generic message rather than
    # silently proceeding without a reservation.
    raise AiQuotaExceededError("This AI connection is temporarily at capacity. Try again shortly.")


async def commit_usage(
    db: AsyncSession, *, reservation: AIQuotaReservation | None, organization_id: int, connection_id: int,
    user_id: int | None, feature: str, request_id: str, usage: dict | None, status: str = "SUCCESS",
    error_message: str | None = None,
) -> AITokenUsageEvent:
    """Call AFTER a real AI call completes (success or failure) —
    records the real ledger entry (idempotent on (connection_id,
    request_id), see AITokenUsageEvent's own docstring on reusing one
    request_id across an internal corrective retry) and, if a
    reservation was held, marks it COMMITTED. usage is whatever the
    connector's own last_usage attribute held right after the call —
    None becomes AIUsageSource.UNAVAILABLE, never a fabricated figure."""
    existing = await db.execute(
        select(AITokenUsageEvent).where(
            AITokenUsageEvent.connection_id == connection_id, AITokenUsageEvent.request_id == request_id,
        )
    )
    event = existing.scalar_one_or_none()
    if event is None:
        total = usage.get("total_tokens") if usage else None
        event = AITokenUsageEvent(
            organization_id=organization_id, user_id=user_id, connection_id=connection_id,
            customer_module_id=reservation.customer_module_id if reservation else None,
            feature=feature, request_id=request_id,
            input_tokens=usage.get("input_tokens") if usage else None,
            output_tokens=usage.get("output_tokens") if usage else None,
            total_tokens=total,
            usage_source=AIUsageSource.PROVIDER_REPORTED if usage and total is not None else AIUsageSource.UNAVAILABLE,
            status=status, error_message=error_message,
        )
        db.add(event)

    if reservation is not None:
        reservation.status = AIQuotaReservationStatus.COMMITTED
        reservation.resolved_at = _now()

    await db.commit()
    await db.refresh(event)
    return event


async def release(db: AsyncSession, reservation: AIQuotaReservation | None) -> None:
    """Call instead of commit_usage() when the real call never actually
    happened (e.g. the connector raised before any HTTP request went
    out) — releases the held slot without recording any usage, since
    nothing was genuinely consumed. A no-op when reservation is None
    (customer-owned connection — see check_and_reserve())."""
    if reservation is None:
        return
    reservation.status = AIQuotaReservationStatus.RELEASED
    reservation.resolved_at = _now()
    await db.commit()
