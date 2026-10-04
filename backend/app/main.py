"""
Data Intelligence Platform — FastAPI backend
Entry point: uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import sqlite3
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.database.connection import init_db
from app.api.auth import router as auth_router
from app.api.data_catalog import router as data_catalog_router
from app.api.documents import router as documents_router
from app.api.review_queue import router as review_queue_router
from app.api.connections import router as connections_router
from app.api.schedules import router as schedules_router
from app.api.pipelines import router as pipelines_router
from app.api.incidents import router as incidents_router
from app.api.user_admin import router as user_admin_router
from app.api.sessions import router as sessions_router
from app.api.sso import router as sso_router
from app.api.sso_admin import router as sso_admin_router
from app.api.roles import router as roles_router
from app.api.licenses import router as licenses_router
from app.api.audit_logs import router as audit_logs_router
from app.api.settings import router as settings_router
from app.api.executions import router as executions_router
from app.api.mapping import router as mapping_router
from app.api.pipeline_mapping import router as pipeline_mapping_router
from app.api.loads import router as loads_router
from app.api.validation import router as validation_router
from app.api.reconciliation import router as reconciliation_router
from app.api.document_storage import router as document_storage_router
from app.api.document_webhook import router as document_webhook_router
from app.api.transformation import router as transformation_router
from app.api.sql_workspace import router as sql_workspace_router
from app.api.seller.dashboard import router as seller_dashboard_router
from app.api.seller.customers import router as seller_customers_router
from app.api.seller.subscriptions import router as seller_subscriptions_router
from app.api.seller.modules import router as seller_modules_router
from app.api.seller.licenses import router as seller_licenses_router
from app.api.license_check import router as license_check_router
from app.api.seller.usage import router as seller_usage_router
from app.api.seller.seller_users import router as seller_users_router
from app.api.seller.audit_logs import router as seller_audit_logs_router
from app.api.seller.settings import router as seller_settings_router
from app.api.seller.billing import router as seller_billing_router
from app.api.seller.companies import router as seller_companies_router
from app.api.pipeline_files import router as pipeline_files_router
from app.api.pipeline_jobs import router as pipeline_jobs_router
from app.api.dashboard import router as dashboard_router
from app.api.extraction import router as extraction_router
from app.api.monitoring import router as monitoring_router
from app.api.lineage import router as lineage_router
from app.api.uploaded_files import router as uploaded_files_router
from app.api.settings_database import router as settings_database_router
from app.api.anomaly import router as anomaly_router
from app.api.access import router as access_router
from app.api.billing.invoices import router as billing_invoices_router
from app.api.billing.payment_gateways import router as payment_gateways_router
from app.api.billing.notifications import router as notifications_router
from app.api.billing.subscriptions import router as billing_subscriptions_router
from app.api.payments.webhook import router as payments_webhook_router
from app.api.payments.pay import router as payments_pay_router
from app.api.invoices import router as invoices_router
from app.api.invoice_mappings import router as invoice_mappings_router
from app.api.invoice_rules import router as invoice_rules_router
from app.api.approvals import router as approvals_router
from app.api.bulk_migration import router as bulk_migration_router
from app.api.tally import router as tally_router
from app.models.pipeline_source_file import PipelineSourceFile  # noqa: F401 — string FK to pipelines.id needs this imported before init_db()
from app.models.tenant_database import TenantDatabase  # noqa: F401 — new table, needs registering before init_db()
from app.models.tenant_migration_job import TenantMigrationJob, TenantMigrationJobLog  # noqa: F401 — new tables, need registering before init_db()
from app.models.pipeline_job import PipelineJob, PipelineJobLog  # noqa: F401 — new tables, need registering before init_db()
# Seller/Plan are only referenced by string FK ("sellers.id") from
# Subscription — SQLAlchemy needs the Seller/Plan classes themselves
# imported somewhere before init_db()'s create_all() runs, or FK
# resolution fails with NoReferencedTableError. Subscription (pulled in
# transitively above via seller_dashboard_router -> dashboard_service)
# already imports Plan; Seller needs an explicit import.
from app.models.seller import Seller  # noqa: F401
from app.models.seller_audit_log import SellerAuditLog  # noqa: F401
from app.models.query_execution_log import QueryExecutionLog  # noqa: F401 — new table, needs registering before init_db()
from app.models.sql_export_job import SqlWorkspaceExportJob  # noqa: F401 — new table, needs registering before init_db()
from app.models.monitoring import SlaDefinition, AlertRule, AlertEvent  # noqa: F401 — new tables, need registering before init_db()
from app.models.document_storage import DocumentProcessedDataset, ProcessedDataStorageConfig  # noqa: F401 — new tables, need registering before init_db()
from app.models.document_webhook import DocumentWebhookConfig, DocumentWebhookDelivery  # noqa: F401 — new tables, need registering before init_db()
from app.models.transformation_job import TransformationJob  # noqa: F401 — new table, needs registering before init_db()
from app.models.user_session import UserSession  # noqa: F401 — new table, needs registering before init_db()
from app.models.sso_configuration import SsoConfiguration  # noqa: F401 — new table, needs registering before init_db()
from app.models.uploaded_dataset_file import UploadedDatasetFile  # noqa: F401 — new table, needs registering before init_db()
from app.models.anomaly import (  # noqa: F401 — new tables, need registering before init_db()
    AnomalyJob, AnomalyExecution, AnomalyColumnResult, AnomalyRecord,
    AnomalyBaseline, AnomalyFeedback, AnomalyReport,
)
# Multi-Subscription Billing & Payments Platform (2026) — new tables,
# need registering before init_db(). No routers reference them yet
# (SB2 is models-only); explicit imports here follow this file's own
# established pattern for that situation (see PipelineJob/TenantDatabase
# above).
from app.models.billing.invoice import Invoice, InvoiceLineItem, InvoiceCounter  # noqa: F401
from app.models.billing.payment_gateway import PaymentGateway, PaymentGatewayConfiguration  # noqa: F401
from app.models.billing.payment import Payment  # noqa: F401
from app.models.billing.payment_link import PaymentLink  # noqa: F401
from app.models.billing.payment_webhook_event import PaymentWebhookEvent  # noqa: F401
from app.models.billing.notification import Notification  # noqa: F401
from app.models.billing.module_price import ModulePrice  # noqa: F401 — per-module price catalog, see SC1
from app.models.company import Company  # noqa: F401 — new table, needs registering before init_db()
# Scheduled Document Intelligence Workflow (2026) — new tables, need
# registering before init_db() the same way every model above does.
from app.models.document_schedule import DocumentScheduleConfig, DocumentSourceRegistry  # noqa: F401
# Bulk Object Migration (2026, BM1) — config-driven 5,000+ object migration
# domain (one pipeline + many objects + one run + many tasks). New tables,
# need registering before init_db(). "bulk_migration"/BulkMigration* prefix
# avoids colliding with the unrelated TenantMigrationJob (BYODB org/platform
# database migration — see models/tenant_migration_job.py).
from app.models.bulk_migration import BulkMigrationPipeline, BulkMigrationObject  # noqa: F401
from app.models.bulk_migration_run import BulkMigrationRun, BulkMigrationTask, BulkMigrationTaskEvent  # noqa: F401
# Invoice Intelligence (2026, INV1) — canonical AP-invoice domain models,
# new tables, need registering before init_db(). `ApInvoice*` prefix
# avoids colliding with models.billing.invoice.Invoice (SaaS subscription
# billing — a different domain entirely).
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine, ApInvoiceVendor  # noqa: F401
from app.models.invoice_intelligence.erp import ApInvoiceErpTransaction, ApInvoiceReconciliation  # noqa: F401
from app.models.invoice_intelligence.rules import (  # noqa: F401
    ApInvoiceValidationRule, ApInvoiceValidationResult, ApInvoiceErpMapping, ApInvoiceModelVersion,
)
from app.models.invoice_intelligence.learning import ApInvoiceLearningRecord, ApInvoiceAuditEvent  # noqa: F401
# Payment Invoice Approval Workflow (2026, Phase 2) — Approval Rule Engine
# models, new tables, need registering before init_db() the same way
# every model above does.
from app.models.approval.rules import (  # noqa: F401
    ApprovalRule, ApprovalRuleCondition, ApprovalRuleStep, ApprovalRuleAuditLog,
)
# Payment Invoice Approval Workflow (2026, Phase 4) — workflow INSTANCE
# models (the rule engine's output made durable per-invoice). Approver
# resolution (Phase 5) needed no new tables — notification lands here.
from app.models.approval.workflow import (  # noqa: F401
    ApprovalWorkflow, ApprovalWorkflowStep, PeerReviewStep, ApprovalWorkflowAuditLog,
)
# Payment Invoice Approval Workflow (2026, Phase 6) — the real queued/
# retried email outbox (see services/approval/notification_outbox_service.py).
from app.models.approval.notification import ApprovalNotificationOutbox  # noqa: F401
# ZIP Batch Upload (2026, ZB1) — one ZIP = one chunked upload session + one
# batch run + many per-file claimable tasks, for both Document Intelligence
# and Invoice Intelligence. New tables, need registering before init_db().
from app.models.document_batch import (  # noqa: F401
    BatchUpload, DocumentBatch, DocumentBatchFile, DocumentBatchEvent,
)
# Tally Integration (TALLY-B) — read-only extraction snapshot tables.
from app.models.tally_extraction import TallyExtractionRun, TallyExtractionRecord  # noqa: F401
# Tally Integration (TALLY-C) — canonical accounting model, materialized
# from the above extraction snapshots.
from app.models.tally_canonical import TallyCanonicalVoucher, TallyCanonicalParty, TallyCanonicalLedger  # noqa: F401
# Tally Integration (TALLY-D) — account mapping + voucher preview +
# pre-write validation.
from app.models.tally_mapping import TallyAccountMapping, TallyVoucherPreview, TallyVoucherValidationResult  # noqa: F401
# Tally Integration (TALLY-E) — reconciliation between ApInvoice and
# TallyCanonicalVoucher/Party, reusing app/reconciliation/engine.py's
# real row-diffing (see that module's own docstring for why).
from app.models.tally_reconciliation import TallyReconciliationRun, TallyReconciliationException  # noqa: F401
# Tally Integration (TALLY-I) — the actual write-back attempt/verification
# tables, gated behind TALLY-H's approval workflow.
from app.models.tally_writeback import TallyVoucherWriteBackTransaction, TallyVoucherWriteBackVerification  # noqa: F401
# Tally Integration (TALLY-J) — scheduled/batch reconciliation config,
# additive extension of the Schedules module (see that model's own
# docstring for the AD12/Scheduled-Document-Intelligence precedent it follows).
from app.models.tally_schedule import TallyReconciliationScheduleConfig  # noqa: F401

settings = get_settings()
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — checking/initialising database …")
    # Ensure any bundled backup DB is restored if the runtime DB has no users
    await _restore_db_from_backup_if_needed()
    await init_db()

    # Clean up stale/orphaned running or aborting pipeline jobs from previous process restarts
    try:
        from app.database import tenant_engine_registry
        from app.models.pipeline_job import PipelineJob, JobStatus
        from sqlalchemy import select, update
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(None)
        async with sessionmaker() as db:
            stale_jobs = (await db.scalars(
                select(PipelineJob).where(PipelineJob.status.in_([JobStatus.RUNNING, JobStatus.PENDING]))
            )).all()
            if stale_jobs:
                logger.info(f"Cleaning up {len(stale_jobs)} stale running/pending pipeline jobs from previous server restart...")
                for j in stale_jobs:
                    j.status = JobStatus.CANCELLED
                    if not j.error_message:
                        j.error_message = "Job process interrupted by server restart."
                await db.commit()
    except Exception as exc:
        logger.warning(f"Could not clean up stale jobs on startup: {exc}")
    await _seed_superadmin()
    await _seed_seller_admin()
    await _seed_seller_data()
    await _seed_customer_modules()
    await _recover_document_schedules()
    await _recover_bulk_migration_tasks()
    await _recover_document_batches()
    await _recover_tally_extraction_runs()
    scheduler = _start_billing_scheduler()
    yield
    scheduler.shutdown(wait=False)
    logger.info("Shutting down.")


async def _recover_document_schedules() -> None:
    """Scheduled Document Intelligence Workflow (2026) — spec section
    56/58 startup recovery: any ScheduleRun/DocumentSourceRegistry row
    left RUNNING/PROCESSING/QUEUED by a backend process that died mid-run
    gets un-stuck here, same place every other one-time startup task
    above already runs. See schedule_service.recover_stuck_document_schedules()."""
    from app.services.schedule_service import recover_stuck_document_schedules

    stats = await recover_stuck_document_schedules()
    if stats["recovered_runs"] or stats["requeued_documents"] or stats["failed_documents"]:
        logger.info("Document schedule recovery: %s", stats)


async def _recover_bulk_migration_tasks() -> None:
    """Bulk Object Migration (2026, BM5) startup recovery — same place
    and same shape as _recover_document_schedules() above. See
    bulk_migration_service.recover_stuck_bulk_migration_tasks()."""
    from app.services.bulk_migration_service import recover_stuck_bulk_migration_tasks

    stats = await recover_stuck_bulk_migration_tasks()
    if stats["recovered_runs"] or stats["requeued_tasks"] or stats["failed_tasks"]:
        logger.info("Bulk migration recovery: %s", stats)


async def _recover_document_batches() -> None:
    """ZIP Batch Upload (2026, ZB8) startup recovery — same place and same
    shape as _recover_bulk_migration_tasks() above. See
    document_batch_service.recover_stuck_document_batches()."""
    from app.services.document_batch_service import recover_stuck_document_batches

    stats = await recover_stuck_document_batches()
    if stats["recovered_batches"] or stats["requeued_files"] or stats["failed_files"]:
        logger.info("Document batch recovery: %s", stats)


async def _recover_tally_extraction_runs() -> None:
    """TALLY-J startup recovery — same place and same shape as
    _recover_document_schedules() above. See
    tally_extraction_service.recover_stuck_tally_extraction_runs()."""
    from app.services.tally_extraction_service import recover_stuck_tally_extraction_runs

    stats = await recover_stuck_tally_extraction_runs()
    if stats["recovered_runs"]:
        logger.info("Tally extraction recovery: %s", stats)


async def _restore_db_from_backup_if_needed() -> None:
    """If the platform DB exists but contains no users, try restoring
    it from the newest bundled `dip.db.bak-*` file found in the repo.
    This helps recover seeded users after an image rebuild.
    """
    # Working directory at container start is /app/backend (see Dockerfile/CMD)
    db_path = Path("./dip.db")
    if not db_path.exists():
        # Nothing to do — init_db() will create a fresh DB
        return

    def _users_count(path: Path) -> Optional[int]:
        try:
            con = sqlite3.connect(str(path))
            cur = con.cursor()
            cur.execute("SELECT count(*) FROM users")
            r = cur.fetchone()
            cur.close()
            con.close()
            return int(r[0]) if r else 0
        except Exception:
            return None

    current_count = _users_count(db_path)
    if current_count and current_count > 0:
        logger.info("Existing DB has %d users; no restore needed.", current_count)
        return

    # Find newest backup file by lexicographic timestamp suffix
    backups = sorted(Path(".").glob("dip.db.bak-*"), reverse=True)
    if not backups:
        logger.info("No bundled DB backup found to restore.")
        return

    for backup in backups:
        try:
            logger.info("Attempting DB restore from %s", str(backup))
            shutil.copyfile(str(backup), str(db_path))
            restored_count = _users_count(db_path)
            if restored_count and restored_count > 0:
                logger.info("DB restored from %s — %d users present.", str(backup), restored_count)
                return
            else:
                logger.warning("Restored DB from %s but users count is %s; trying next backup.", str(backup), restored_count)
        except Exception:
            logger.exception("Failed to restore DB from %s", str(backup))

    logger.warning("DB restore attempted but no backup produced a users table with rows.")


def _start_billing_scheduler():
    """The one in-process AsyncIOScheduler this backend runs, inside the
    same event loop uvicorn already uses — no separate worker process.
    Originally just the two billing sweeps (SB22 — the first real
    background worker this codebase had); now also runs the Schedules
    poller (scheduler_poller.run_poll — spec section 10/56's "scheduler
    starts automatically", closing the gap schedule_service.py's own
    prior docstring called out: "nothing here fires automatically on a
    wall-clock timer"). Every job is individually try/excepted so one
    job's failure never cancels the others or crashes the scheduler
    thread."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.services.billing.billing_scheduler import run_billing_sweep_standalone
    from app.services.billing.expiry_job import run_expiry_sweep_standalone
    from app.services.scheduler_poller import run_poll as run_schedule_poll
    from app.services.approval.notification_outbox_service import process_pending_standalone as run_approval_notification_outbox
    from app.services.approval.sla_scheduler_service import run_sla_sweep_standalone as run_approval_sla_sweep
    from app.services.bulk_migration_service import sweep_stale_bulk_migration_tasks as run_bulk_migration_stale_sweep
    from app.services.document_batch_service import sweep_stale_document_batch_files as run_document_batch_stale_sweep
    from app.services.document_batch_service import cleanup_expired_document_batches as run_document_batch_retention_cleanup
    from app.services.batch_upload_service import cleanup_expired_uploads as run_batch_upload_retention_cleanup

    async def _run_expiry_sweep_safely():
        try:
            stats = await run_expiry_sweep_standalone()
            logger.info("Expiry sweep completed: %s", stats)
        except Exception:
            logger.exception("Expiry sweep failed")

    async def _run_billing_sweep_safely():
        try:
            stats = await run_billing_sweep_standalone()
            logger.info("Billing sweep completed: %s", stats)
        except Exception:
            logger.exception("Billing sweep failed")

    async def _run_schedule_poll_safely():
        try:
            stats = await run_schedule_poll()
            if stats["fired"] or stats["errors"]:
                logger.info("Schedule poll: %s", stats)
        except Exception:
            logger.exception("Schedule poll failed")

    async def _run_approval_notification_outbox_safely():
        try:
            stats = await run_approval_notification_outbox()
            if stats["picked_up"]:
                logger.info("Approval notification outbox: %s", stats)
        except Exception:
            logger.exception("Approval notification outbox worker failed")

    async def _run_approval_sla_sweep_safely():
        try:
            stats = await run_approval_sla_sweep()
            if stats.get("reminders_processed") or stats.get("escalated") or stats.get("escalation_failed"):
                logger.info("Approval SLA sweep: %s", stats)
        except Exception:
            logger.exception("Approval SLA sweep failed")

    async def _run_bulk_migration_stale_sweep_safely():
        try:
            stats = await run_bulk_migration_stale_sweep()
            if stats["requeued_tasks"] or stats["failed_tasks"]:
                logger.info("Bulk migration stale-task sweep: %s", stats)
        except Exception:
            logger.exception("Bulk migration stale-task sweep failed")

    async def _run_document_batch_stale_sweep_safely():
        try:
            stats = await run_document_batch_stale_sweep()
            if stats["requeued_files"] or stats["failed_files"]:
                logger.info("Document batch stale-file sweep: %s", stats)
        except Exception:
            logger.exception("Document batch stale-file sweep failed")

    async def _run_document_batch_retention_cleanup_safely():
        try:
            batch_stats = await run_document_batch_retention_cleanup()
            upload_stats = await run_batch_upload_retention_cleanup()
            if batch_stats["purged_batches"] or upload_stats["purged_completed_zips"] or upload_stats["marked_abandoned"]:
                logger.info("Document batch retention cleanup: %s / %s", batch_stats, upload_stats)
        except Exception:
            logger.exception("Document batch retention cleanup failed")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_run_expiry_sweep_safely, "interval", hours=1, id="billing_expiry_sweep")
    scheduler.add_job(_run_billing_sweep_safely, "interval", hours=1, id="billing_cycle_sweep")
    # 60s — real-time enough that "Daily at 02:00" fires within a minute
    # of due, without hammering the DB with a due-schedules query faster
    # than any real schedule frequency (minimum granularity is "Every
    # Hour") would ever need.
    scheduler.add_job(_run_schedule_poll_safely, "interval", seconds=60, id="schedule_poll")
    # 60s — an approver waiting on an email shouldn't wait minutes for the
    # first attempt; retries beyond that follow the outbox's own
    # exponential backoff (notification_outbox_service._BACKOFF_MINUTES),
    # not this poll interval.
    scheduler.add_job(_run_approval_notification_outbox_safely, "interval", seconds=60, id="approval_notification_outbox")
    # 15 minutes — reminder/escalation timing is hour-granularity by
    # design (reminder_after_hours/escalation_after_hours), so this
    # doesn't need 60s polling; 15 min keeps a breach from sitting
    # unnoticed for long without adding meaningful DB load.
    scheduler.add_job(_run_approval_sla_sweep_safely, "interval", minutes=15, id="approval_sla_sweep")
    # 2 minutes — well under STALE_HEARTBEAT_THRESHOLD_SECONDS (5 min), so
    # a hung task's heartbeat isn't stale for much longer than the
    # threshold itself before this sweep notices and requeues it.
    scheduler.add_job(_run_bulk_migration_stale_sweep_safely, "interval", minutes=2, id="bulk_migration_stale_sweep")
    # 2 minutes — same reasoning as the bulk-migration sweep above, sized
    # against _STALE_FILE_HEARTBEAT_THRESHOLD_SECONDS (180s = 6 missed
    # heartbeat ticks at document_batch_service._HEARTBEAT_INTERVAL_SECONDS).
    scheduler.add_job(_run_document_batch_stale_sweep_safely, "interval", minutes=2, id="document_batch_stale_sweep")
    # Once daily — this is disk housekeeping (expired ZIPs/extracted files,
    # abandoned uploads), not a correctness-critical recovery path, so it
    # doesn't need the stale-sweep's tight interval; retention windows are
    # measured in days (zip_retention_days/extracted_file_retention_days,
    # default 30) so a day's delay in reclaiming disk space is immaterial.
    scheduler.add_job(_run_document_batch_retention_cleanup_safely, "interval", hours=24, id="document_batch_retention_cleanup")
    scheduler.start()
    logger.info("Backend scheduler started — billing sweeps hourly, schedule poll + approval notification outbox every 60s, approval SLA sweep every 15min.")
    return scheduler


async def _seed_superadmin() -> None:
    """Create the platform super-admin if it doesn't exist yet."""
    from app.database.connection import AsyncSessionLocal
    from app.models.user import User, UserRole
    from app.core.security import hash_password
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.email == settings.first_superadmin_email)
        )
        if result.scalar_one_or_none():
            return
        admin = User(
            email=settings.first_superadmin_email,
            full_name="Platform Admin",
            hashed_password=hash_password(settings.first_superadmin_password),
            role=UserRole.SUPER_ADMIN,
            is_active=True,
            is_verified=True,
        )
        db.add(admin)
        await db.commit()
        logger.info("Seeded platform super-admin: %s", settings.first_superadmin_email)


async def _seed_seller_admin() -> None:
    """Create the platform's first Seller Portal admin if it doesn't exist
    yet — same bootstrap pattern as _seed_superadmin() above. Seller Portal
    (SELLER PORTAL spec) — organization_id stays NULL: a Seller user isn't
    a customer, see core/deps.require_seller_action."""
    from app.database.connection import AsyncSessionLocal
    from app.models.user import User, UserRole
    from app.core.security import hash_password
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.email == settings.first_seller_admin_email)
        )
        if result.scalar_one_or_none():
            return
        seller_admin = User(
            email=settings.first_seller_admin_email,
            full_name="Seller Admin",
            hashed_password=hash_password(settings.first_seller_admin_password),
            role=UserRole.SELLER,
            seller_role="SELLER_ADMIN",
            is_active=True,
            is_verified=True,
        )
        db.add(seller_admin)
        await db.commit()
        logger.info("Seeded seller admin: %s", settings.first_seller_admin_email)


async def _seed_seller_data() -> None:
    """Seed the Seller row + starter Plan catalog + backfill a Subscription
    for every pre-existing Organization — see services/seller/plan_seed.py."""
    from app.database.connection import AsyncSessionLocal
    from app.services.seller.plan_seed import ensure_seller_seeded

    async with AsyncSessionLocal() as db:
        await ensure_seller_seeded(db)


async def _seed_customer_modules() -> None:
    """Backfill a real CustomerModule row for every module already in an
    Organization's licensed_modules — see
    services/seller/license_service.ensure_customer_modules_seeded."""
    from app.database.connection import AsyncSessionLocal
    from app.services.seller.license_service import ensure_customer_modules_seeded

    async with AsyncSessionLocal() as db:
        await ensure_customer_modules_seeded(db)


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers ────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(data_catalog_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(review_queue_router, prefix="/api")
app.include_router(connections_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(pipelines_router, prefix="/api")
app.include_router(incidents_router, prefix="/api")
app.include_router(user_admin_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(sso_router, prefix="/api")
app.include_router(sso_admin_router, prefix="/api")
app.include_router(roles_router, prefix="/api")
app.include_router(licenses_router, prefix="/api")
app.include_router(audit_logs_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(executions_router, prefix="/api")
app.include_router(mapping_router, prefix="/api")
app.include_router(pipeline_mapping_router, prefix="/api")
app.include_router(loads_router, prefix="/api")
app.include_router(validation_router, prefix="/api")
app.include_router(reconciliation_router, prefix="/api")
app.include_router(document_storage_router, prefix="/api")
app.include_router(document_webhook_router, prefix="/api")
app.include_router(transformation_router, prefix="/api")
app.include_router(sql_workspace_router, prefix="/api")
app.include_router(seller_dashboard_router, prefix="/api")
app.include_router(seller_customers_router, prefix="/api")
app.include_router(seller_subscriptions_router, prefix="/api")
app.include_router(seller_modules_router, prefix="/api")
app.include_router(seller_licenses_router, prefix="/api")
app.include_router(license_check_router, prefix="/api")
app.include_router(seller_usage_router, prefix="/api")
app.include_router(seller_users_router, prefix="/api")
app.include_router(seller_audit_logs_router, prefix="/api")
app.include_router(seller_settings_router, prefix="/api")
app.include_router(seller_billing_router, prefix="/api")
app.include_router(seller_companies_router, prefix="/api")
app.include_router(pipeline_files_router, prefix="/api")
app.include_router(uploaded_files_router, prefix="/api")
app.include_router(pipeline_jobs_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(extraction_router, prefix="/api")
app.include_router(monitoring_router, prefix="/api")
app.include_router(lineage_router, prefix="/api")
app.include_router(settings_database_router, prefix="/api")
app.include_router(anomaly_router, prefix="/api")
app.include_router(access_router, prefix="/api")
app.include_router(billing_invoices_router, prefix="/api")
app.include_router(payment_gateways_router, prefix="/api")
app.include_router(notifications_router, prefix="/api")
app.include_router(billing_subscriptions_router, prefix="/api")
app.include_router(payments_webhook_router, prefix="/api")
app.include_router(payments_pay_router, prefix="/api")
app.include_router(invoices_router, prefix="/api")
app.include_router(invoice_mappings_router, prefix="/api")
app.include_router(invoice_rules_router, prefix="/api")
app.include_router(approvals_router, prefix="/api")
app.include_router(bulk_migration_router, prefix="/api")
app.include_router(tally_router, prefix="/api")

if _FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="frontend_assets")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": settings.app_name}


@app.get("/{full_path:path}")
async def serve_react_app(full_path: str):
    """Fall back to serving the React app for any unknown browser route."""
    if not _FRONTEND_DIST.exists():
        return {"status": "ok", "service": settings.app_name}

    candidate = (_FRONTEND_DIST / full_path).resolve()
    if candidate.is_file() and str(candidate).startswith(str(_FRONTEND_DIST)):
        return FileResponse(candidate)

    return FileResponse(_FRONTEND_DIST / "index.html")
