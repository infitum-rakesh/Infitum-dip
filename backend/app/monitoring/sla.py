"""
SLA evaluation — pure function, no DB access. Judges one finished
PipelineJob against its SlaDefinition, returning a live verdict rather
than a stored history row (see models/monitoring.py's module docstring
for why: the verdict can never drift out of sync with the job record
it's judging because it's recomputed from it every time, not cached).

Known, disclosed limitation: expected_start_time/expected_completion_time
are compared against the job's started_at/completed_at in UTC, not
converted through the organization's configured timezone (Settings ->
General -> Timezone) — a real gap, not silently pretended away, exactly
like this codebase discloses ScheduleRun's simulated execution.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.monitoring import SlaDefinition, SlaStatus
from app.models.pipeline_job import JobStatus, PipelineJob


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


def evaluate_sla(job: PipelineJob, sla: SlaDefinition, recent_statuses: list[str]) -> tuple[SlaStatus, str]:
    """`recent_statuses` — this job's own status first, then progressively
    older sibling jobs for the same pipeline (repository.recent_jobs_for_pipeline
    order) — used to count consecutive failures."""
    if not sla.enabled:
        return SlaStatus.NOT_EVALUATED, "SLA definition is disabled."

    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        return SlaStatus.NOT_EVALUATED, "Run still in progress."

    if job.status == JobStatus.CANCELLED:
        return SlaStatus.NOT_EVALUATED, "Run was cancelled — not judged against SLA."

    if job.status == JobStatus.FAILED:
        consecutive = 0
        for s in recent_statuses:
            if s == JobStatus.FAILED.value:
                consecutive += 1
            else:
                break
        if consecutive >= sla.max_failure_count:
            return SlaStatus.BREACHED, f"{consecutive} consecutive failure(s) — exceeds the allowed {sla.max_failure_count}."
        return SlaStatus.WARNING, f"Run failed ({consecutive}/{sla.max_failure_count} consecutive failures allowed before breach)."

    # SUCCEEDED
    reasons: list[str] = []
    verdict = SlaStatus.MET

    if job.started_at and job.completed_at and sla.max_duration_minutes:
        duration_min = (job.completed_at - job.started_at).total_seconds() / 60.0
        if duration_min > sla.max_duration_minutes:
            verdict = SlaStatus.BREACHED
            reasons.append(f"Duration {duration_min:.1f}m exceeded max {sla.max_duration_minutes}m.")
        elif duration_min > sla.max_duration_minutes * (sla.warning_threshold_pct / 100.0):
            verdict = max(verdict, SlaStatus.WARNING, key=lambda s: {"MET": 0, "WARNING": 1, "BREACHED": 2}[s.value])
            reasons.append(f"Duration {duration_min:.1f}m is within {sla.warning_threshold_pct}% of the {sla.max_duration_minutes}m max.")

    if job.completed_at and sla.expected_completion_time:
        parsed = _parse_hhmm(sla.expected_completion_time)
        if parsed:
            h, m = parsed
            completed = job.completed_at if job.completed_at.tzinfo else job.completed_at.replace(tzinfo=timezone.utc)
            expected = completed.replace(hour=h, minute=m, second=0, microsecond=0)
            if completed > expected:
                verdict = SlaStatus.BREACHED
                reasons.append(f"Completed at {completed.strftime('%H:%M UTC')}, after the expected {sla.expected_completion_time} UTC.")

    if job.started_at and sla.expected_start_time:
        parsed = _parse_hhmm(sla.expected_start_time)
        if parsed:
            h, m = parsed
            started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=timezone.utc)
            expected = started.replace(hour=h, minute=m, second=0, microsecond=0)
            if started > expected:
                verdict = SlaStatus.BREACHED
                reasons.append(f"Started at {started.strftime('%H:%M UTC')}, after the expected {sla.expected_start_time} UTC.")

    if not reasons:
        reasons.append("Completed within all configured SLA thresholds.")
    return verdict, " ".join(reasons)
