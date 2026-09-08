"""
Automated tests — Bulk Object Migration (BM8). Real (isolated) SQLite DB via
the actual service-layer functions — no mocking of the code under test,
following the exact convention conftest.py/test_document_scheduling.py
established for this project's other suites.

Scope, matching the approved plan's own BM8 line item: core mechanics —
dependency ordering + cycle detection, atomic claim/no-double-processing,
retry classification, checkpoint/resume (crash recovery), pause/cancel,
tenant isolation, RBAC (module entitlement). The full REST API layer was
already live-verified end to end via curl during BM7 (pipeline/object CRUD,
validate, dry-run, run, task/event listing, pause/resume/cancel/retry-failed
all exercised against the real HTTP stack with a real JWT-authenticated
user) — what this suite adds is durable, always-on regression coverage for
the orchestration logic underneath that API, including the exact counter-
drift and cancel/pause-race bugs found live during BM7 and fixed in this
same session.

Real vs. faked, and why — mirrors test_document_scheduling.py's own
disclosed boundary: connectors/loading/writers (the actual row-by-row I/O
to a real source/target database) are separately, already live-verified
elsewhere (BM7's curl session moved real rows through a real Postgres
table). What this suite needs to exercise is the code ABOVE that boundary —
claim/retry/pause/cancel/dependency-gating/counter-sync — so
`bulk_migration_task_executor.execute_task()` is patched with a small,
per-test-configurable fake (`fake_pool` fixture below), exactly the same
principle as that suite's `_FakeDbConnector`/`_FakeApiConnector` test
doubles standing in for a real connector one layer below the code under
test. Everything else (claim_task's atomic CAS, the worker pool's
generation loop, dependency-level promotion, retry backoff/classification,
crash recovery, run-counter synchronization, pause/cancel semantics) is the
real, unmodified bulk_migration_service.py / bulk_migration_planner_service.
py / bulk_migration_config_service.py code.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database.connection import AsyncSessionLocal
from app.models.bulk_migration import BulkMigrationLoadType, BulkMigrationObject, BulkMigrationPipeline
from app.models.bulk_migration_run import (
    BulkMigrationError, BulkMigrationErrorCode, BulkMigrationRun, BulkMigrationRunStatus,
    BulkMigrationTask, BulkMigrationTaskStatus,
)
from app.models.connection import Connection, ConnectionCategory, ConnectionEnvironment, ConnectionStatus, ConnectionType
from app.models.customer_module import CustomerModule
from app.models.subscription import SubscriptionStatus
from app.services import bulk_migration_config_service as config_svc
from app.services import bulk_migration_planner_service as planner
from app.services import bulk_migration_service as bm_svc
from app.services import bulk_migration_task_executor as task_executor
from app.services.billing.access_service import access_service


def _slug() -> str:
    return uuid.uuid4().hex[:10]


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def org(make_org):
    return await make_org(name="BM8 Test Org")


@pytest_asyncio.fixture
async def connections(db, org):
    """Two real Connection rows (source/target) — no real driver/network
    needed since fake_pool patches build_connector()/get_bulk_writer()
    below the boundary these are looked up at; get_connection() itself
    (the real code under test in the tenant-isolation tests) needs real
    rows to find or fail to find."""
    src = Connection(
        organization_id=org.id, name="bm8-source", category=ConnectionCategory.DATABASE,
        connection_type=ConnectionType.POSTGRESQL, environment=ConnectionEnvironment.TEST,
        config={"host": "fake", "port": "5432", "database": "fake"}, status=ConnectionStatus.ACTIVE,
    )
    tgt = Connection(
        organization_id=org.id, name="bm8-target", category=ConnectionCategory.DATABASE,
        connection_type=ConnectionType.POSTGRESQL, environment=ConnectionEnvironment.TEST,
        config={"host": "fake", "port": "5432", "database": "fake"}, status=ConnectionStatus.ACTIVE,
    )
    db.add_all([src, tgt])
    await db.commit()
    await db.refresh(src)
    await db.refresh(tgt)
    return src, tgt


@pytest_asyncio.fixture
async def pipeline(db, org, connections):
    src, tgt = connections
    return await config_svc.create_pipeline(
        db, organization_id=org.id, created_by=None, name=f"BM8 Pipeline {_slug()}",
        source_connection_id=src.id, target_connection_id=tgt.id, max_workers=4, max_retries=2, max_concurrent_runs=5,
    )


@pytest_asyncio.fixture
def make_object(db, pipeline):
    async def _make(*, source_object: str, target_object: str, depends_on=None, dependency_order: int = 0, load_type=BulkMigrationLoadType.FULL_REPLACE) -> BulkMigrationObject:
        return await config_svc.create_object(
            db, pipeline, actor_id=None, source_object=source_object, target_object=target_object,
            depends_on=depends_on, dependency_order=dependency_order, load_type=load_type,
        )
    return _make


@pytest.fixture
def fake_pool(monkeypatch):
    """Patches the DB-I/O boundary of the worker pool — see module
    docstring. `behaviors[object_key]` controls what a claimed task does:
    "success" (default), an Exception instance (raised as-is — the run
    orchestration classifies it), or ("slow", seconds) to simulate a
    long-running task a mid-flight pause/cancel can interrupt."""
    behaviors: dict[str, object] = {}
    calls: list[str] = []

    async def _fake_execute_task(source_connector, target_writer, task, *, organization_id, cancel_check):
        calls.append(task.object_key)
        behavior = behaviors.get(task.object_key, "success")
        if behavior == "success":
            # The real execute_task() marks the task SUCCESS itself (via
            # its own _mark_success(), a raw UPDATE on its own session) —
            # that responsibility isn't part of the "row movement" this
            # fake skips, so call the real function here too rather than
            # silently leaving the task never transitioned out of RUNNING.
            await task_executor._mark_success(task.id, task.run_id, organization_id, task.rows_processed or 0, task.rows_rejected or 0)
            return
        if isinstance(behavior, BaseException):
            raise behavior
        if isinstance(behavior, tuple) and behavior[0] == "slow":
            elapsed = 0.0
            step = 0.03
            while elapsed < behavior[1]:
                if await cancel_check(task.run_id):
                    raise task_executor.TaskCancelledSignal("cancelled mid-flight")
                await asyncio.sleep(step)
                elapsed += step
            await task_executor._mark_success(task.id, task.run_id, organization_id, task.rows_processed or 0, task.rows_rejected or 0)
            return
        raise RuntimeError(f"unrecognized fake behavior: {behavior!r}")

    monkeypatch.setattr(bm_svc.task_executor, "execute_task", _fake_execute_task)
    monkeypatch.setattr(bm_svc.connection_service, "build_connector", lambda conn: object())
    monkeypatch.setattr(bm_svc, "get_bulk_writer", lambda conn_type, connector: object())
    return behaviors, calls


async def _get_run(run_id: int) -> BulkMigrationRun:
    """Fresh short-lived session, never the test's own `db` fixture — the
    orchestration code under test (claim_task/_fail_or_retry/worker pool)
    commits via raw SQL UPDATEs against ITS OWN sessions, which never
    touches the identity map of any Python object the test already holds
    (AsyncSessionLocal is expire_on_commit=False — see database/connection.
    py). Re-querying fresh here is what proves the write actually landed,
    not just that some in-memory object still looks right."""
    async with AsyncSessionLocal() as s:
        return await s.get(BulkMigrationRun, run_id)


async def _get_task(task_id: int) -> BulkMigrationTask:
    async with AsyncSessionLocal() as s:
        return await s.get(BulkMigrationTask, task_id)


async def _wait_for_run_status(run_id: int, terminal: set[BulkMigrationRunStatus], timeout: float = 5.0) -> BulkMigrationRun:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        run = await _get_run(run_id)
        if run is not None and run.status in terminal:
            return run
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"run {run_id} did not reach {terminal} within {timeout}s (last status: {run.status if run else 'MISSING'})")
        await asyncio.sleep(0.05)


TERMINAL = {
    BulkMigrationRunStatus.COMPLETED, BulkMigrationRunStatus.COMPLETED_WITH_ERRORS,
    BulkMigrationRunStatus.FAILED, BulkMigrationRunStatus.CANCELLED,
}


# ── Dependency ordering + cycle detection ───────────────────────────────

class TestDependencyLevels:
    async def test_fine_grained_depends_on_chain_orders_correctly(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a")
        b = await make_object(source_object="b", target_object="b", depends_on=[a.object_key])
        c = await make_object(source_object="c", target_object="c", depends_on=[b.object_key])
        levels = planner._compute_dependency_levels([a, b, c])
        assert [lvl[0].object_key for lvl in levels] == [a.object_key, b.object_key, c.object_key]

    async def test_fine_grained_parallel_siblings_share_a_level(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a")
        b = await make_object(source_object="b", target_object="b", depends_on=[a.object_key])
        c = await make_object(source_object="c", target_object="c", depends_on=[a.object_key])
        levels = planner._compute_dependency_levels([a, b, c])
        assert len(levels) == 2
        assert {o.object_key for o in levels[1]} == {b.object_key, c.object_key}

    async def test_coarse_fallback_groups_by_dependency_order_when_no_depends_on_set(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a", dependency_order=1)
        b = await make_object(source_object="b", target_object="b", dependency_order=1)
        c = await make_object(source_object="c", target_object="c", dependency_order=2)
        levels = planner._compute_dependency_levels([a, b, c])
        assert len(levels) == 2
        assert {o.object_key for o in levels[0]} == {a.object_key, b.object_key}
        assert [o.object_key for o in levels[1]] == [c.object_key]

    async def test_cycle_is_detected_and_rejected(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a")
        b = await make_object(source_object="b", target_object="b", depends_on=[a.object_key])
        # Close the cycle: a now (also) depends on b.
        a.depends_on = [b.object_key]
        db.add(a)
        await db.commit()
        with pytest.raises(BulkMigrationError) as exc_info:
            planner._compute_dependency_levels([a, b])
        assert exc_info.value.code == BulkMigrationErrorCode.DEPENDENCY_ERROR

    async def test_create_run_rejects_cycle_before_persisting_anything(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a")
        b = await make_object(source_object="b", target_object="b", depends_on=[a.object_key])
        a.depends_on = [b.object_key]
        db.add(a)
        await db.commit()

        with pytest.raises(BulkMigrationError):
            await planner.create_run(db, pipeline)

        runs = (await db.execute(select(BulkMigrationRun).where(BulkMigrationRun.pipeline_id == pipeline.id))).scalars().all()
        assert runs == []  # nothing persisted — the cycle is caught before the first INSERT

    async def test_create_run_materializes_pending_and_blocked_tasks_by_level(self, db, pipeline, make_object):
        a = await make_object(source_object="a", target_object="a")
        b = await make_object(source_object="b", target_object="b", depends_on=[a.object_key])
        run = await planner.create_run(db, pipeline)
        tasks = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalars().all()
        by_key = {t.object_key: t for t in tasks}
        assert by_key[a.object_key].status == BulkMigrationTaskStatus.PENDING
        assert by_key[a.object_key].dependency_level == 0
        assert by_key[b.object_key].status == BulkMigrationTaskStatus.BLOCKED
        assert by_key[b.object_key].dependency_level == 1
        assert run.pending_objects == 1
        assert run.blocked_objects == 1
        assert run.total_objects == 2


# ── Atomic claim / no double-processing ─────────────────────────────────

class TestAtomicClaim:
    async def test_concurrent_claims_never_double_claim_a_task(self, db, pipeline, make_object):
        for i in range(5):
            await make_object(source_object=f"t{i}", target_object=f"t{i}")
        run = await planner.create_run(db, pipeline)

        async def _claim(worker_id: str):
            async with AsyncSessionLocal() as s:
                return await bm_svc.claim_task(s, run.id, worker_id)

        results = await asyncio.gather(*[_claim(f"w{i}") for i in range(10)])
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 5  # exactly one claim per task, never more than exist
        assert len({t.id for t in claimed}) == 5  # no task object claimed twice
        assert sum(1 for r in results if r is None) == 5  # the other 5 workers found nothing left

        for t in claimed:
            fresh = await _get_task(t.id)
            assert fresh.status == BulkMigrationTaskStatus.RUNNING
            assert fresh.attempt_number == 1

    async def test_claim_only_takes_pending_or_retry_pending(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()

        async with AsyncSessionLocal() as s:
            first = await bm_svc.claim_task(s, run.id, "w1")
        assert first is not None and first.id == task.id

        async with AsyncSessionLocal() as s:
            second = await bm_svc.claim_task(s, run.id, "w2")
        assert second is None  # already RUNNING — not claimable again


# ── Retry classification ────────────────────────────────────────────────

class TestRetryClassification:
    def test_configuration_error_is_permanent(self):
        code, message, permanent = bm_svc._classify_error(BulkMigrationError(BulkMigrationErrorCode.CONFIGURATION_ERROR, "bad config"))
        assert permanent is True
        assert code == "CONFIGURATION_ERROR"

    def test_dependency_error_is_permanent(self):
        _, _, permanent = bm_svc._classify_error(BulkMigrationError(BulkMigrationErrorCode.DEPENDENCY_ERROR, "cycle"))
        assert permanent is True

    def test_timeout_error_is_transient(self):
        code, _, permanent = bm_svc._classify_error(BulkMigrationError(BulkMigrationErrorCode.TIMEOUT, "slow"))
        assert permanent is False
        assert code == "TIMEOUT"

    def test_unclassified_exception_is_transient_unknown(self):
        code, _, permanent = bm_svc._classify_error(ConnectionError("network blip"))
        assert permanent is False
        assert code == "UNKNOWN_ERROR"

    async def test_permanent_failure_marks_task_failed_on_first_attempt(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        async with AsyncSessionLocal() as s:
            claimed = await bm_svc.claim_task(s, run.id, "w1")
            await bm_svc._fail_or_retry(s, run.id, claimed, "CONFIGURATION_ERROR", "bad config", permanent=True)
        fresh = await _get_task(task.id)
        assert fresh.status == BulkMigrationTaskStatus.FAILED
        assert fresh.error_code == "CONFIGURATION_ERROR"

    async def test_transient_failure_schedules_retry_with_future_backoff(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        assert task.max_retries >= 1  # pipeline fixture sets max_retries=2
        async with AsyncSessionLocal() as s:
            claimed = await bm_svc.claim_task(s, run.id, "w1")
            await bm_svc._fail_or_retry(s, run.id, claimed, "TIMEOUT", "slow response", permanent=False)
        fresh = await _get_task(task.id)
        assert fresh.status == BulkMigrationTaskStatus.RETRY_PENDING
        assert fresh.retry_after is not None
        # SQLite doesn't actually persist tzinfo (DateTime(timezone=True)
        # is a hint, not an enforced storage type there) — a value read
        # back is naive even though it was written as aware UTC, so
        # compare against a naive "now" too rather than assuming the
        # round-tripped column stayed timezone-aware.
        retry_after = fresh.retry_after if fresh.retry_after.tzinfo else fresh.retry_after.replace(tzinfo=timezone.utc)
        assert retry_after > datetime.now(timezone.utc)

    async def test_transient_failure_past_retry_budget_marks_failed_not_retry_pending(self, db, pipeline, make_object):
        """_fail_or_retry()'s exhaustion check is attempt_number >= max(1,
        max_retries) + 1 — the max(1, ...) floor guarantees at least one
        retry even when max_retries is configured to 0, so exhaustion here
        takes two failed attempts (claim -> fail -> RETRY_PENDING -> claim
        again -> fail again -> FAILED), not one."""
        obj = await make_object(source_object="a", target_object="a")
        await config_svc.update_object(db, pipeline, obj, actor_id=None, retry_count_override=0)
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        assert task.max_retries == 0

        async with AsyncSessionLocal() as s:
            claimed = await bm_svc.claim_task(s, run.id, "w1")
            await bm_svc._fail_or_retry(s, run.id, claimed, "TIMEOUT", "slow response", permanent=False)
        first_attempt = await _get_task(task.id)
        assert first_attempt.status == BulkMigrationTaskStatus.RETRY_PENDING  # the max(1, 0) floor still grants one retry
        assert first_attempt.retry_after is not None  # real backoff delay — not claimable again for ~10s+

        # Fast-forward past the backoff window so the 2nd claim below can
        # actually pick it up — claim_task()'s own WHERE clause requires
        # retry_after <= now, and BACKOFF_BASE_SECONDS alone is 10s.
        async with AsyncSessionLocal() as s:
            t = await s.get(BulkMigrationTask, task.id)
            t.retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
            await s.commit()

        async with AsyncSessionLocal() as s:
            reclaimed = await bm_svc.claim_task(s, run.id, "w2")
            await bm_svc._fail_or_retry(s, run.id, reclaimed, "TIMEOUT", "slow response again", permanent=False)
        fresh = await _get_task(task.id)
        assert fresh.status == BulkMigrationTaskStatus.FAILED  # attempt 2, budget now genuinely exhausted


# ── Checkpoint / crash recovery ─────────────────────────────────────────

class TestCrashRecovery:
    async def test_recover_stuck_tasks_requeues_running_tasks_with_budget_left(self, db, pipeline, make_object, fake_pool):
        """Uses fake_pool even though this test isn't about execution
        behavior — recover_stuck_bulk_migration_tasks() auto-redispatches
        a fresh worker-pool generation (asyncio.create_task) whenever it
        requeues at least one task, exactly like the "with budget left"
        case here does. Without a patched executor that background task
        would try a real connector against this fixture's fake
        host/port config; with it patched, and by waiting for the run to
        reach a terminal status below, the redispatch resolves cleanly
        within this test's own event loop instead of leaking past it."""
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        async with AsyncSessionLocal() as s:
            await bm_svc.claim_task(s, run.id, "w1")  # -> RUNNING, attempt_number=1 (max_retries=2, so budget remains)

        # Simulate the previous process dying mid-run: run status still RUNNING.
        async with AsyncSessionLocal() as s:
            r = await s.get(BulkMigrationRun, run.id)
            r.status = BulkMigrationRunStatus.RUNNING
            await s.commit()

        result = await bm_svc.recover_stuck_bulk_migration_tasks()
        assert result["recovered_runs"] >= 1
        assert result["requeued_tasks"] >= 1

        requeued = await _get_task(task.id)
        assert requeued.status == BulkMigrationTaskStatus.RETRY_PENDING
        assert requeued.error_code == "TIMEOUT"

        # The requeued task is still bound by its own retry_after backoff
        # window, so the auto-redispatched pool won't actually claim it
        # yet — clear that window, same as the backoff test above, then
        # let the (already-running) redispatched pool pick it up and
        # finish for real rather than leaving it dangling past this test.
        async with AsyncSessionLocal() as s:
            t = await s.get(BulkMigrationTask, task.id)
            t.retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
            await s.commit()
        finished = await _wait_for_run_status(run.id, TERMINAL, timeout=8.0)  # redispatched pool's outer loop sleeps 2s between generations
        assert finished.status == BulkMigrationRunStatus.COMPLETED
        assert finished.success_objects == 1

    async def test_recover_stuck_tasks_marks_failed_when_retry_budget_exhausted(self, db, pipeline, make_object):
        obj = await make_object(source_object="a", target_object="a")
        await config_svc.update_object(db, pipeline, obj, actor_id=None, retry_count_override=0)
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        async with AsyncSessionLocal() as s:
            await bm_svc.claim_task(s, run.id, "w1")

        # _recover_run_tasks()'s exhaustion check is the same max(1, 0)+1
        # floor as _fail_or_retry() — attempt_number must reach 2 before a
        # max_retries=0 task is actually treated as exhausted. Simulate
        # "this is the SECOND attempt, and it got stuck too" directly.
        async with AsyncSessionLocal() as s:
            t = await s.get(BulkMigrationTask, task.id)
            t.attempt_number = 2
            r = await s.get(BulkMigrationRun, run.id)
            r.status = BulkMigrationRunStatus.RUNNING
            await s.commit()

        await bm_svc.recover_stuck_bulk_migration_tasks()
        fresh = await _get_task(task.id)
        assert fresh.status == BulkMigrationTaskStatus.FAILED

        finalized_run = await _get_run(run.id)
        assert finalized_run.status in (BulkMigrationRunStatus.FAILED, BulkMigrationRunStatus.COMPLETED_WITH_ERRORS)
        assert finalized_run.failed_objects == 1  # _sync_live_counters kept this accurate through recovery too

    async def test_sweep_stale_tasks_only_touches_a_run_that_is_still_running(self, db, pipeline, make_object):
        obj = await make_object(source_object="a", target_object="a")
        await config_svc.update_object(db, pipeline, obj, actor_id=None, retry_count_override=0)
        run = await planner.create_run(db, pipeline)
        task = (await db.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
        async with AsyncSessionLocal() as s:
            await bm_svc.claim_task(s, run.id, "w1")

        stale_time = datetime.now(timezone.utc) - timedelta(seconds=bm_svc.STALE_HEARTBEAT_THRESHOLD_SECONDS + 30)
        async with AsyncSessionLocal() as s:
            t = await s.get(BulkMigrationTask, task.id)
            t.last_heartbeat = stale_time
            t.attempt_number = 2  # see the crash-recovery test above re: the max(1, max_retries)+1 exhaustion floor
            r = await s.get(BulkMigrationRun, run.id)
            r.status = BulkMigrationRunStatus.RUNNING  # sweep only acts on runs whose OWN status is still RUNNING
            await s.commit()

        result = await bm_svc.sweep_stale_bulk_migration_tasks()
        assert result["failed_tasks"] == 1  # max_retries=0, attempt 2 -> exhausted -> FAILED, no background pool re-dispatch triggered
        fresh = await _get_task(task.id)
        assert fresh.status == BulkMigrationTaskStatus.FAILED


# ── Pause / cancel (state machine + real worker-pool orchestration) ────

class TestPauseCancel:
    async def test_pause_rejected_when_run_is_not_running(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)  # status PENDING, never started
        with pytest.raises(bm_svc.BulkMigrationServiceError):
            await bm_svc.request_pause(db, run)

    async def test_cancel_rejected_when_run_already_terminal(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        run.status = BulkMigrationRunStatus.COMPLETED
        db.add(run)
        await db.commit()
        with pytest.raises(bm_svc.BulkMigrationServiceError):
            await bm_svc.request_cancel(db, run)

    async def test_resume_rejected_when_run_is_not_paused(self, db, pipeline, make_object):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)
        with pytest.raises(bm_svc.BulkMigrationServiceError):
            await bm_svc.resume_run(db, run)

    async def test_full_run_completes_successfully_through_the_worker_pool(self, db, pipeline, make_object, fake_pool):
        behaviors, calls = fake_pool
        await make_object(source_object="a", target_object="a")
        await make_object(source_object="b", target_object="b")
        run = await bm_svc.start_run(db, pipeline)

        finished = await _wait_for_run_status(run.id, TERMINAL)
        assert finished.status == BulkMigrationRunStatus.COMPLETED
        assert finished.success_objects == 2
        assert finished.failed_objects == 0
        assert finished.pending_objects == 0
        assert set(calls) == {".a->.a", ".b->.b"}

    async def test_cancel_mid_flight_finalizes_as_cancelled_not_completed(self, db, pipeline, make_object, fake_pool):
        """Regression test for a real bug found live during BM7: if
        cancel_requested flips to True while a generation's workers are
        already claimed and executing, the outer loop used to fall
        straight through to the finalize branch and report COMPLETED even
        though cancelled_objects > 0. Fixed by re-checking cancel/pause
        immediately after each generation drains (bulk_migration_service.
        py's _run_worker_pool)."""
        behaviors, calls = fake_pool
        obj = await make_object(source_object="a", target_object="a")
        behaviors[obj.object_key] = ("slow", 1.5)
        run = await bm_svc.start_run(db, pipeline)

        await asyncio.sleep(0.15)  # let the worker actually claim + start executing
        live_run = await _get_run(run.id)
        assert live_run.status == BulkMigrationRunStatus.RUNNING
        async with AsyncSessionLocal() as s:
            r = await s.get(BulkMigrationRun, run.id)
            await bm_svc.request_cancel(s, r)

        finished = await _wait_for_run_status(run.id, TERMINAL, timeout=5.0)
        assert finished.status == BulkMigrationRunStatus.CANCELLED
        assert finished.cancelled_objects == 1

    async def test_pause_race_after_generation_drains_finalizes_normally_not_stuck_paused(self, db, pipeline, make_object, fake_pool):
        """Companion regression test: the asymmetric half of the same
        fix — pause requested while the last task is already in flight
        (not before it was even claimed, which is a genuinely different,
        correctly-PAUSED case) should NOT leave the run stuck PAUSED with
        nothing to resume once that task finishes; it should finalize
        normally and the stale pause_requested flag should be cleared.
        Uses a "slow" fake task specifically so the pause request is
        guaranteed to land mid-generation (polling on the TASK's own
        status, not the run's, since the run flips to RUNNING immediately
        on dispatch — well before the task is actually claimed)."""
        behaviors, calls = fake_pool
        obj = await make_object(source_object="a", target_object="a")
        behaviors[obj.object_key] = ("slow", 0.3)
        run = await bm_svc.start_run(db, pipeline)

        for _ in range(50):
            async with AsyncSessionLocal() as s:
                t = (await s.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalar_one()
                if t.status == BulkMigrationTaskStatus.RUNNING:
                    r = await s.get(BulkMigrationRun, run.id)
                    await bm_svc.request_pause(s, r)  # lands squarely mid-generation
                    break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("task never reached RUNNING within the polling window")

        finished = await _wait_for_run_status(run.id, TERMINAL, timeout=5.0)
        assert finished.status == BulkMigrationRunStatus.COMPLETED
        assert finished.pause_requested is False
        assert finished.success_objects == 1

    async def test_dependency_chain_produces_a_real_skipped_task_and_correct_counters(self, db, pipeline, make_object, fake_pool):
        """Regression test for the other real bug found live during BM7:
        skipped_objects was never incremented anywhere. Fixed by making
        _sync_live_counters() the single source of truth for every
        object-status counter, called after _promote_blocked_tasks()
        transitions a BLOCKED task to SKIPPED."""
        behaviors, calls = fake_pool
        upstream = await make_object(source_object="up", target_object="up")
        downstream = await make_object(source_object="down", target_object="down", depends_on=[upstream.object_key])
        behaviors[upstream.object_key] = BulkMigrationError(BulkMigrationErrorCode.CONFIGURATION_ERROR, "upstream is broken")

        run = await bm_svc.start_run(db, pipeline)
        finished = await _wait_for_run_status(run.id, TERMINAL)

        assert finished.status == BulkMigrationRunStatus.COMPLETED_WITH_ERRORS
        assert finished.failed_objects == 1
        assert finished.skipped_objects == 1
        assert finished.pending_objects == 0
        assert finished.blocked_objects == 0
        assert downstream.object_key not in calls  # the skipped task's executor never even ran

        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id))).scalars().all()
        by_key = {t.object_key: t for t in rows}
        assert by_key[upstream.object_key].status == BulkMigrationTaskStatus.FAILED
        assert by_key[downstream.object_key].status == BulkMigrationTaskStatus.SKIPPED
        assert by_key[downstream.object_key].error_code == "DEPENDENCY_ERROR"

    async def test_retry_failed_requeues_and_resyncs_counters_without_double_counting(self, db, pipeline, make_object, fake_pool):
        behaviors, calls = fake_pool
        obj = await make_object(source_object="a", target_object="a")
        behaviors[obj.object_key] = BulkMigrationError(BulkMigrationErrorCode.CONFIGURATION_ERROR, "boom")
        run = await bm_svc.start_run(db, pipeline)
        first = await _wait_for_run_status(run.id, TERMINAL)
        assert first.status == BulkMigrationRunStatus.FAILED
        assert first.failed_objects == 1

        behaviors[obj.object_key] = "success"  # "fix" the underlying issue before retrying
        async with AsyncSessionLocal() as s:
            r = await s.get(BulkMigrationRun, run.id)
            requeued = await bm_svc.retry_failed(s, r)
        assert requeued == 1

        second = await _wait_for_run_status(run.id, TERMINAL)
        assert second.status == BulkMigrationRunStatus.COMPLETED
        assert second.success_objects == 1
        assert second.failed_objects == 0  # not still 1 from before the retry — the exact bug this fix targets


# ── Tenant isolation ─────────────────────────────────────────────────────

class TestTenantIsolation:
    async def test_get_connection_is_scoped_to_its_owning_organization(self, db, org, connections, make_org):
        src, _tgt = connections
        other_org = await make_org(name="Other Org")
        from app.services import connection_service
        assert await connection_service.get_connection(db, src.id, org.id) is not None
        assert await connection_service.get_connection(db, src.id, other_org.id) is None

    async def test_pipelines_are_not_visible_across_organizations(self, db, org, pipeline, make_org):
        other_org = await make_org(name="Other Org")
        own = await config_svc.list_pipelines(db, org.id)
        other = await config_svc.list_pipelines(db, other_org.id)
        assert pipeline.id in {p.id for p in own["items"]}
        assert pipeline.id not in {p.id for p in other["items"]}

    async def test_claim_task_never_crosses_into_another_orgs_run(self, db, org, pipeline, make_object, make_org):
        await make_object(source_object="a", target_object="a")
        run = await planner.create_run(db, pipeline)

        other_org = await make_org(name="Other Org")
        other_src = Connection(organization_id=other_org.id, name="other-src", category=ConnectionCategory.DATABASE, connection_type=ConnectionType.POSTGRESQL, config={}, status=ConnectionStatus.ACTIVE)
        other_tgt = Connection(organization_id=other_org.id, name="other-tgt", category=ConnectionCategory.DATABASE, connection_type=ConnectionType.POSTGRESQL, config={}, status=ConnectionStatus.ACTIVE)
        db.add_all([other_src, other_tgt])
        await db.commit()
        await db.refresh(other_src)
        await db.refresh(other_tgt)
        other_pipeline = await config_svc.create_pipeline(
            db, organization_id=other_org.id, created_by=None, name="Other Pipeline",
            source_connection_id=other_src.id, target_connection_id=other_tgt.id,
        )
        await config_svc.create_object(db, other_pipeline, actor_id=None, source_object="x", target_object="x")
        other_run = await planner.create_run(db, other_pipeline)

        # Claiming against the FIRST org's run must only ever surface that
        # run's own task, never the second org's — even though both runs
        # have exactly one claimable PENDING task sitting in the same
        # bulk_migration_tasks table at the same time.
        async with AsyncSessionLocal() as s:
            claimed = await bm_svc.claim_task(s, run.id, "w1")
        assert claimed is not None
        assert claimed.run_id == run.id

        async with AsyncSessionLocal() as s:
            claimed_other = await bm_svc.claim_task(s, other_run.id, "w2")
        assert claimed_other is not None
        assert claimed_other.run_id == other_run.id
        assert claimed_other.id != claimed.id

        # And a second claim attempt against the first run must not somehow
        # pick up the second org's (still-PENDING-until-just-now) task.
        async with AsyncSessionLocal() as s:
            nothing_left = await bm_svc.claim_task(s, run.id, "w3")
        assert nothing_left is None


# ── RBAC / module entitlement ────────────────────────────────────────────

class TestModuleAccess:
    async def test_access_denied_with_no_entitlement_at_all(self, db, org):
        result = await access_service.has_module_access(db, org.id, "BULK_MIGRATION")
        assert result.granted is False

    async def test_access_granted_with_an_active_subscription_entitlement(self, db, org, make_subscription):
        await make_subscription(organization_id=org.id, modules=["BULK_MIGRATION"], status=SubscriptionStatus.ACTIVE)
        result = await access_service.has_module_access(db, org.id, "BULK_MIGRATION")
        assert result.granted is True

    async def test_access_denied_when_the_only_subscription_is_expired(self, db, org, make_subscription):
        await make_subscription(
            organization_id=org.id, modules=["BULK_MIGRATION"], status=SubscriptionStatus.EXPIRED,
            end_date=datetime.now(timezone.utc) - timedelta(days=1),
        )
        result = await access_service.has_module_access(db, org.id, "BULK_MIGRATION")
        assert result.granted is False

    async def test_module_is_a_real_registered_module_key(self):
        from app.services.license_service import ALL_MODULES
        assert "BULK_MIGRATION" in ALL_MODULES
