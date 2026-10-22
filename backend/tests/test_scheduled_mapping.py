"""
Regression tests for Scheduled Mapping (Document Intelligence: Dynamic
JSON Extraction spec, section 7 — "Default Mapping for Scheduled
Processing").

Covers: the three-tier precedence rule (explicit -> default -> no
mapping) in document_mapping_resolution_service.resolve_scheduled_mapping(),
the "never silently fall through to a different tier on failure" rule for
both the explicit and default branches, org-level default CRUD (set is an
upsert, never a duplicate row; clear removes it), save-time validation of
a schedule's mapping_id in schedule_service._validate_and_apply_document_
config(), apply_scheduled_mapping_if_configured()'s idempotency and
trigger/schedule_id/schedule_run_id snapshotting on DocumentMappedOutput,
document_mapping_service.retry_scheduled_mapping()'s reprocess-without-
duplicating behaviour, and document_mapping_resolution_service.
list_schedules_using_mapping()'s EXPLICIT/DEFAULT usage detection.

Exercises real production code throughout against the real test SQLite
DB — no mocks.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.document import Document, DocumentStatus
from app.models.document_schedule import DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.models.schedule import RunStatus, Schedule, ScheduleEnvironment, ScheduleFrequency, ScheduleRun, ScheduleStatus
from app.schemas.mapping import ObjectRef
from app.services import (
    document_mapping_resolution_service as resolution_svc,
)
from app.services import document_mapping_service, mapping_service
from app.services import schedule_service as sched_svc
from app.services.document_mapping_resolution_service import ScheduledMappingConfigError
from app.services.schedule_service import ScheduleValidationError


def _doc(organization_id: int, *, document_type: str = "INVOICE", status: DocumentStatus = DocumentStatus.COMPLETED) -> Document:
    return Document(
        organization_id=organization_id, filename="invoice.pdf", storage_path="/tmp/invoice.pdf",
        extraction_result={
            "document_type": document_type,
            "fields": [
                {"name": "invoice_number", "type": "string", "value": "INV-900"},
                {"name": "vendor_name", "type": "string", "value": "acme corp"},
            ],
        },
        status=status,
    )


async def _create_published_mapping(db, *, organization_id: int, document_type: str = "INVOICE", name: str = "Invoice -> Invoice Schema"):
    # A constant_value rule never references a source column, so this
    # helper works for any document_type (e.g. PURCHASE_ORDER doesn't
    # have an "invoice_number" source field the way INVOICE does).
    mapping = await mapping_service.create_mapping(
        db, organization_id=organization_id, name=name,
        source=ObjectRef(document_type=document_type), target=ObjectRef(target_format="INVOICE_SCHEMA"),
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": [], "constant_value": "INV-CONST"}],
        created_by=None,
    )
    version = await mapping_service.get_version(db, mapping.id, 1)
    await mapping_service.run_validate_version(db, organization_id=organization_id, version=version)
    await db.refresh(version)
    version = await mapping_service.publish_version(db, mapping=mapping, version=version, actor_id=None)
    await db.refresh(mapping)
    return mapping, version


async def _make_schedule(db, organization_id: int, *, mapping_id: int | None = None, document_type: str | None = "INVOICE") -> Schedule:
    schedule = Schedule(
        organization_id=organization_id, owner_id=None, name="Doc Schedule Mapping Test",
        pipeline_id="0", pipeline_name="Document Source: /tmp/x",
        environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
        retry_count=0, retry_interval_seconds=0,
    )
    db.add(schedule)
    await db.flush()
    config = DocumentScheduleConfig(
        schedule_id=schedule.id, source_connection_id=1, source_path="/tmp/x",
        file_pattern="*", recursive_scan=False, document_type=document_type, mapping_id=mapping_id,
    )
    db.add(config)
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def _make_run(db, schedule: Schedule, *, status: RunStatus = RunStatus.RUNNING) -> ScheduleRun:
    run = ScheduleRun(schedule_id=schedule.id, status=status, trigger="Manual")
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _make_registry_row(db, *, organization_id: int, schedule_id: int, run_id: int, document_id: int, file_name: str, status=DocumentSourceStatus.PROCESSED) -> DocumentSourceRegistry:
    row = DocumentSourceRegistry(
        organization_id=organization_id, schedule_id=schedule_id, source_connection_id=1, source_type="LOCAL_FILE",
        source_path="/tmp/x", file_name=file_name, content_hash="d" * 64,
        status=status, processing_run_id=run_id, document_id=document_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── resolve_scheduled_mapping() precedence (section 7.2) ────────────

@pytest.mark.asyncio
async def test_resolve_explicit_mapping_wins_over_default(db, make_org):
    org = await make_org()
    explicit_mapping, _ = await _create_published_mapping(db, organization_id=org.id, name="Explicit")
    default_mapping, _ = await _create_published_mapping(db, organization_id=org.id, name="Default")
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=default_mapping.id, actor_id=None)

    resolved = await resolution_svc.resolve_scheduled_mapping(
        db, organization_id=org.id, schedule_mapping_id=explicit_mapping.id, document_type="INVOICE",
    )
    assert resolved.mapping_id == explicit_mapping.id
    assert resolved.source == "EXPLICIT"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default_when_no_explicit_mapping(db, make_org):
    org = await make_org()
    default_mapping, _ = await _create_published_mapping(db, organization_id=org.id, name="Default")
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=default_mapping.id, actor_id=None)

    resolved = await resolution_svc.resolve_scheduled_mapping(
        db, organization_id=org.id, schedule_mapping_id=None, document_type="INVOICE",
    )
    assert resolved.mapping_id == default_mapping.id
    assert resolved.source == "DEFAULT"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_neither_explicit_nor_default(db, make_org):
    org = await make_org()
    resolved = await resolution_svc.resolve_scheduled_mapping(
        db, organization_id=org.id, schedule_mapping_id=None, document_type="INVOICE",
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_bad_explicit_mapping_raises_and_does_not_fall_through_to_default(db, make_org):
    """Section 7.2 — "do not silently choose an unrelated profile": a bad
    explicit mapping_id must raise, never silently fall through to a
    perfectly good org default."""
    org = await make_org()
    default_mapping, _ = await _create_published_mapping(db, organization_id=org.id, name="Default")
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=default_mapping.id, actor_id=None)

    with pytest.raises(ScheduledMappingConfigError):
        await resolution_svc.resolve_scheduled_mapping(
            db, organization_id=org.id, schedule_mapping_id=999999, document_type="INVOICE",
        )


@pytest.mark.asyncio
async def test_resolve_bad_default_also_raises(db, make_org):
    org = await make_org()
    unpublished = await mapping_service.create_mapping(
        db, organization_id=org.id, name="Unpublished",
        source=ObjectRef(document_type="INVOICE"), target=ObjectRef(target_format="INVOICE_SCHEMA"),
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
        created_by=None,
    )
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=unpublished.id, actor_id=None)

    with pytest.raises(ScheduledMappingConfigError, match="PUBLISHED"):
        await resolution_svc.resolve_scheduled_mapping(
            db, organization_id=org.id, schedule_mapping_id=None, document_type="INVOICE",
        )


@pytest.mark.asyncio
async def test_resolve_explicit_mapping_incompatible_document_type_raises(db, make_org):
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id, document_type="PURCHASE_ORDER", name="PO Mapping")
    with pytest.raises(ScheduledMappingConfigError, match="incompatible"):
        await resolution_svc.resolve_scheduled_mapping(
            db, organization_id=org.id, schedule_mapping_id=mapping.id, document_type="INVOICE",
        )


# ── Org default administration (section 7.3) ────────────────────────

@pytest.mark.asyncio
async def test_set_default_is_an_upsert_not_a_duplicate_row(db, make_org):
    org = await make_org()
    mapping_a, _ = await _create_published_mapping(db, organization_id=org.id, name="A")
    mapping_b, _ = await _create_published_mapping(db, organization_id=org.id, name="B")

    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=mapping_a.id, actor_id=None)
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=mapping_b.id, actor_id=None)

    rows = await resolution_svc.list_defaults(db, org.id)
    assert len(rows) == 1
    assert rows[0].mapping_id == mapping_b.id


@pytest.mark.asyncio
async def test_clear_default_removes_it(db, make_org):
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=mapping.id, actor_id=None)
    await resolution_svc.clear_default(db, organization_id=org.id, document_type="INVOICE", actor_id=None)
    assert await resolution_svc.get_default(db, org.id, "INVOICE") is None


@pytest.mark.asyncio
async def test_set_default_rejects_connection_sourced_mapping(db, make_org):
    org = await make_org()
    from app.models.column_mapping import ColumnMapping
    non_doc_mapping = ColumnMapping(
        organization_id=org.id, name="Connector mapping", source_connection_id=None, source_document_type=None,
        target_connection_id=None, target_format="INVOICE_SCHEMA", mappings=[], status="DRAFT",
        latest_version=1, current_published_version=None,
    )
    db.add(non_doc_mapping)
    await db.commit()
    await db.refresh(non_doc_mapping)

    with pytest.raises(resolution_svc.DefaultMappingError, match="document_type"):
        await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=non_doc_mapping.id, actor_id=None)


# ── Schedule save-time validation (schedule_service.py) ─────────────

@pytest.mark.asyncio
async def test_schedule_save_time_rejects_unresolvable_mapping_id(db, make_org):
    org = await make_org()
    schedule = Schedule(
        organization_id=org.id, owner_id=None, name="Validation Test Schedule",
        pipeline_id="0", pipeline_name="Document Source: /tmp/x",
        environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
    )
    db.add(schedule)
    await db.flush()

    class _FakeConfigIn:
        source_connection_id = 1
        source_kind = "FILE"
        archive_after_success = False
        archive_path = None
        invoice_auto_post = False
        invoice_erp_connection_id = None
        document_type = "INVOICE"
        mapping_id = 999999

        def model_dump(self):
            return {"source_connection_id": 1, "mapping_id": 999999, "document_type": "INVOICE"}

    with pytest.raises(ScheduleValidationError):
        await sched_svc._validate_and_apply_document_config(db, schedule, _FakeConfigIn())


# ── apply_scheduled_mapping_if_configured() — idempotency + snapshot ─

@pytest.mark.asyncio
async def test_apply_scheduled_mapping_idempotent_for_same_run(db, make_org):
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    schedule = await _make_schedule(db, org.id, mapping_id=mapping.id)
    run = await _make_run(db, schedule)
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    first = await document_mapping_service.apply_scheduled_mapping_if_configured(
        db, document=doc, schedule_id=schedule.id, schedule_run_id=run.id,
        schedule_mapping_id=mapping.id, document_type="INVOICE", organization_id=org.id,
    )
    assert first["status"] == "applied"
    assert first["mapping_status"] == "SUCCESS"

    second = await document_mapping_service.apply_scheduled_mapping_if_configured(
        db, document=doc, schedule_id=schedule.id, schedule_run_id=run.id,
        schedule_mapping_id=mapping.id, document_type="INVOICE", organization_id=org.id,
    )
    assert second["status"] == "already_applied"

    outputs = await document_mapping_service.list_mapped_outputs(db, doc.id, org.id)
    assert len(outputs) == 1  # no duplicate output from the second call


@pytest.mark.asyncio
async def test_apply_scheduled_mapping_config_error_produces_no_output_row(db, make_org):
    org = await make_org()
    schedule = await _make_schedule(db, org.id, mapping_id=999999)
    run = await _make_run(db, schedule)
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    result = await document_mapping_service.apply_scheduled_mapping_if_configured(
        db, document=doc, schedule_id=schedule.id, schedule_run_id=run.id,
        schedule_mapping_id=999999, document_type="INVOICE", organization_id=org.id,
    )
    assert result["status"] == "config_error"
    assert "error" in result
    assert await document_mapping_service.list_mapped_outputs(db, doc.id, org.id) == []


@pytest.mark.asyncio
async def test_apply_scheduled_mapping_no_mapping_configured(db, make_org):
    org = await make_org()
    schedule = await _make_schedule(db, org.id, mapping_id=None, document_type=None)
    run = await _make_run(db, schedule)
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    result = await document_mapping_service.apply_scheduled_mapping_if_configured(
        db, document=doc, schedule_id=schedule.id, schedule_run_id=run.id,
        schedule_mapping_id=None, document_type=None, organization_id=org.id,
    )
    assert result == {"status": "no_mapping_configured"}


@pytest.mark.asyncio
async def test_scheduled_output_records_trigger_and_schedule_ids(db, make_org):
    """Section 7.4 — every scheduled execution records mapping profile
    ID/version/schedule ID/run ID; a manual apply leaves them unset with
    trigger=MANUAL."""
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    schedule = await _make_schedule(db, org.id, mapping_id=mapping.id)
    run = await _make_run(db, schedule)
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await document_mapping_service.apply_scheduled_mapping_if_configured(
        db, document=doc, schedule_id=schedule.id, schedule_run_id=run.id,
        schedule_mapping_id=mapping.id, document_type="INVOICE", organization_id=org.id,
    )
    scheduled_output = (await document_mapping_service.list_mapped_outputs(db, doc.id, org.id))[0]
    assert scheduled_output.schedule_id == schedule.id
    assert scheduled_output.schedule_run_id == run.id
    assert scheduled_output.trigger == "SCHEDULED"

    doc2 = _doc(org.id)
    db.add(doc2)
    await db.commit()
    await db.refresh(doc2)
    manual_output = await document_mapping_service.apply_mapping_profile_to_document(
        db, document=doc2, mapping_id=mapping.id, organization_id=org.id, created_by=None,
    )
    assert manual_output.schedule_id is None
    assert manual_output.schedule_run_id is None
    assert manual_output.trigger == "MANUAL"


# ── retry_scheduled_mapping() (section 7.5) ─────────────────────────

@pytest.mark.asyncio
async def test_retry_scheduled_mapping_applies_after_config_fix_without_duplicating(db, make_org):
    org = await make_org()
    schedule = await _make_schedule(db, org.id, mapping_id=999999)  # bad config at run time
    run = await _make_run(db, schedule, status=RunStatus.SUCCESS)
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    await _make_registry_row(db, organization_id=org.id, schedule_id=schedule.id, run_id=run.id, document_id=doc.id, file_name="a.pdf")

    # First retry attempt: config is still bad -> config_error, no output.
    counts = await document_mapping_service.retry_scheduled_mapping(db, schedule_id=schedule.id, organization_id=org.id)
    assert counts["candidates"] == 1
    assert counts["config_error"] == 1
    assert counts["applied_success"] == 0

    # Admin fixes the config.
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    config = (await db.execute(
        select(DocumentScheduleConfig).where(DocumentScheduleConfig.schedule_id == schedule.id)
    )).scalar_one()
    config.mapping_id = mapping.id
    await db.commit()

    # Second retry: now resolves and applies.
    counts2 = await document_mapping_service.retry_scheduled_mapping(db, schedule_id=schedule.id, organization_id=org.id)
    assert counts2["applied_success"] == 1
    assert counts2["config_error"] == 0

    # Third retry: idempotent — already SUCCESS for this exact run, no new output.
    counts3 = await document_mapping_service.retry_scheduled_mapping(db, schedule_id=schedule.id, organization_id=org.id)
    assert counts3["already_applied"] == 1
    assert counts3["applied_success"] == 0

    outputs = await document_mapping_service.list_mapped_outputs(db, doc.id, org.id)
    assert len(outputs) == 1  # never duplicated across the three retry calls


@pytest.mark.asyncio
async def test_retry_scheduled_mapping_requires_a_document_schedule(db, make_org):
    org = await make_org()
    schedule = Schedule(
        organization_id=org.id, owner_id=None, name="Not a doc schedule",
        pipeline_id="123", pipeline_name="Some pipeline",
        environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)

    with pytest.raises(document_mapping_service.DocumentMappingError):
        await document_mapping_service.retry_scheduled_mapping(db, schedule_id=schedule.id, organization_id=org.id)


# ── list_schedules_using_mapping() (section 7.3) ────────────────────

@pytest.mark.asyncio
async def test_list_schedules_using_mapping_distinguishes_explicit_and_default(db, make_org):
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    explicit_schedule = await _make_schedule(db, org.id, mapping_id=mapping.id)
    default_schedule = await _make_schedule(db, org.id, mapping_id=None)
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=mapping.id, actor_id=None)

    usages = await resolution_svc.list_schedules_using_mapping(db, mapping_id=mapping.id, organization_id=org.id)
    by_schedule = {u["schedule_id"]: u["usage"] for u in usages}
    assert by_schedule[explicit_schedule.id] == "EXPLICIT"
    assert by_schedule[default_schedule.id] == "DEFAULT"


@pytest.mark.asyncio
async def test_list_schedules_using_mapping_explicit_override_is_not_also_counted_as_default(db, make_org):
    """A schedule with its OWN explicit mapping_id must never show up as
    using the org default too, even if the default happens to point at
    the exact same mapping — it's using the explicit tier, period."""
    org = await make_org()
    mapping, _ = await _create_published_mapping(db, organization_id=org.id)
    schedule = await _make_schedule(db, org.id, mapping_id=mapping.id)
    await resolution_svc.set_default(db, organization_id=org.id, document_type="INVOICE", mapping_id=mapping.id, actor_id=None)

    usages = await resolution_svc.list_schedules_using_mapping(db, mapping_id=mapping.id, organization_id=org.id)
    matches = [u for u in usages if u["schedule_id"] == schedule.id]
    assert len(matches) == 1
    assert matches[0]["usage"] == "EXPLICIT"
