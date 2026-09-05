"""
SB33 — automated acceptance tests for the Multi-Subscription Billing &
Payments Platform, against a real (isolated) SQLite DB via the actual
service-layer functions — no mocking of the code under test. 16 cases,
grouped to match the build's own phase table: the core multi-subscription
access engine (1-5), invoicing (6-8), payments (9-11), the Custom
Gateway's webhook signature scheme (12), and regression coverage for the
three real bugs found live during SB28/SB30/SB32 (13-16).

Scope note: these exercise the service layer directly (access_service,
subscription_service, invoice_service, payment_service, payment_gateway_
service, CustomProvider) against real DB rows — not the full FastAPI HTTP
stack. The HTTP-layer behavior (auth, request/response shapes, the actual
webhook endpoint's signature-verification 401, real browser checkout) was
already live-verified end to end during SB7/SB10/SB16/SB21/SB31/SB32 via
curl and the browser; this suite exists to keep that business logic
covered by something that runs on every future change, not to redo that
same HTTP-layer verification a second way.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.billing.payment import Payment, PaymentStatus
from app.models.billing.payment_gateway import GatewayProvider
from app.models.billing.notification import Notification
from app.models.customer_module import CustomerModule
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole
from app.payment_gateways.custom_provider import CustomProvider
from app.services.billing import invoice_service, payment_gateway_service, payment_service
from app.services.billing.access_service import DenialReason, access_service
from app.services.seller import subscription_service


def _future(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _past(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ── TEST 1-5: the core multi-subscription access engine ────────────────

@pytest.mark.asyncio
async def test_01_single_active_subscription_grants_its_modules(db, make_org, make_subscription):
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=["OCR", "VALIDATION"], status=SubscriptionStatus.ACTIVE, end_date=_future(30))

    result = await access_service.has_module_access(db, org.id, "OCR")
    assert result.granted is True
    result_other = await access_service.has_module_access(db, org.id, "VALIDATION")
    assert result_other.granted is True


@pytest.mark.asyncio
async def test_02_expired_subscription_denies_its_modules(db, make_org, make_subscription):
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.EXPIRED, end_date=_past(5))

    result = await access_service.has_module_access(db, org.id, "OCR")
    assert result.granted is False
    # The org's only subscription is expired, so there is zero currently-
    # active subscription at all — NO_ACTIVE_SUBSCRIPTION, not
    # MODULE_EXPIRED (that reason is reserved for "you DO have an active
    # subscription, it just doesn't cover this module" — see test_03).
    assert result.reason == DenialReason.NO_ACTIVE_SUBSCRIPTION


@pytest.mark.asyncio
async def test_03_mixed_active_and_expired_subscriptions_are_independent(db, make_org, make_subscription):
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.ACTIVE, end_date=_future(30))
    await make_subscription(organization_id=org.id, modules=["RECONCILIATION"], status=SubscriptionStatus.EXPIRED, end_date=_past(3))

    ocr = await access_service.has_module_access(db, org.id, "OCR")
    recon = await access_service.has_module_access(db, org.id, "RECONCILIATION")
    assert ocr.granted is True
    assert recon.granted is False
    assert recon.reason == DenialReason.MODULE_EXPIRED


@pytest.mark.asyncio
async def test_04_overlapping_subscriptions_one_expiring_never_removes_a_module_the_other_still_covers(db, make_org, make_subscription):
    """The load-bearing requirement of the whole feature (spec section 4's
    Subscription A/B example): two DIFFERENT subscriptions both grant
    OCR. One expires. OCR must still be granted via the other."""
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.EXPIRED, end_date=_past(2))
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.ACTIVE, end_date=_future(60))

    result = await access_service.has_module_access(db, org.id, "OCR")
    assert result.granted is True

    active_modules = await access_service.get_active_modules(db, org.id)
    assert "OCR" in active_modules
    expired_modules = await access_service.get_expired_modules(db, org.id)
    assert "OCR" not in expired_modules  # not expired from the tenant's point of view


@pytest.mark.asyncio
async def test_05_all_subscriptions_expired_denies_tenant_access_but_zero_ever_falls_back_to_legacy_cache(db, make_org, make_subscription):
    all_expired_org = await make_org()
    await make_subscription(organization_id=all_expired_org.id, modules=["OCR"], status=SubscriptionStatus.EXPIRED, end_date=_past(10))
    assert await access_service.has_tenant_access(db, all_expired_org.id) is False

    legacy_org = await make_org()  # zero Subscription rows ever
    legacy_org.licensed_modules = ["OCR"]
    await db.commit()
    assert await access_service.has_tenant_access(db, legacy_org.id) is True


# ── TEST 6-8: invoicing ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_06_trial_subscription_creates_a_zero_dollar_no_payment_required_invoice(db, make_org, make_subscription, plan_with_modules):
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR"], price=0.0, trial_days=14)
    sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.TRIAL, end_date=_future(14), plan_id=plan.id, amount=0.0)

    invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="TRIAL", actor_id=None)

    assert invoice.status == InvoiceStatus.NO_PAYMENT_REQUIRED
    assert invoice.total == 0.0
    assert invoice.invoice_number.startswith(f"INV-{datetime.now(timezone.utc).year}-")


@pytest.mark.asyncio
async def test_07_paid_subscription_creates_a_payment_pending_invoice_with_correct_total(db, make_org, make_subscription, plan_with_modules):
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR", "VALIDATION"], price=200.0)
    sub = await make_subscription(organization_id=org.id, modules=["OCR", "VALIDATION"], status=SubscriptionStatus.ACTIVE, end_date=_future(30), plan_id=plan.id, amount=200.0)

    invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="CREATE", actor_id=None)

    assert invoice.status == InvoiceStatus.PAYMENT_PENDING
    assert invoice.total == 200.0  # 2 entitlements x 100 each, 0% default tax
    assert invoice.due_date is not None


@pytest.mark.asyncio
async def test_08_invoice_numbers_are_unique_and_sequential_within_a_year(db, make_org, make_subscription, plan_with_modules):
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR"], price=100.0)
    numbers = []
    for _ in range(5):
        sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.ACTIVE, end_date=_future(30), plan_id=plan.id, amount=100.0)
        invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="CREATE", actor_id=None)
        numbers.append(invoice.invoice_number)

    assert len(numbers) == len(set(numbers))  # all unique
    suffixes = sorted(int(n.rsplit("-", 1)[1]) for n in numbers)
    assert suffixes == list(range(suffixes[0], suffixes[0] + 5))  # strictly sequential


# ── TEST 9-11: payments ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_09_record_payment_is_idempotent_on_gateway_and_transaction_id(db, make_org, make_subscription, plan_with_modules):
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR"], price=50.0)
    sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.PAYMENT_PENDING, end_date=_future(30), plan_id=plan.id, amount=50.0)
    invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="CREATE", actor_id=None)

    p1, created1 = await payment_service.record_payment(
        db, gateway_id=1, gateway_transaction_id="txn-dup-1", invoice_id=invoice.id,
        amount=50.0, currency="USD", status="SUCCEEDED",
    )
    p2, created2 = await payment_service.record_payment(
        db, gateway_id=1, gateway_transaction_id="txn-dup-1", invoice_id=invoice.id,
        amount=50.0, currency="USD", status="SUCCEEDED",
    )

    assert created1 is True
    assert created2 is False
    assert p1.id == p2.id
    count = (await db.execute(select(Payment).where(Payment.gateway_transaction_id == "txn-dup-1"))).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_10_record_payment_rejects_amount_currency_mismatch_against_the_invoice(db, make_org, make_subscription, plan_with_modules):
    """Never trusts a claimed SUCCEEDED payment's amount/currency — must
    match the invoice's own total/currency or it's rejected outright."""
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR"], price=75.0)
    sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.PAYMENT_PENDING, end_date=_future(30), plan_id=plan.id, amount=75.0)
    invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="CREATE", actor_id=None)

    with pytest.raises(payment_service.PaymentServiceError):
        await payment_service.record_payment(
            db, gateway_id=1, gateway_transaction_id="txn-mismatch", invoice_id=invoice.id,
            amount=1.00, currency="USD", status="SUCCEEDED",  # claims $1 against a $75 invoice
        )


@pytest.mark.asyncio
async def test_11_successful_payment_marks_invoice_paid_reactivates_subscription_and_notifies(db, make_org, make_subscription, plan_with_modules):
    org = await make_org()
    plan = await plan_with_modules(modules=["OCR"], price=40.0)
    sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.PAYMENT_PENDING, end_date=_future(30), plan_id=plan.id, amount=40.0)
    invoice = await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="CREATE", actor_id=None)

    await payment_service.record_payment(
        db, gateway_id=1, gateway_transaction_id="txn-success-1", invoice_id=invoice.id,
        amount=40.0, currency="USD", status="SUCCEEDED",
    )

    await db.refresh(invoice)
    await db.refresh(sub)
    assert invoice.status == InvoiceStatus.PAID
    assert sub.status == SubscriptionStatus.ACTIVE

    notifications = (await db.execute(select(Notification).where(Notification.organization_id == org.id))).scalars().all()
    assert any(n.type == "PAYMENT_SUCCESSFUL" for n in notifications)


# ── TEST 12: Custom Gateway webhook signature scheme ────────────────────

def test_12_custom_gateway_webhook_signature_round_trips_and_rejects_tampering():
    provider = CustomProvider(credentials={"internal_secret": "test-signing-secret"}, webhook_secret=None, environment="TEST")
    payload = provider.build_webhook_payload(
        event_type="PAYMENT_SUCCESS", gateway_transaction_id="custom_abc", amount=10.0, currency="USD", invoice_id=1,
    )
    signature = provider.sign_payload(payload)

    assert provider.verify_webhook_signature(payload, signature) is True
    assert provider.verify_webhook_signature(payload, "0" * len(signature)) is False  # wrong signature
    assert provider.verify_webhook_signature(payload + b"tampered", signature) is False  # tampered payload
    assert provider.verify_webhook_signature(payload, None) is False  # missing header


# ── TEST 13-16: regression coverage for real bugs found live this session ─

@pytest.mark.asyncio
async def test_13_extending_an_expired_subscription_reactivates_its_status_not_just_its_end_date(db, make_org, make_subscription, seller_actor):
    """SB28 bug: extend_subscription() used to push end_date out without
    ever updating .status, so an EXPIRED subscription's CustomerModule
    rows stayed EXPIRED too even though the date moved — access stayed
    denied despite a visibly future end_date."""
    org = await make_org()
    sub = await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.EXPIRED, end_date=_past(2))

    await subscription_service.extend_subscription(db, sub, days=60, actor=seller_actor)

    await db.refresh(sub)
    assert sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING)
    end_date_aware = sub.end_date if sub.end_date.tzinfo else sub.end_date.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo on round-trip
    assert end_date_aware > datetime.now(timezone.utc)

    entitlements = (await db.execute(select(CustomerModule).where(CustomerModule.subscription_id == sub.id))).scalars().all()
    assert all(cm.status == "ACTIVE" for cm in entitlements)

    result = await access_service.has_module_access(db, org.id, "OCR")
    assert result.granted is True


@pytest.mark.asyncio
async def test_14_has_tenant_access_legacy_fallback_only_applies_with_zero_subscription_rows_ever(db, make_org, make_subscription):
    """SB30 bug: has_tenant_access() used to fall back to reading the
    stale Organization.licensed_modules cache whenever get_active_
    subscriptions() was empty, even for an org with real Subscription
    rows that are just currently all expired — silently granting access
    to a fully-lapsed tenant for up to an hour between expiry sweeps."""
    org = await make_org(name="Has real but expired subscriptions")
    org.licensed_modules = ["OCR"]  # stale cache, not yet swept
    await db.commit()
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.EXPIRED, end_date=_past(1))

    assert await access_service.has_tenant_access(db, org.id) is False  # must NOT fall back to the stale cache


@pytest.mark.asyncio
async def test_15_expiring_soon_uses_the_real_7_day_window_not_30(db, make_org, make_subscription):
    """SB30 bug: get_expiring_modules() defaulted to within_days=30,
    inconsistent with the actual 7-day warning window used everywhere
    else (expiry_job.py, extend_subscription())."""
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=["OCR"], status=SubscriptionStatus.ACTIVE, end_date=_future(20))  # inside the old 30-day window, outside the real 7-day one

    expiring = await access_service.get_expiring_modules(db, org.id)
    assert expiring == []

    await make_subscription(organization_id=org.id, modules=["VALIDATION"], status=SubscriptionStatus.ACTIVE, end_date=_future(5))
    expiring_soon = await access_service.get_expiring_modules(db, org.id)
    assert any(e["module"] == "VALIDATION" for e in expiring_soon)
    assert not any(e["module"] == "OCR" for e in expiring_soon)


@pytest.mark.asyncio
async def test_16_scrub_reference_redacts_secret_and_pii_hinting_fields_including_nested(db, seller, seller_actor):
    """SB32 bug: PaymentWebhookEvent.payload_reference was persisted
    unscrubbed. scrub_reference() must redact secret/PII-shaped keys at
    any nesting depth, and payment_gateway_service must never let a
    decrypted credential value escape its own module."""
    raw = {
        "event_id": "evt_1", "amount": 10.0, "currency": "USD",  # safe — kept as-is
        "customer": {"email": "real.customer@example.com", "card_last4": "4242"},  # nested — must be scrubbed
        "secret_key": "sk_live_should_never_appear",
    }
    scrubbed = payment_service.scrub_reference(raw)
    assert scrubbed["event_id"] == "evt_1"
    assert scrubbed["amount"] == 10.0
    assert scrubbed["secret_key"] == "••••••••"
    assert scrubbed["customer"]["email"] == "••••••••"
    assert "4242" not in str(scrubbed)

    gateway = await payment_gateway_service.get_or_create_gateway(db, GatewayProvider.CUSTOM, actor=seller_actor)
    await payment_gateway_service.update_credentials(
        db, gateway, environment="TEST", credentials={"internal_secret": "super-secret-value"}, webhook_secret=None, actor=seller_actor,
    )
    summary = await payment_gateway_service.get_gateway_summary(db, gateway)
    assert "super-secret-value" not in str(summary)
    assert "internal_secret" in summary["configured_credential_keys"]["TEST"]
