"""
Shared pytest fixtures for the Multi-Subscription Billing & Payments
Platform test suite (SB33). Real async SQLite DB, real ORM models, real
service-layer functions — no mocks of the code under test. The only
concession to "real" is which DB file: an isolated one, so running this
suite never touches the actual dev database (backend/dip.db) that the
rest of this project's live verification has been building up all
session.

IMPORTANT: the DATABASE_URL env var must be set BEFORE any `app.*` module
is imported anywhere in the process, because app.database.connection
creates its async engine at import time. This file is pytest's first
touchpoint, so it does that here, first, before any other import.
"""
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_dip.db")
os.environ.setdefault("SECRET_KEY", "test-secret-not-for-production")
os.environ.setdefault("APP_DEBUG", "false")

import pytest
import pytest_asyncio

from app.core.security import hash_password
from app.database.connection import PlatformBase, TenantBase, AsyncSessionLocal, engine
from app.models.customer_module import CustomerModule
from app.models.organization import Organization
from app.models.plan import Plan, BillingCycle
from app.models.seller import Seller
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole

# app.main imports every model module in the codebase (it has to, so that
# every table is registered on PlatformBase/TenantBase.metadata before its
# own init_db() call) — importing it here, for its side effects only, is
# what makes create_all() below resolve every cross-model ForeignKey
# (e.g. users.custom_role_id -> roles.id) instead of erroring on whichever
# referenced table this test file didn't happen to import directly.
import app.main  # noqa: F401


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _db_schema():
    """Create every table once for the whole test session against the
    isolated test DB file, then drop the file afterward."""
    db_path = os.environ["DATABASE_URL"].split("///")[-1]
    async with engine.begin() as conn:
        await conn.run_sync(PlatformBase.metadata.create_all)
        await conn.run_sync(TenantBase.metadata.create_all)
    yield
    await engine.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Function-scoped isolation: truncate every table before each test
    rather than sharing accumulated state across tests. Runs BEFORE the
    test (not after) so a failed test's leftover rows never contaminate
    the next one, and the final test's data stays inspectable if a run is
    paused mid-suite for debugging."""
    async with engine.begin() as conn:
        for table in reversed(PlatformBase.metadata.sorted_tables):
            await conn.execute(table.delete())
        for table in reversed(TenantBase.metadata.sorted_tables):
            await conn.execute(table.delete())
    yield


@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session
        await session.close()


def _slug() -> str:
    return uuid.uuid4().hex[:10]


@pytest_asyncio.fixture
async def seller(db):
    s = Seller(
        name="Test Seller Co", legal_name="Test Seller Co LLC",
        support_email="support@testseller.example", billing_email="billing@testseller.example",
        sales_email="sales@testseller.example", sales_contact_name="Test Sales Team",
        default_trial_days=14,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


@pytest_asyncio.fixture
async def seller_actor(db, seller):
    """A real SELLER-role User to pass as `actor` into every service call
    that records an audit event — matches how every seller endpoint's
    `Depends(get_current_user)` supplies a real User, not a raw id."""
    u = User(
        email=f"seller-{_slug()}@testseller.example", full_name="Test Seller Admin",
        hashed_password=hash_password("Test1234!"), role=UserRole.SELLER, seller_role="SELLER_ADMIN",
        is_active=True, is_verified=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def make_plan(seller_id: int, *, modules: list[str], price: float = 100.0, trial_days: int | None = None, code: str | None = None) -> Plan:
    return Plan(
        seller_id=seller_id, code=code or f"plan-{_slug()}", name="Test Plan",
        modules=modules, limits={}, price=price, currency="USD",
        billing_cycle=BillingCycle.MONTHLY, trial_days=trial_days, is_active=True,
    )


@pytest_asyncio.fixture
async def make_org(db):
    async def _make(*, name: str = "Test Org") -> Organization:
        org = Organization(name=name, slug=f"org-{_slug()}", is_active=True, licensed_modules=[])
        db.add(org)
        await db.commit()
        await db.refresh(org)
        return org
    return _make


_ENTITLEMENT_STATUS_FOR_SUB = {
    SubscriptionStatus.TRIAL: "ACTIVE", SubscriptionStatus.ACTIVE: "ACTIVE", SubscriptionStatus.EXPIRING: "ACTIVE",
    SubscriptionStatus.EXPIRED: "EXPIRED", SubscriptionStatus.CANCELLED: "EXPIRED", SubscriptionStatus.SUSPENDED: "SUSPENDED",
    SubscriptionStatus.TERMINATED: "REVOKED", SubscriptionStatus.PENDING: "SUSPENDED",
    SubscriptionStatus.PAST_DUE: "SUSPENDED", SubscriptionStatus.PAYMENT_PENDING: "SUSPENDED",
}


@pytest_asyncio.fixture
async def make_subscription(db, seller):
    """Builds a real Subscription + one CustomerModule row per module,
    tied together via subscription_id — the exact shape
    subscription_service._create_entitlements() produces, built directly
    here so tests can set up arbitrary status/end_date/module
    combinations without going through the full create_subscription()
    body-schema plumbing. This is fixture setup, not the code under
    test — access_service/extend_subscription/invoice_service are the
    real functions exercised against these rows."""
    async def _make(
        *, organization_id: int, modules: list[str], status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
        end_date=None, plan_id: int | None = None, amount: float = 100.0, entitlement_status: str | None = None,
    ) -> Subscription:
        sub = Subscription(
            seller_id=seller.id, organization_id=organization_id, plan_id=plan_id,
            status=status, end_date=end_date, amount=amount, currency="USD",
        )
        db.add(sub)
        await db.flush()
        ent_status = entitlement_status or _ENTITLEMENT_STATUS_FOR_SUB.get(status, "ACTIVE")
        for module in modules:
            db.add(CustomerModule(
                organization_id=organization_id, subscription_id=sub.id, module=module,
                enabled=True, status=ent_status, expiry_date=end_date,
            ))
        await db.commit()
        await db.refresh(sub)
        return sub
    return _make


@pytest_asyncio.fixture
async def plan_with_modules(db, seller):
    async def _make(*, modules: list[str], price: float = 100.0, trial_days: int | None = None) -> Plan:
        p = make_plan(seller.id, modules=modules, price=price, trial_days=trial_days)
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p
    return _make
