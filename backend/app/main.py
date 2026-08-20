"""
Data Intelligence Platform — FastAPI backend
Entry point: uvicorn app.main:app --reload --port 8000
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.api.loads import router as loads_router
from app.api.validation import router as validation_router
from app.api.reconciliation import router as reconciliation_router
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
from app.api.pipeline_files import router as pipeline_files_router
from app.api.pipeline_jobs import router as pipeline_jobs_router
from app.api.dashboard import router as dashboard_router
from app.api.extraction import router as extraction_router
from app.api.monitoring import router as monitoring_router
from app.api.lineage import router as lineage_router
from app.models.pipeline_source_file import PipelineSourceFile  # noqa: F401 — string FK to pipelines.id needs this imported before init_db()
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
from app.models.user_session import UserSession  # noqa: F401 — new table, needs registering before init_db()
from app.models.sso_configuration import SsoConfiguration  # noqa: F401 — new table, needs registering before init_db()

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initialising database …")
    await init_db()
    await _seed_superadmin()
    await _seed_seller_admin()
    await _seed_seller_data()
    await _seed_customer_modules()
    yield
    logger.info("Shutting down.")


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
app.include_router(loads_router, prefix="/api")
app.include_router(validation_router, prefix="/api")
app.include_router(reconciliation_router, prefix="/api")
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
app.include_router(pipeline_files_router, prefix="/api")
app.include_router(pipeline_jobs_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(extraction_router, prefix="/api")
app.include_router(monitoring_router, prefix="/api")
app.include_router(lineage_router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": settings.app_name}
