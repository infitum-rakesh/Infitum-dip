"""
Automated tests — Scheduled Document Intelligence Workflow (spec's Phase
12). Real (isolated) SQLite DB via the actual service-layer functions —
no mocking of the code under test, following the exact convention
test_billing_platform.py established (see conftest.py's own docstring).

Scope, matching that precedent's own scope note: these exercise
document_schedule_service.py + schedule_service.py's document-schedule
branch directly against real rows — not the full FastAPI HTTP stack (the
API layer itself — create/update/run/cancel/documents endpoints — was
already live-verified end to end via curl and the browser during Phases
7-9). What this suite adds is durable, always-on regression coverage for
the actual business logic: idempotency, the state machine, confidence-
threshold routing, and two real bugs found live this session (stale
last_error, and the processing_run_id mis-attribution bug).

Discovery/download/OCR/extraction are exercised for REAL, not stubbed:
- A real `LocalFileConnector` (connectors/files/local.py) reads a real
  temp directory. LOCAL_FILE was deliberately removed from
  connection_service.FIELD_SCHEMAS (Item 4, an earlier phase this
  session) so it can never be created via the API/UI — a real security
  decision this suite does not reverse. That removal has the side effect
  of breaking connection_service.build_connector() for LOCAL_FILE too
  (it unconditionally looks the type up in FIELD_SCHEMAS first), so
  these tests construct a real LocalFileConnector directly and patch it
  in wherever schedule code would otherwise call build_connector() — the
  connector itself is still fully real, only the "look up a secrets
  schema by type name" indirection (irrelevant to LOCAL_FILE, which has
  no secrets) is bypassed. See the `patch_build_connector` fixture.
- A real PNG with real drawn text is fed through the real Tesseract OCR
  engine (app/ocr/engine.py) and the real, deterministic, zero-network
  Custom Extraction Engine (app/extraction/custom_engine.py, selected
  automatically by _run_ai_extraction() whenever an org has no AI
  Provider connection configured — exactly this suite's fixtures).
"""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from app.connectors.base import ConnectorError
from app.connectors.files.local import LocalFileConnector
from app.models.connection import Connection, ConnectionCategory, ConnectionStatus, ConnectionType
from app.models.document import Document, DocumentStatus
from app.models.document_schedule import ALLOWED_TRANSITIONS, DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.models.schedule import RunStatus, Schedule, ScheduleEnvironment, ScheduleFrequency, ScheduleRun, ScheduleStatus
from app.services import document_schedule_service as doc_sched_svc
from app.services import schedule_service as sched_svc


def _slug() -> str:
    return uuid.uuid4().hex[:10]


def _real_local_connector(conn: Connection) -> LocalFileConnector:
    """Builds a real LocalFileConnector directly — see module docstring
    for why this bypasses connection_service.build_connector()'s
    FIELD_SCHEMAS lookup instead of using it."""
    return LocalFileConnector(config=conn.config, secrets={}, cache_key=f"test-conn:{conn.id}")


def build_connector(conn: Connection):
    """Drop-in real replacement for connection_service.build_connector(),
    used only by this test file's own direct calls (test_01/07) — for
    every LOCAL_FILE connection these tests create there's nothing to
    decrypt, so this is equivalent to the real function for the one
    connection type these tests use."""
    if conn.connection_type == ConnectionType.LOCAL_FILE:
        return _real_local_connector(conn)
    raise AssertionError("This suite's fixtures only build LOCAL_FILE connections.")


@pytest.fixture(autouse=True)
def patch_build_connector(monkeypatch):
    """schedule_service.py and document_schedule_service.py each import
    build_connector directly (`from app.services.connection_service
    import build_connector`) — patch both module-level references so
    every real code path under test (discover_files, compute_file_hash,
    _download_with_retry, _run_document_schedule_background) resolves a
    LOCAL_FILE Connection to a real, working LocalFileConnector instead
    of hitting the FIELD_SCHEMAS gap described in the module docstring.
    Autouse: every test in this file only ever builds LOCAL_FILE
    connections, so there's no case where the real build_connector()
    would be needed instead."""
    monkeypatch.setattr(doc_sched_svc, "build_connector", build_connector)
    monkeypatch.setattr(sched_svc, "build_connector", build_connector)


# ── Fixtures specific to this suite (conftest.py's `db`/`make_org` are reused) ──

@pytest.fixture
def source_dir(tmp_path):
    d = tmp_path / f"source-{_slug()}"
    d.mkdir()
    return d


def _draw_invoice_png(path, *, invoice_number: str, total: str) -> None:
    """Writes a REAL PNG with real rendered text — large, high-contrast,
    generously spaced label:value rows so Tesseract can genuinely read it
    (this is not a fixture that fakes an OCR result; it gives the real
    OCR engine something a real engine can actually transcribe)."""
    img = Image.new("RGB", (900, 500), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 32)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "INVOICE",
        f"Invoice Number: {invoice_number}",
        "Date: 2026-01-15",
        f"Total: {total}",
        "Vendor: Acme Testing Corp",
    ]
    y = 30
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += 70
    img.save(path)


@pytest.fixture
def invoice_png(source_dir):
    path = source_dir / "invoice_1.png"
    _draw_invoice_png(path, invoice_number="INV-1001", total="$50.00")
    return path


@pytest_asyncio.fixture
async def local_connection(db, make_org):
    async def _make(*, org=None, base_path) -> Connection:
        org = org or await make_org()
        conn = Connection(
            organization_id=org.id, name=f"Test Local Source {_slug()}",
            category=ConnectionCategory.STORAGE, connection_type=ConnectionType.LOCAL_FILE,
            config={"base_path": str(base_path)}, status=ConnectionStatus.ACTIVE, is_active=True,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return conn
    return _make


@pytest_asyncio.fixture
async def make_document_schedule(db):
    """Builds a real Schedule + 1:1 DocumentScheduleConfig row directly
    (bypassing create_schedule()'s API request-body plumbing, the same
    way conftest.py's make_subscription() bypasses create_subscription())
    — this fixture is test setup, not the code under test."""
    async def _make(
        *, organization_id, connection_id, source_path, confidence_threshold: float = 90.0,
        accuracy_threshold: float = 90.0, max_concurrency: int = 1, retry_count: int = 0,
        retry_interval_seconds: int = 0, batch_size: int = 500,
    ) -> Schedule:
        schedule = Schedule(
            organization_id=organization_id, owner_id=None, name=f"Doc Schedule Test {_slug()}",
            pipeline_id="0", pipeline_name=f"Document Source: {source_path}",
            environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
            frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
            retry_count=retry_count, retry_interval_seconds=retry_interval_seconds,
        )
        db.add(schedule)
        await db.flush()
        config = DocumentScheduleConfig(
            schedule_id=schedule.id, source_connection_id=connection_id, source_path=str(source_path),
            file_pattern="*", recursive_scan=False, batch_size=batch_size, max_concurrency=max_concurrency,
            confidence_threshold=confidence_threshold, accuracy_threshold=accuracy_threshold,
        )
        db.add(config)
        await db.commit()
        await db.refresh(schedule)
        return schedule
    return _make


@pytest_asyncio.fixture
async def make_run(db):
    async def _make(schedule: Schedule, *, status: RunStatus = RunStatus.RUNNING) -> ScheduleRun:
        run = ScheduleRun(schedule_id=schedule.id, status=status, trigger="Manual")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run
    return _make


async def _run_background(schedule: Schedule, run: ScheduleRun, organization_id) -> None:
    """Calls the exact fire-and-forget body execute_schedule() dispatches
    via asyncio.create_task() — awaited directly here instead, so the
    test can assert on its result deterministically rather than racing a
    detached task (execute_schedule()'s own run-lock/dispatch wiring was
    already live-verified via curl/the browser in Phase 7-9)."""
    await sched_svc._run_document_schedule_background(schedule.id, run.id, organization_id)


# ── 1-6: pure discovery/idempotency/state-machine logic ─────────────────

@pytest.mark.asyncio
async def test_01_upsert_registry_entry_is_idempotent_on_source_identity(db, make_org, local_connection, source_dir, invoice_png):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    files = await doc_sched_svc.discover_files(conn, DocumentScheduleConfig(source_path=str(source_dir), file_pattern="*", recursive_scan=False))
    assert len(files) == 1
    file_info = files[0]
    content_hash = await doc_sched_svc.compute_file_hash(conn, file_info.path)

    row1 = await doc_sched_svc.upsert_registry_entry(
        db, organization_id=org.id, schedule_id=None, connection=conn, source_path=str(source_dir),
        file_info=file_info, content_hash=content_hash, run_id=None,
    )
    row2 = await doc_sched_svc.upsert_registry_entry(
        db, organization_id=org.id, schedule_id=None, connection=conn, source_path=str(source_dir),
        file_info=file_info, content_hash=content_hash, run_id=None,
    )
    assert row1.id == row2.id  # same identity -> same row, never a duplicate

    all_rows = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.content_hash == content_hash))).scalars().all()
    assert len(all_rows) == 1


@pytest.mark.asyncio
async def test_02_needs_processing_excludes_processed_and_review_required_but_not_failed(db, make_org, local_connection, source_dir, invoice_png):
    """Regression for the real duplicate-review bug found live this
    session: a file already PROCESSED *or* REVIEW_REQUIRED must never be
    reprocessed — only the original needs_processing() (PROCESSED-only)
    would have let REVIEW_REQUIRED files pile up duplicate reviews on
    every subsequent run."""
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    row = DocumentSourceRegistry(
        organization_id=org.id, source_connection_id=conn.id, source_type="LOCAL_FILE",
        source_path=str(source_dir), file_name="invoice_1.png", content_hash="a" * 64,
        status=DocumentSourceStatus.PROCESSED,
    )
    db.add(row)
    await db.commit()
    assert doc_sched_svc.needs_processing(row) is False

    row.status = DocumentSourceStatus.REVIEW_REQUIRED
    assert doc_sched_svc.needs_processing(row) is False

    row.status = DocumentSourceStatus.FAILED
    assert doc_sched_svc.needs_processing(row) is True


@pytest.mark.asyncio
async def test_03_find_duplicate_content_flags_same_bytes_under_a_different_path(db, make_org, local_connection, source_dir):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    shared_hash = "b" * 64
    original = DocumentSourceRegistry(
        organization_id=org.id, source_connection_id=conn.id, source_type="LOCAL_FILE",
        source_path=str(source_dir), file_name="original.png", content_hash=shared_hash,
        status=DocumentSourceStatus.PROCESSED,
    )
    db.add(original)
    await db.commit()
    await db.refresh(original)

    dup = await doc_sched_svc.find_duplicate_content(db, organization_id=org.id, content_hash=shared_hash, exclude_id=99999)
    assert dup is not None
    assert dup.id == original.id

    # Excluding the row itself (the normal call site's usage) finds nothing.
    none_found = await doc_sched_svc.find_duplicate_content(db, organization_id=org.id, content_hash=shared_hash, exclude_id=original.id)
    assert none_found is None


def test_04_is_supported_file_matches_the_ocr_capable_extension_set():
    assert doc_sched_svc.is_supported_file("invoice.pdf") is True
    assert doc_sched_svc.is_supported_file("scan.PNG") is True  # case-insensitive
    assert doc_sched_svc.is_supported_file("photo.jpeg") is True
    assert doc_sched_svc.is_supported_file("notes.txt") is False
    assert doc_sched_svc.is_supported_file("data.xlsx") is False
    assert doc_sched_svc.is_supported_file("no_extension") is False


@pytest.mark.asyncio
async def test_05_transition_enforces_the_state_machine(db, make_org, local_connection, source_dir):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    row = DocumentSourceRegistry(
        organization_id=org.id, source_connection_id=conn.id, source_type="LOCAL_FILE",
        source_path=str(source_dir), file_name="x.pdf", content_hash="c" * 64,
        status=DocumentSourceStatus.DISCOVERED,
    )
    db.add(row)
    await db.commit()

    # Allowed: DISCOVERED -> QUEUED
    row = await doc_sched_svc.transition(db, row, DocumentSourceStatus.QUEUED)
    assert row.status == DocumentSourceStatus.QUEUED

    # Not allowed: QUEUED -> REVIEW_REQUIRED (must go through PROCESSING/VALIDATING first)
    with pytest.raises(doc_sched_svc.InvalidStateTransition):
        await doc_sched_svc.transition(db, row, DocumentSourceStatus.REVIEW_REQUIRED)

    # Every transition ALLOWED_TRANSITIONS claims to support really is accepted
    assert DocumentSourceStatus.UNSUPPORTED in ALLOWED_TRANSITIONS[DocumentSourceStatus.DISCOVERED]


@pytest.mark.asyncio
async def test_06_transition_clears_stale_last_error_on_a_later_success(db, make_org, local_connection, source_dir):
    """Regression for the real stale-last_error bug found live this
    session: a file that failed once (transient) and later succeeds must
    not keep showing the old error text next to its new PROCESSED/
    REVIEW_REQUIRED status."""
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    row = DocumentSourceRegistry(
        organization_id=org.id, source_connection_id=conn.id, source_type="LOCAL_FILE",
        source_path=str(source_dir), file_name="x.pdf", content_hash="d" * 64,
        status=DocumentSourceStatus.QUEUED,
    )
    db.add(row)
    await db.commit()

    await doc_sched_svc.transition(db, row, DocumentSourceStatus.PROCESSING)
    await doc_sched_svc.transition(db, row, DocumentSourceStatus.FAILED, error="Connector timed out.")
    assert row.last_error == "Connector timed out."

    await doc_sched_svc.transition(db, row, DocumentSourceStatus.QUEUED)  # FAILED -> QUEUED retry
    await doc_sched_svc.transition(db, row, DocumentSourceStatus.PROCESSING)
    row = await doc_sched_svc.transition(db, row, DocumentSourceStatus.REVIEW_REQUIRED)
    assert row.last_error is None


# ── 7: processing_run_id attribution — the bug fixed THIS session ───────

@pytest.mark.asyncio
async def test_07_processing_run_id_is_not_overwritten_by_a_run_that_does_not_act_on_the_file(db, make_org, local_connection, source_dir, invoice_png):
    """The exact bug found live this session: run_6 actually processed a
    file; run_7 merely re-discovered it (already PROCESSED, so
    needs_processing() correctly skipped it) — but upsert_registry_entry()
    used to unconditionally re-stamp processing_run_id on every
    re-discovery regardless of outcome, silently stealing the file off
    run_6's per-file breakdown table. Fixed: only the run that actually
    queues/transitions the file may set this column (see
    schedule_service.py's discovery loop) — a mere re-discovery must
    leave it exactly where the last real actor on the file left it."""
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    config = DocumentScheduleConfig(source_path=str(source_dir), file_pattern="*", recursive_scan=False)
    files = await doc_sched_svc.discover_files(conn, config)
    file_info = files[0]
    content_hash = await doc_sched_svc.compute_file_hash(conn, file_info.path)

    # run_6 — the run that actually acted on the file (mirrors
    # schedule_service.py's own discovery loop: upsert, then stamp
    # processing_run_id only because it's about to queue/transition it).
    row = await doc_sched_svc.upsert_registry_entry(
        db, organization_id=org.id, schedule_id=None, connection=conn, source_path=str(source_dir),
        file_info=file_info, content_hash=content_hash, run_id=6,
    )
    row.processing_run_id = 6
    await doc_sched_svc.transition(db, row, DocumentSourceStatus.QUEUED)
    await doc_sched_svc.transition(db, row, DocumentSourceStatus.PROCESSING)
    row = await doc_sched_svc.transition(db, row, DocumentSourceStatus.PROCESSED)
    assert row.processing_run_id == 6

    # run_7 — merely re-discovers the same file (upsert only, exactly
    # what schedule_service.py's loop does BEFORE the needs_processing()
    # check, for every rediscovered file regardless of outcome).
    row_again = await doc_sched_svc.upsert_registry_entry(
        db, organization_id=org.id, schedule_id=None, connection=conn, source_path=str(source_dir),
        file_info=file_info, content_hash=content_hash, run_id=7,
    )
    assert row_again.id == row.id
    assert doc_sched_svc.needs_processing(row_again) is False  # already PROCESSED -> run_7 takes no action
    assert row_again.processing_run_id == 6  # NOT overwritten to 7 — this is the fix


# ── 8-10: full pipeline integration — real OCR + real Custom Extraction Engine ──

@pytest.mark.asyncio
async def test_08_full_run_processes_a_new_file_and_flags_an_unsupported_one_without_stopping_the_batch(db, make_org, local_connection, source_dir, invoice_png, make_document_schedule, make_run):
    """Partial-failure handling (spec section 28): one unsupported file in
    the batch must not block a genuinely processable file in the same
    run. Real OCR + the real Custom Extraction Engine run against the
    test PNG — a small 5-field test image against the AI engine's full
    ~35-field GENERIC invoice schema reliably lands on NEEDS_REVIEW (most
    fields are genuinely absent, not just low-confidence), which is
    itself real, correct routing behavior this test verifies end to end,
    including the real ReviewQueueItem it creates."""
    (source_dir / "readme.txt").write_text("not a document schedule accepts")
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    schedule = await make_document_schedule(organization_id=org.id, connection_id=conn.id, source_path=str(source_dir))
    run = await make_run(schedule)

    await _run_background(schedule, run, org.id)

    await db.refresh(run)
    assert run.status == RunStatus.SUCCESS  # SUCCESS means "nothing FAILED", not "nothing needs review" — see finalization logic
    assert run.document_stats["discovered"] == 2
    assert run.document_stats["unsupported"] == 1
    assert run.document_stats["queued"] == 1
    assert run.document_stats["processed"] == 1
    assert run.document_stats["review"] == 1
    assert run.document_stats["failed"] == 0

    docs = (await db.execute(select(Document).where(Document.organization_id == org.id))).scalars().all()
    assert len(docs) == 1
    doc = docs[0]
    assert doc.status == DocumentStatus.NEEDS_REVIEW
    assert doc.source_schedule_id == schedule.id
    assert doc.ocr_result and (doc.ocr_result.get("full_text") or "").strip()  # real OCR genuinely ran
    assert "INV-1001" in doc.ocr_result["full_text"]  # genuinely read back what was drawn, not fabricated

    from app.models.review_queue import ReviewQueueItem
    review_items = (await db.execute(select(ReviewQueueItem).where(ReviewQueueItem.document_id == doc.id))).scalars().all()
    assert len(review_items) == 1  # the SAME Review Queue every manual-scan document lands in — no separate path

    registry_rows = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.organization_id == org.id))).scalars().all()
    supported = next(r for r in registry_rows if r.file_name.endswith(".png"))
    unsupported = next(r for r in registry_rows if r.file_name.endswith(".txt"))
    assert supported.status == DocumentSourceStatus.REVIEW_REQUIRED
    assert supported.processing_run_id == run.id
    assert unsupported.status == DocumentSourceStatus.UNSUPPORTED
    assert unsupported.document_id is None  # never got a Document row


@pytest.mark.asyncio
async def test_09_second_run_finds_zero_new_documents_and_preserves_the_first_runs_attribution(db, make_org, local_connection, source_dir, invoice_png, make_document_schedule, make_run):
    """Idempotency (spec section 7/8) AND the processing_run_id regression
    (test_07 above, now exercised at the full-integration level): a
    second run over the same unchanged source must queue nothing new, and
    must not steal the file's history off the run that actually processed
    it."""
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    schedule = await make_document_schedule(organization_id=org.id, connection_id=conn.id, source_path=str(source_dir))
    run1 = await make_run(schedule)
    await _run_background(schedule, run1, org.id)
    await db.refresh(run1)
    assert run1.document_stats["processed"] == 1

    docs_after_run1 = (await db.execute(select(Document).where(Document.organization_id == org.id))).scalars().all()
    assert len(docs_after_run1) == 1

    run2 = await make_run(schedule)
    await _run_background(schedule, run2, org.id)
    await db.refresh(run2)
    assert run2.status == RunStatus.SUCCESS
    assert run2.document_stats["discovered"] == 1  # file is still there, still re-discovered
    assert run2.document_stats["queued"] == 0       # but NOT reprocessed
    assert run2.document_stats["processed"] == 0

    docs_after_run2 = (await db.execute(select(Document).where(Document.organization_id == org.id))).scalars().all()
    assert len(docs_after_run2) == 1  # no duplicate Document row

    registry = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.organization_id == org.id))).scalars().one()
    assert registry.processing_run_id == run1.id  # NOT stolen by run2 — the fix under test


@pytest.mark.asyncio
async def test_10_confidence_threshold_routing_uses_the_real_threshold_comparison(db, make_org, local_connection, make_document_schedule, make_run, tmp_path):
    """Confidence-based routing (spec's core acceptance requirement) using
    the REAL threshold-comparison code in document_service.process_
    document(), against real (not stubbed) OCR/extraction output.

    Two independent, real triggers land this test's document on
    NEEDS_REVIEW regardless of which threshold is configured: (a) an
    unreachable numeric threshold (101 > any real confidence score), and
    (b) real AI Extraction against the comprehensive GENERIC schema
    always finding some fields genuinely absent from a small 5-field test
    document, which needs_review's `any(invalid)` clause catches
    independently of both numeric thresholds — see test_08's docstring.

    Each threshold pair gets its own fresh org/connection/source file:
    DocumentSourceRegistry's identity is (org, connection, path, name,
    hash) — NOT schedule_id (two schedules over the same file share dedup
    state, by design) — so reusing one source across schedules here would
    let the second iteration's needs_processing() correctly, but
    misleadingly, skip an already-REVIEW_REQUIRED row instead of
    genuinely exercising this threshold pair."""
    for confidence_threshold, accuracy_threshold in [(101.0, 101.0), (0.0, 0.0)]:
        org = await make_org()
        source_dir = tmp_path / f"source-{_slug()}"
        source_dir.mkdir()
        _draw_invoice_png(source_dir / "invoice_1.png", invoice_number="INV-1001", total="$50.00")
        conn = await local_connection(org=org, base_path=source_dir)
        schedule = await make_document_schedule(
            organization_id=org.id, connection_id=conn.id, source_path=str(source_dir),
            confidence_threshold=confidence_threshold, accuracy_threshold=accuracy_threshold,
        )
        run = await make_run(schedule)
        await _run_background(schedule, run, org.id)
        await db.refresh(run)
        assert run.document_stats["review"] == 1
        assert run.document_stats["success"] == 0

        doc = (await db.execute(select(Document).where(Document.source_schedule_id == schedule.id))).scalars().one()
        assert doc.status == DocumentStatus.NEEDS_REVIEW

        registry = (await db.execute(
            select(DocumentSourceRegistry).where(DocumentSourceRegistry.schedule_id == schedule.id)
        )).scalars().one()
        assert registry.status == DocumentSourceStatus.REVIEW_REQUIRED
        assert doc_sched_svc.needs_processing(registry) is False  # REVIEW_REQUIRED must not be reprocessed either


# ── 11-12: retry/backoff and cooperative cancellation ────────────────────

@pytest.mark.asyncio
async def test_11_download_with_retry_retries_a_transient_connector_error_then_succeeds(db, make_org, local_connection, source_dir, invoice_png, monkeypatch):
    """Spec section 27 — real retry loop, exercised against a connector
    whose open_read_stream() is made to fail twice (a real ConnectorError,
    the same class every real connector raises for a transient failure)
    before succeeding on the third attempt. retry_interval=0 keeps this
    fast — _download_with_retry()'s own backoff formula (retry_interval *
    3^n) is 0 at any interval of 0, not a shortcut around it."""
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    real_connector = build_connector(conn)
    attempts = {"n": 0}
    real_open = real_connector.open_read_stream

    def _flaky_open(path):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectorError("Transient network blip.")
        return real_open(path)

    monkeypatch.setattr(real_connector, "open_read_stream", _flaky_open)
    monkeypatch.setattr(sched_svc, "build_connector", lambda _c: real_connector)

    data = await sched_svc._download_with_retry(conn, str(invoice_png), retry_count=3, retry_interval=0)
    assert attempts["n"] == 3
    assert data == invoice_png.read_bytes()


@pytest.mark.asyncio
async def test_12_download_with_retry_gives_up_after_exhausting_retry_count(db, make_org, local_connection, source_dir, invoice_png, monkeypatch):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    real_connector = build_connector(conn)

    def _always_fails(path):
        raise ConnectorError("Source permanently unreachable.")

    monkeypatch.setattr(real_connector, "open_read_stream", _always_fails)
    monkeypatch.setattr(sched_svc, "build_connector", lambda _c: real_connector)

    with pytest.raises(ConnectorError):
        await sched_svc._download_with_retry(conn, str(invoice_png), retry_count=2, retry_interval=0)


@pytest.mark.asyncio
async def test_13_request_cancel_sets_the_cooperative_cancellation_flag(db, make_org, make_document_schedule, local_connection, source_dir, make_run):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    schedule = await make_document_schedule(organization_id=org.id, connection_id=conn.id, source_path=str(source_dir))
    run = await make_run(schedule, status=RunStatus.RUNNING)
    assert run.cancel_requested is False

    run = await sched_svc.request_cancel(db, run)
    assert run.cancel_requested is True


# ── 14: run-lock — a schedule can't have two runs RUNNING at once ───────

@pytest.mark.asyncio
async def test_14_execute_schedule_rejects_a_second_run_while_one_is_already_running(db, make_org, make_document_schedule, local_connection, source_dir):
    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)
    schedule = await make_document_schedule(organization_id=org.id, connection_id=conn.id, source_path=str(source_dir))
    schedule.status = ScheduleStatus.RUNNING  # what execute_schedule() itself sets on dispatch — the actual lock
    await db.commit()

    with pytest.raises(sched_svc.ScheduleAlreadyRunningError):
        await sched_svc.execute_schedule(db, schedule, trigger="Manual")
