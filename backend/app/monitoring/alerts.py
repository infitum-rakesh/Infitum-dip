"""
Alert evaluation — called from service.py's on_job_finished() (and,
symmetrically, from validation/reconciliation completion points once
those are wired — see PP1) with a small, typed context dict. Matches
enabled AlertRule rows whose condition+scope+threshold are satisfied,
creates one AlertEvent per match, dispatches it through every configured
channel (notification_channels.dispatch), and — when the triggering
condition is SLA_BREACHED and the rule's severity is CRITICAL/HIGH, or
the condition is JOB_FAILED — additionally offers to file an Incident
(see service.py for the actual create-vs-not decision, which belongs to
SlaDefinition.auto_create_incident / a rule's own settings, not to this
module).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monitoring import AlertCondition, AlertEvent, AlertRule
from app.monitoring import notification_channels


async def _last_fired_at(db: AsyncSession, rule_id: int) -> datetime | None:
    stmt = (
        select(AlertEvent.triggered_at)
        .where(AlertEvent.rule_id == rule_id)
        .order_by(AlertEvent.triggered_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _condition_matches(rule: AlertRule, ctx: dict) -> bool:
    cond = rule.condition
    if cond == AlertCondition.JOB_FAILED:
        return ctx.get("event") == "JOB_FAILED"
    if cond == AlertCondition.SLA_BREACHED:
        return ctx.get("sla_status") == "BREACHED"
    if cond == AlertCondition.SLA_WARNING:
        return ctx.get("sla_status") == "WARNING"
    if cond == AlertCondition.REJECT_RATE_ABOVE:
        rate = ctx.get("reject_rate_pct")
        return rate is not None and rule.threshold is not None and rate > rule.threshold
    if cond == AlertCondition.DURATION_ABOVE_MINUTES:
        dur = ctx.get("duration_minutes")
        return dur is not None and rule.threshold is not None and dur > rule.threshold
    if cond == AlertCondition.VALIDATION_FAILED:
        return ctx.get("event") == "VALIDATION_FAILED"
    if cond == AlertCondition.RECONCILIATION_MISMATCH:
        return ctx.get("event") == "RECONCILIATION_MISMATCH"
    if cond == AlertCondition.ANOMALY_DETECTED:
        return ctx.get("event") == "ANOMALY_DETECTED"
    if cond == AlertCondition.INVOICE_ERP_POSTING_FAILED:
        return ctx.get("event") == "INVOICE_ERP_POSTING_FAILED"
    if cond == AlertCondition.CONSECUTIVE_FAILURES:
        n = ctx.get("consecutive_failures")
        return n is not None and rule.threshold is not None and n >= rule.threshold
    return False


async def evaluate_rules(db: AsyncSession, *, organization_id: int | None, ctx: dict) -> list[AlertEvent]:
    """ctx keys used across conditions: event, pipeline_id, pipeline_name,
    job_id, sla_status, reject_rate_pct, duration_minutes, consecutive_failures,
    message. Returns the AlertEvent rows created (already committed)."""
    stmt = select(AlertRule).where(AlertRule.enabled.is_(True))
    if organization_id is not None:
        stmt = stmt.where(AlertRule.organization_id == organization_id)
    rules = list((await db.execute(stmt)).scalars().all())

    fired: list[AlertEvent] = []
    for rule in rules:
        if rule.pipeline_id is not None and rule.pipeline_id != ctx.get("pipeline_id"):
            continue
        if not _condition_matches(rule, ctx):
            continue
        if rule.cooldown_minutes:
            last = await _last_fired_at(db, rule.id)
            if last is not None:
                last_utc = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_utc < timedelta(minutes=rule.cooldown_minutes):
                    continue

        message = ctx.get("message") or f"{rule.condition.value} condition met for {ctx.get('pipeline_name') or 'this pipeline'}."
        event = AlertEvent(
            organization_id=organization_id, rule_id=rule.id, rule_name=rule.name,
            condition=rule.condition.value, severity=rule.severity.value, message=message,
            pipeline_id=ctx.get("pipeline_id"), pipeline_name=ctx.get("pipeline_name"), job_id=ctx.get("job_id"),
        )
        db.add(event)
        await db.flush()  # need event.id before dispatch(), and before Incident FK below

        payload = {
            "rule_name": rule.name, "condition": rule.condition.value, "severity": rule.severity.value,
            "message": message, "pipeline_id": ctx.get("pipeline_id"), "pipeline_name": ctx.get("pipeline_name"),
            "job_id": ctx.get("job_id"), "triggered_at": event.triggered_at.isoformat() if event.triggered_at else None,
        }
        event.delivery_results = await notification_channels.dispatch(rule.channels, payload)
        fired.append(event)

    if fired:
        await db.commit()
        for event in fired:
            await db.refresh(event)
    return fired
