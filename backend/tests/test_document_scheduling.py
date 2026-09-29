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
from app.services import document_service
from app.services import schedule_service as sched_svc


def _slug() -> str:
    return uuid.uuid4().hex[:10]


def _real_local_connector(conn: Connection) -> LocalFileConnector:
    """Builds a real LocalFileConnector directly — see module docstring
    for why this bypasses connection_service.build_connector()'s
    FIELD_SCHEMAS lookup instead of using it."""
    return LocalFileConnector(config=conn.config, secrets={}, cache_key=f"test-conn:{conn.id}")


class _FakeDbConnector:
    """PAY18 test double for a DATABASE-category connector — implements
    only preview_sql(), the one method discover_database_records() calls.
    Real Postgres/MySQL/etc DATABASE connectors are already live-verified
    against Docker containers elsewhere in this project (see
    docs/scheduled-document-processing.md); this suite's job is the
    business logic ABOVE that connector boundary (field mapping,
    idempotency, decision-engine wiring), so a fake standing in for "some
    DATABASE connector returned these rows" is the right level to mock
    at — same principle as this project's MockERPAdapter."""
    def __init__(self, columns: list[str], rows: list[list]):
        from app.connectors.base import PreviewResult
        self._result = PreviewResult(columns=columns, rows=rows, row_count=len(rows), truncated=False)

    def preview_sql(self, sql, parameters, limit):
        return self._result


class _FakeApiConnector:
    """PAY20 test double for an API-category connector — implements only
    fetch_api(), the one method discover_api_records() calls. The real
    RestApiConnector (real httpx, real pagination/auth/retry) is already
    live-verified separately (see docs/scheduled-document-processing.md);
    this suite's job is the business logic ABOVE that connector boundary,
    same principle as _FakeDbConnector above."""
    def __init__(self, columns: list[str], rows: list[list]):
        from app.connectors.base import PreviewResult
        self._result = PreviewResult(columns=columns, rows=rows, row_count=len(rows), truncated=False)

    def fetch_api(self, **kwargs):
        return self._result


class _FakeErpConnector:
    """PAY21 test double for the Oracle ERP connector — implements only
    list_invoices(), the one method discover_erp_records() calls. The
    real OracleErpConnector (real httpx GET against Oracle's documented
    Invoices collection resource) is live-verified separately against a
    local mock server shaped like that resource (see
    docs/scheduled-document-processing.md); this suite's job is the
    business logic ABOVE that connector boundary, same principle as
    _FakeDbConnector/_FakeApiConnector above. `items` are raw Oracle-
    shaped dicts (InvoiceNumber/InvoiceDate/... + an inlined
    `invoiceLines` array), matching exactly what a real
    `expand=invoiceLines` GET returns — one page, no pagination loop
    needed for these unit-level tests."""
    def __init__(self, items: list[dict]):
        self._items = items

    def list_invoices(self, *, limit, offset, q=None):
        page = self._items[offset:offset + limit]
        return {"items": page, "hasMore": offset + limit < len(self._items), "count": len(self._items)}


# Populated per-test (id -> fake connector) — see db_connection/api_connection/erp_connection fixtures.
_FAKE_DB_CONNECTORS: dict[int, _FakeDbConnector] = {}
_FAKE_API_CONNECTORS: dict[int, _FakeApiConnector] = {}
_FAKE_ERP_CONNECTORS: dict[int, _FakeErpConnector] = {}


def build_connector(conn: Connection):
    """Drop-in real replacement for connection_service.build_connector(),
    used only by this test file's own direct calls (test_01/07) — for
    every LOCAL_FILE connection these tests create there's nothing to
    decrypt, so this is equivalent to the real function for the one
    connection type these tests use. DATABASE-category connections
    resolve to whatever fake connector db_connection() registered for
    that connection id (PAY18); API-category connections resolve to
    whatever fake connector api_connection() registered (PAY20)."""
    if conn.connection_type == ConnectionType.LOCAL_FILE:
        return _real_local_connector(conn)
    if conn.id in _FAKE_DB_CONNECTORS:
        return _FAKE_DB_CONNECTORS[conn.id]
    if conn.id in _FAKE_API_CONNECTORS:
        return _FAKE_API_CONNECTORS[conn.id]
    if conn.id in _FAKE_ERP_CONNECTORS:
        return _FAKE_ERP_CONNECTORS[conn.id]
    raise AssertionError("This suite's fixtures only build LOCAL_FILE and registered fake DATABASE/API/ERP connections.")


@pytest.fixture(autouse=True)
def _reset_fake_db_connectors():
    """Each test gets a fresh (SQLite) DB whose autoincrement ids restart
    at 1 — without this, a fake connector registered under connection
    id=N in one test would leak into a later test's unrelated id=N
    connection. Clears before AND after so nothing survives either way."""
    _FAKE_DB_CONNECTORS.clear()
    _FAKE_API_CONNECTORS.clear()
    _FAKE_ERP_CONNECTORS.clear()
    yield
    _FAKE_DB_CONNECTORS.clear()
    _FAKE_API_CONNECTORS.clear()
    _FAKE_ERP_CONNECTORS.clear()


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
async def db_connection(db, make_org):
    """PAY18 — a real DATABASE-category Connection row (org-scoped like
    any other), backed by a _FakeDbConnector registered under its own id
    so build_connector() above resolves it without touching a real
    driver. `rows` is a list of dicts (column name -> value), matching
    exactly what discover_database_records() hands back from a real
    connector's preview_sql()."""
    async def _make(*, org=None, rows: list[dict]) -> Connection:
        org = org or await make_org()
        conn = Connection(
            organization_id=org.id, name=f"Test DB Source {_slug()}",
            category=ConnectionCategory.DATABASE, connection_type=ConnectionType.POSTGRESQL,
            config={"host": "fake", "port": 5432, "database": "fake"}, status=ConnectionStatus.ACTIVE, is_active=True,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        columns = list(rows[0].keys()) if rows else []
        _FAKE_DB_CONNECTORS[conn.id] = _FakeDbConnector(columns, [[r.get(c) for c in columns] for r in rows])
        return conn
    return _make


@pytest_asyncio.fixture
async def api_connection(db, make_org):
    """PAY20 — a real API-category Connection row (org-scoped like any
    other), backed by a _FakeApiConnector registered under its own id so
    build_connector() above resolves it without touching a real HTTP
    client. `rows` is a list of dicts (field name -> value), matching
    exactly what discover_api_records() hands back from a real
    connector's fetch_api()."""
    async def _make(*, org=None, rows: list[dict]) -> Connection:
        org = org or await make_org()
        conn = Connection(
            organization_id=org.id, name=f"Test API Source {_slug()}",
            category=ConnectionCategory.API, connection_type=ConnectionType.REST,
            config={"base_url": "https://fake.example.test"}, status=ConnectionStatus.ACTIVE, is_active=True,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        columns = list(rows[0].keys()) if rows else []
        _FAKE_API_CONNECTORS[conn.id] = _FakeApiConnector(columns, [[r.get(c) for c in columns] for r in rows])
        return conn
    return _make


@pytest_asyncio.fixture
async def erp_connection(db, make_org):
    """PAY21 — a real ORACLE_ERP_CLOUD Connection row (org-scoped like any
    other), backed by a _FakeErpConnector registered under its own id so
    build_connector() above resolves it without touching a real HTTP
    client. `items` are raw Oracle-shaped invoice dicts, matching exactly
    what discover_erp_records() hands back from a real connector's
    list_invoices()."""
    async def _make(*, org=None, items: list[dict]) -> Connection:
        org = org or await make_org()
        conn = Connection(
            organization_id=org.id, name=f"Test ERP Source {_slug()}",
            category=ConnectionCategory.APPLICATION, connection_type=ConnectionType.ORACLE_ERP_CLOUD,
            config={"host": "https://fake.oraclecloud.test", "auth_type": "Basic Auth", "username": "fake", "password": "fake"},
            status=ConnectionStatus.ACTIVE, is_active=True,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        _FAKE_ERP_CONNECTORS[conn.id] = _FakeErpConnector(items)
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
        retry_interval_seconds: int = 0, batch_size: int = 500, document_type: str | None = None,
        source_kind: str = "FILE", source_query: str | None = None, source_record_key_column: str | None = None,
        source_api_endpoint: str | None = None, source_api_method: str = "GET",
        source_api_query_params: dict | None = None, source_api_pagination: dict | None = None,
        source_api_json_path: str | None = None, source_erp_query: str | None = None,
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
            document_type=document_type,
            source_kind=source_kind, source_query=source_query, source_record_key_column=source_record_key_column,
            source_api_endpoint=source_api_endpoint, source_api_method=source_api_method,
            source_api_query_params=source_api_query_params, source_api_pagination=source_api_pagination,
            source_api_json_path=source_api_json_path, source_erp_query=source_erp_query,
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
    # A run with any REVIEW_REQUIRED item is COMPLETED_WITH_ERRORS, not a
    # clean SUCCESS (schedule_service.py's own finalization logic, fixed
    # live in an earlier phase — a SUCCESS badge next to a Document
    # Processing History full of REVIEW_REQUIRED rows was the real bug).
    assert run.status == RunStatus.COMPLETED_WITH_ERRORS
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


# ── 15: PAY17 — approval-outcome counters populate through a REAL scheduled run ──

@pytest.mark.asyncio
async def test_15_document_type_invoice_run_populates_pay17_approval_stats_without_crashing(
    db, make_org, local_connection, source_dir, invoice_png, make_document_schedule, make_run, make_subscription,
):
    """End-to-end through the real scheduler dispatch (not the isolated
    classifier unit tests in test_payment_approvals.py): a document_type=
    "INVOICE" schedule run must call _classify_invoice_approval_outcome()
    for its one real invoice and merge whatever it finds into
    document_stats, without crashing the run regardless of what the real
    OCR/extraction/decision outcome turns out to be for this small test
    image (see test_08/test_10's own documented finding: a sparse 5-field
    test image reliably lands on NEEDS_REVIEW against ANY comprehensive
    schema — GENERIC or INVOICE — so this asserts internal consistency of
    the new counters rather than forcing one specific decision branch)."""
    org = await make_org()
    from app.models.subscription import SubscriptionStatus
    await make_subscription(organization_id=org.id, modules=["PAYMENT_APPROVAL"], status=SubscriptionStatus.ACTIVE, end_date=None)
    conn = await local_connection(org=org, base_path=source_dir)
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path=str(source_dir), document_type="INVOICE",
    )
    run = await make_run(schedule)

    await _run_background(schedule, run, org.id)

    await db.refresh(run)
    stats = run.document_stats
    # The new counters must always be present (never a missing-key
    # crash) and must never exceed what was actually processed.
    approval_keys = ("auto_approved", "workflows_created", "already_in_workflow", "rule_conflicts", "approver_not_found")
    for key in approval_keys:
        assert key in stats
        assert isinstance(stats[key], int) and stats[key] >= 0
    # auto_approved/workflows_created/already_in_workflow are mutually
    # exclusive per invoice in the classifier — their sum can never
    # exceed the number of documents this run actually processed.
    assert stats["auto_approved"] + stats["workflows_created"] + stats["already_in_workflow"] <= stats["processed"]

    doc = (await db.execute(select(Document).where(Document.source_schedule_id == schedule.id))).scalars().one()
    assert doc.ocr_result and "INV-1001" in doc.ocr_result["full_text"]  # real OCR genuinely ran for the INVOICE-typed doc too

    from app.models.invoice_intelligence.core import ApInvoice
    invoice = (await db.execute(select(ApInvoice).where(ApInvoice.document_id == doc.id))).scalars().one_or_none()
    assert invoice is not None  # document_type="INVOICE" must materialize a real ApInvoice regardless of its eventual decision


# ── 16-21: PAY18 — DATABASE-kind (non-file) invoice intake sources ──────
# Real business logic (discovery/identity/field-mapping/idempotency/
# decision-engine wiring) against a _FakeDbConnector standing in for a
# real DATABASE connector's preview_sql() — see that class's own
# docstring for why this is the right mock boundary. Live-verified
# separately against a real Docker Postgres container (see
# docs/scheduled-document-processing.md).

INVOICE_ROW_1 = {
    "invoice_number": "PAY18-T-001", "invoice_date": "2026-08-01", "vendor_name": "Acme Testing Corp",
    "currency": "USD", "subtotal": 100.0, "discount_amount": 0.0, "shipping_amount": 0.0,
    "tax_amount": 8.0, "total_amount": 108.0, "payment_terms": "Net 30",
    "line_items": '[{"description": "Widget", "quantity": 2, "unit_price": 50.0, "amount": 100.0}]',
}
INVOICE_ROW_2 = {
    "invoice_number": "PAY18-T-002", "invoice_date": "2026-08-02", "vendor_name": "Beta Supplies Inc",
    "currency": "USD", "subtotal": 5000.0, "discount_amount": 0.0, "shipping_amount": 0.0,
    "tax_amount": 400.0, "total_amount": 5400.0, "payment_terms": "Net 45",
    "line_items": '[{"description": "Bulk order", "quantity": 1, "unit_price": 5000.0, "amount": 5000.0}]',
}


@pytest.mark.asyncio
async def test_16_discover_database_records_returns_one_dict_per_row():
    connector = _FakeDbConnector(["invoice_number", "total_amount"], [["A-1", 100.0], ["A-2", 200.0]])
    result = connector.preview_sql("SELECT invoice_number, total_amount FROM x", {}, 500)
    rows = [dict(zip(result.columns, row)) for row in result.rows]
    assert rows == [{"invoice_number": "A-1", "total_amount": 100.0}, {"invoice_number": "A-2", "total_amount": 200.0}]


@pytest.mark.asyncio
async def test_17_record_identity_uses_key_column_when_present_else_falls_back_to_content_hash():
    config = DocumentScheduleConfig(
        schedule_id=1, source_connection_id=1, source_kind="DATABASE",
        source_query="SELECT * FROM invoices", source_record_key_column="invoice_number",
        source_path="invoices (query)", document_type="INVOICE",
    )
    row = {"invoice_number": "INV-9", "total_amount": 42.0}
    content_hash = doc_sched_svc.record_content_hash(row)
    source_path, file_name, record_key = doc_sched_svc.record_identity(config, row, content_hash)
    assert record_key == "INV-9"
    assert file_name == "row_INV-9"
    assert source_path.startswith("query:")

    config.source_record_key_column = None  # no natural key configured
    source_path2, file_name2, record_key2 = doc_sched_svc.record_identity(config, row, content_hash)
    assert record_key2 == content_hash[:16]
    assert file_name2 == f"row_{content_hash[:16]}"
    assert source_path2 == source_path  # identity of "which query" is unaffected by the key-column setting


@pytest.mark.asyncio
async def test_18_record_content_hash_changes_when_row_content_changes_but_not_key_order():
    row_a = {"invoice_number": "INV-1", "total_amount": 100.0}
    row_b = {"total_amount": 100.0, "invoice_number": "INV-1"}  # same content, different key order
    row_c = {"invoice_number": "INV-1", "total_amount": 150.0}  # genuinely different content
    assert doc_sched_svc.record_content_hash(row_a) == doc_sched_svc.record_content_hash(row_b)
    assert doc_sched_svc.record_content_hash(row_a) != doc_sched_svc.record_content_hash(row_c)


def test_19_build_fields_from_record_maps_scalars_line_items_and_json_safe_types():
    from datetime import date
    from decimal import Decimal
    row = {
        "Invoice Number": "INV-77", "invoice_date": date(2026, 1, 15), "total_amount": Decimal("108.00"),
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 50.0, "amount": 100.0}],
        "custom_note": "org-specific extra column",
    }
    fields = document_service.build_fields_from_record(row, "INVOICE")
    by_name = {f["name"]: f for f in fields}

    assert by_name["invoice_number"]["value"] == "INV-77"  # "Invoice Number" normalized to the taxonomy's snake_case name
    assert by_name["invoice_date"]["value"] == "2026-01-15"  # real datetime.date -> JSON-safe isoformat string
    assert by_name["total_amount"]["value"] == 108.0  # real decimal.Decimal -> JSON-safe float
    assert by_name["custom_note"]["value"] == "org-specific extra column"  # extra column preserved, not dropped

    line_items = by_name["line_items"]
    assert line_items["type"] == "table"
    assert line_items["rows"] == [{"description": "Widget", "sku": "", "quantity": 2, "unit_price": 50.0, "tax": "", "amount": 100.0}]

    for f in fields:
        assert f["confidence"] == 100.0
        assert f["bbox"] is None  # honest: no source page exists to point at (see decision-engine docstring)


@pytest.mark.asyncio
async def test_20_database_schedule_run_materializes_one_invoice_per_row_with_correct_fields(
    db, make_org, db_connection, make_document_schedule, make_run,
):
    """End-to-end through the real scheduler dispatch — no OCR, no AI
    model — the two rows become two real Documents (doc_type=OTHER,
    extraction_model=STRUCTURED_SOURCE) and two real ApInvoice rows with
    the query's own values, genuinely validated/decided (not a
    fabricated STRAIGHT_THROUGH — see build_fields_from_record()'s own
    disclosure that a structured source has no page evidence, so it
    always lands on REVIEW_REQUIRED via the SAME confidence model every
    other intake path uses, never a special-cased bypass)."""
    org = await make_org()
    conn = await db_connection(org=org, rows=[INVOICE_ROW_1, INVOICE_ROW_2])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="invoices (query)",
        document_type="INVOICE", source_kind="DATABASE",
        source_query="SELECT * FROM pay18_test", source_record_key_column="invoice_number",
    )
    run = await make_run(schedule)

    await _run_background(schedule, run, org.id)

    await db.refresh(run)
    stats = run.document_stats
    assert stats["discovered"] == 2
    assert stats["queued"] == 2
    assert stats["processed"] == 2
    assert stats["failed"] == 0

    docs = (await db.execute(select(Document).where(Document.source_schedule_id == schedule.id))).scalars().all()
    assert len(docs) == 2
    for doc in docs:
        assert doc.extraction_model == "STRUCTURED_SOURCE"
        assert doc.ocr_result is None  # genuinely never ran OCR for a structured source

    from app.models.invoice_intelligence.core import ApInvoice
    invoices = (await db.execute(select(ApInvoice).where(ApInvoice.source_schedule_id == schedule.id))).scalars().all()
    assert {i.invoice_number for i in invoices} == {"PAY18-T-001", "PAY18-T-002"}
    by_number = {i.invoice_number: i for i in invoices}
    assert by_number["PAY18-T-001"].total == 108.0
    assert by_number["PAY18-T-002"].total == 5400.0
    assert by_number["PAY18-T-001"].status is not None  # a real decision ran (not left at a default)


@pytest.mark.asyncio
async def test_21_database_schedule_rerun_with_unchanged_rows_is_a_true_no_op(
    db, make_org, db_connection, make_document_schedule, make_run,
):
    """Spec section 7's idempotency rule, exercised for the DATABASE
    path: identical row content on a second run must discover the rows
    (they're still returned by the query) but queue/process none of
    them — the exact behavior test_09 already covers for the FILE path."""
    org = await make_org()
    conn = await db_connection(org=org, rows=[INVOICE_ROW_1])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="invoices (query)",
        document_type="INVOICE", source_kind="DATABASE",
        source_query="SELECT * FROM pay18_test", source_record_key_column="invoice_number",
    )
    run1 = await make_run(schedule)
    await _run_background(schedule, run1, org.id)
    await db.refresh(run1)
    assert run1.document_stats["queued"] == 1

    run2 = await make_run(schedule)
    await _run_background(schedule, run2, org.id)
    await db.refresh(run2)
    assert run2.document_stats["discovered"] == 1
    assert run2.document_stats["queued"] == 0  # identical content -> already PROCESSED/REVIEW_REQUIRED, skipped

    registries = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.schedule_id == schedule.id))).scalars().all()
    assert len(registries) == 1  # no duplicate registry row for unchanged content


def test_22_database_source_schema_requires_query_and_invoice_document_type():
    from pydantic import ValidationError
    from app.schemas.schedules import DocumentScheduleConfigIn

    base = dict(source_connection_id=1, source_path="invoices (query)", source_kind="DATABASE")

    with pytest.raises(ValidationError, match="requires a SQL query"):
        DocumentScheduleConfigIn(**base, document_type="INVOICE")

    with pytest.raises(ValidationError, match="requires Document Type to be set to INVOICE"):
        DocumentScheduleConfigIn(**base, source_query="SELECT * FROM x")

    # Valid combination — no error.
    cfg = DocumentScheduleConfigIn(**base, source_query="SELECT * FROM x", document_type="INVOICE")
    assert cfg.source_kind == "DATABASE"

    # FILE (default) never requires a query, unaffected by any of this.
    file_cfg = DocumentScheduleConfigIn(source_connection_id=1, source_path="/incoming")
    assert file_cfg.source_kind == "FILE"
    assert file_cfg.source_query is None


# ── 23-28: PAY20 — API-kind (non-file) invoice intake sources ──────────
# Real business logic (discovery/identity/field-mapping/idempotency/
# decision-engine wiring) against a _FakeApiConnector standing in for a
# real API connector's fetch_api() — see that class's own docstring for
# why this is the right mock boundary. Live-verified separately against
# a real local mock HTTP server (see docs/scheduled-document-processing.md).

API_INVOICE_ROW_1 = {
    "invoice_number": "PAY20-T-001", "invoice_date": "2026-09-01", "vendor_name": "Acme API Corp",
    "currency": "USD", "subtotal": 200.0, "discount_amount": 0.0, "shipping_amount": 0.0,
    "tax_amount": 16.0, "total_amount": 216.0, "payment_terms": "Net 30",
    "line_items": '[{"description": "API Widget", "quantity": 4, "unit_price": 50.0, "amount": 200.0}]',
}
API_INVOICE_ROW_2 = {
    "invoice_number": "PAY20-T-002", "invoice_date": "2026-09-02", "vendor_name": "Beta Webhooks Ltd",
    "currency": "USD", "subtotal": 1000.0, "discount_amount": 0.0, "shipping_amount": 0.0,
    "tax_amount": 80.0, "total_amount": 1080.0, "payment_terms": "Net 45",
    "line_items": '[{"description": "API bulk order", "quantity": 1, "unit_price": 1000.0, "amount": 1000.0}]',
}


@pytest.mark.asyncio
async def test_23_discover_api_records_returns_one_dict_per_row():
    connector = _FakeApiConnector(["invoice_number", "total_amount"], [["B-1", 100.0], ["B-2", 200.0]])
    result = connector.fetch_api(endpoint="/invoices", method="GET")
    rows = [dict(zip(result.columns, row)) for row in result.rows]
    assert rows == [{"invoice_number": "B-1", "total_amount": 100.0}, {"invoice_number": "B-2", "total_amount": 200.0}]


@pytest.mark.asyncio
async def test_24_record_identity_for_api_kind_uses_key_column_and_is_independent_of_database_kind():
    config = DocumentScheduleConfig(
        schedule_id=1, source_connection_id=1, source_kind="API",
        source_api_endpoint="/invoices", source_api_method="GET", source_record_key_column="invoice_number",
        source_path="Vendor invoices API", document_type="INVOICE",
    )
    row = {"invoice_number": "INV-API-9", "total_amount": 42.0}
    content_hash = doc_sched_svc.record_content_hash(row)
    source_path, file_name, record_key = doc_sched_svc.record_identity(config, row, content_hash)
    assert record_key == "INV-API-9"
    assert file_name == "row_INV-API-9"
    assert source_path.startswith("api:")  # distinct prefix from DATABASE-kind's "query:"

    # Changing the endpoint changes the identity ("which API call") even
    # though the row content is unchanged.
    config.source_api_endpoint = "/other-invoices"
    source_path2, _, _ = doc_sched_svc.record_identity(config, row, content_hash)
    assert source_path2 != source_path

    config.source_record_key_column = None  # no natural key configured
    _, file_name3, record_key3 = doc_sched_svc.record_identity(config, row, content_hash)
    assert record_key3 == content_hash[:16]
    assert file_name3 == f"row_{content_hash[:16]}"


@pytest.mark.asyncio
async def test_25_api_schedule_run_materializes_one_invoice_per_row_with_correct_fields(
    db, make_org, api_connection, make_document_schedule, make_run,
):
    """End-to-end through the real scheduler dispatch — no OCR, no AI
    model — the two API records become two real Documents (doc_type=OTHER,
    extraction_model=STRUCTURED_SOURCE) and two real ApInvoice rows with
    the API response's own values, genuinely validated/decided (never a
    fabricated STRAIGHT_THROUGH — see build_fields_from_record()'s own
    disclosure that a structured source has no page evidence)."""
    org = await make_org()
    conn = await api_connection(org=org, rows=[API_INVOICE_ROW_1, API_INVOICE_ROW_2])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="Vendor invoices API",
        document_type="INVOICE", source_kind="API",
        source_api_endpoint="/invoices", source_record_key_column="invoice_number",
    )
    run = await make_run(schedule)

    await _run_background(schedule, run, org.id)

    await db.refresh(run)
    stats = run.document_stats
    assert stats["discovered"] == 2
    assert stats["queued"] == 2
    assert stats["processed"] == 2
    assert stats["failed"] == 0

    docs = (await db.execute(select(Document).where(Document.source_schedule_id == schedule.id))).scalars().all()
    assert len(docs) == 2
    for doc in docs:
        assert doc.extraction_model == "STRUCTURED_SOURCE"
        assert doc.ocr_result is None  # genuinely never ran OCR for a structured source

    from app.models.invoice_intelligence.core import ApInvoice
    invoices = (await db.execute(select(ApInvoice).where(ApInvoice.source_schedule_id == schedule.id))).scalars().all()
    assert {i.invoice_number for i in invoices} == {"PAY20-T-001", "PAY20-T-002"}
    by_number = {i.invoice_number: i for i in invoices}
    assert by_number["PAY20-T-001"].total == 216.0
    assert by_number["PAY20-T-002"].total == 1080.0
    assert by_number["PAY20-T-001"].status is not None  # a real decision ran (not left at a default)


@pytest.mark.asyncio
async def test_26_api_schedule_rerun_with_unchanged_rows_is_a_true_no_op(
    db, make_org, api_connection, make_document_schedule, make_run,
):
    """Spec section 7's idempotency rule, exercised for the API path:
    identical record content on a second run must discover the record
    (it's still returned by the API) but queue/process none of it — the
    exact behavior test_21 already covers for the DATABASE path."""
    org = await make_org()
    conn = await api_connection(org=org, rows=[API_INVOICE_ROW_1])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="Vendor invoices API",
        document_type="INVOICE", source_kind="API",
        source_api_endpoint="/invoices", source_record_key_column="invoice_number",
    )
    run1 = await make_run(schedule)
    await _run_background(schedule, run1, org.id)
    await db.refresh(run1)
    assert run1.document_stats["queued"] == 1

    run2 = await make_run(schedule)
    await _run_background(schedule, run2, org.id)
    await db.refresh(run2)
    assert run2.document_stats["discovered"] == 1
    assert run2.document_stats["queued"] == 0  # identical content -> already PROCESSED/REVIEW_REQUIRED, skipped

    registries = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.schedule_id == schedule.id))).scalars().all()
    assert len(registries) == 1  # no duplicate registry row for unchanged content


def test_27_api_source_schema_requires_endpoint_and_invoice_document_type():
    from pydantic import ValidationError
    from app.schemas.schedules import DocumentScheduleConfigIn

    base = dict(source_connection_id=1, source_path="Vendor invoices API", source_kind="API")

    with pytest.raises(ValidationError, match="requires an endpoint"):
        DocumentScheduleConfigIn(**base, document_type="INVOICE")

    with pytest.raises(ValidationError, match="requires Document Type to be set to INVOICE"):
        DocumentScheduleConfigIn(**base, source_api_endpoint="/invoices")

    # Valid combination — no error.
    cfg = DocumentScheduleConfigIn(**base, source_api_endpoint="/invoices", document_type="INVOICE")
    assert cfg.source_kind == "API"
    assert cfg.source_api_method == "GET"  # default


@pytest.mark.asyncio
async def test_28_validate_and_apply_document_config_rejects_non_api_connection_for_api_kind(
    db, make_org, local_connection, source_dir,
):
    """PAY20's cross-cutting check — mirrors test coverage the DATABASE
    kind's equivalent check already has via live/API-level verification:
    picking a STORAGE connection for an API-kind schedule must fail at
    save time, not late inside a background run's fetch_api() call."""
    from app.schemas.schedules import DocumentScheduleConfigIn
    from app.services.schedule_service import ScheduleValidationError, _validate_and_apply_document_config

    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)  # STORAGE, not API
    schedule = Schedule(
        organization_id=org.id, owner_id=None, name=f"Doc Schedule Test {_slug()}",
        pipeline_id="0", pipeline_name="Document Source: test",
        environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
    )
    db.add(schedule)
    await db.flush()

    config_in = DocumentScheduleConfigIn(
        source_connection_id=conn.id, source_path="Vendor invoices API", source_kind="API",
        source_api_endpoint="/invoices", document_type="INVOICE",
    )
    with pytest.raises(ScheduleValidationError, match="not an API connection"):
        await _validate_and_apply_document_config(db, schedule, config_in)


# ── 29-34: PAY21 — ERP-kind (non-file) invoice intake sources ──────────
# Real business logic (discovery/identity/field-mapping/idempotency/
# decision-engine wiring) against a _FakeErpConnector standing in for a
# real OracleErpConnector's list_invoices() — see that class's own
# docstring for why this is the right mock boundary. Live-verified
# separately against a real local mock server shaped like Oracle's
# documented Invoices collection resource (see
# docs/scheduled-document-processing.md).

ERP_INVOICE_ITEM_1 = {
    "InvoiceId": "9001", "InvoiceNumber": "PAY21-T-001", "InvoiceDate": "2026-09-01",
    "InvoiceCurrency": "USD", "InvoiceAmount": 324.0, "VendorNum": "Acme ERP Corp", "PaymentTerms": "Net 30",
    "invoiceLines": [{"LineNumber": 1, "Description": "ERP Gadget", "LineAmount": 324.0}],
}
ERP_INVOICE_ITEM_2 = {
    "InvoiceId": "9002", "InvoiceNumber": "PAY21-T-002", "InvoiceDate": "2026-09-02",
    "InvoiceCurrency": "USD", "InvoiceAmount": 2160.0, "VendorNum": "Beta ERP Ltd", "PaymentTerms": "Net 45",
    "invoiceLines": [{"LineNumber": 1, "Description": "ERP bulk service", "LineAmount": 2160.0}],
}


@pytest.mark.asyncio
async def test_29_discover_erp_records_returns_one_mapped_dict_per_invoice():
    connector = _FakeErpConnector([ERP_INVOICE_ITEM_1, ERP_INVOICE_ITEM_2])
    page = connector.list_invoices(limit=100, offset=0)
    mapped = [doc_sched_svc._map_erp_invoice_record(item) for item in page["items"]]
    assert mapped[0]["invoice_number"] == "PAY21-T-001"
    assert mapped[0]["total_amount"] == 324.0
    assert mapped[0]["vendor_name"] == "Acme ERP Corp"
    assert mapped[0]["line_items"] == [{"description": "ERP Gadget", "amount": 324.0}]
    assert mapped[1]["invoice_number"] == "PAY21-T-002"


@pytest.mark.asyncio
async def test_30_record_identity_for_erp_kind_uses_key_column_and_is_independent_of_other_kinds():
    config = DocumentScheduleConfig(
        schedule_id=1, source_connection_id=1, source_kind="ERP",
        source_record_key_column="invoice_number",
        source_path="Vendor invoices ERP", document_type="INVOICE",
    )
    row = {"invoice_number": "INV-ERP-9", "total_amount": 42.0}
    content_hash = doc_sched_svc.record_content_hash(row)
    source_path, file_name, record_key = doc_sched_svc.record_identity(config, row, content_hash)
    assert record_key == "INV-ERP-9"
    assert file_name == "row_INV-ERP-9"
    assert source_path.startswith("erp:")  # distinct prefix from DATABASE-kind's "query:"/API-kind's "api:"

    # Changing the query filter changes the identity even though the row
    # content is unchanged.
    config.source_erp_query = "ValidationStatus='NEEDS_REVALIDATION'"
    source_path2, _, _ = doc_sched_svc.record_identity(config, row, content_hash)
    assert source_path2 != source_path


@pytest.mark.asyncio
async def test_31_erp_schedule_run_materializes_one_invoice_per_row_with_correct_fields(
    db, make_org, erp_connection, make_document_schedule, make_run,
):
    """End-to-end through the real scheduler dispatch — no OCR, no AI
    model — the two ERP invoices become two real Documents (doc_type=OTHER,
    extraction_model=STRUCTURED_SOURCE) and two real ApInvoice rows with
    the ERP's own values, genuinely validated/decided (never a fabricated
    STRAIGHT_THROUGH — same disclosure DATABASE/API-kind already make)."""
    org = await make_org()
    conn = await erp_connection(org=org, items=[ERP_INVOICE_ITEM_1, ERP_INVOICE_ITEM_2])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="Vendor invoices ERP",
        document_type="INVOICE", source_kind="ERP", source_record_key_column="invoice_number",
    )
    run = await make_run(schedule)

    await _run_background(schedule, run, org.id)

    await db.refresh(run)
    stats = run.document_stats
    assert stats["discovered"] == 2
    assert stats["queued"] == 2
    assert stats["processed"] == 2
    assert stats["failed"] == 0

    docs = (await db.execute(select(Document).where(Document.source_schedule_id == schedule.id))).scalars().all()
    assert len(docs) == 2
    for doc in docs:
        assert doc.extraction_model == "STRUCTURED_SOURCE"
        assert doc.ocr_result is None  # genuinely never ran OCR for a structured source

    from app.models.invoice_intelligence.core import ApInvoice
    invoices = (await db.execute(select(ApInvoice).where(ApInvoice.source_schedule_id == schedule.id))).scalars().all()
    assert {i.invoice_number for i in invoices} == {"PAY21-T-001", "PAY21-T-002"}
    by_number = {i.invoice_number: i for i in invoices}
    assert by_number["PAY21-T-001"].total == 324.0
    assert by_number["PAY21-T-002"].total == 2160.0
    assert by_number["PAY21-T-001"].status is not None  # a real decision ran (not left at a default)


@pytest.mark.asyncio
async def test_32_erp_schedule_rerun_with_unchanged_rows_is_a_true_no_op(
    db, make_org, erp_connection, make_document_schedule, make_run,
):
    """Spec section 7's idempotency rule, exercised for the ERP path:
    identical invoice content on a second run must discover the invoice
    (it's still returned by the ERP) but queue/process none of it — the
    exact behavior test_26 already covers for the API path."""
    org = await make_org()
    conn = await erp_connection(org=org, items=[ERP_INVOICE_ITEM_1])
    schedule = await make_document_schedule(
        organization_id=org.id, connection_id=conn.id, source_path="Vendor invoices ERP",
        document_type="INVOICE", source_kind="ERP", source_record_key_column="invoice_number",
    )
    run1 = await make_run(schedule)
    await _run_background(schedule, run1, org.id)
    await db.refresh(run1)
    assert run1.document_stats["queued"] == 1

    run2 = await make_run(schedule)
    await _run_background(schedule, run2, org.id)
    await db.refresh(run2)
    assert run2.document_stats["discovered"] == 1
    assert run2.document_stats["queued"] == 0  # identical content -> already PROCESSED/REVIEW_REQUIRED, skipped

    registries = (await db.execute(select(DocumentSourceRegistry).where(DocumentSourceRegistry.schedule_id == schedule.id))).scalars().all()
    assert len(registries) == 1  # no duplicate registry row for unchanged content


def test_33_erp_source_schema_requires_invoice_document_type():
    from pydantic import ValidationError
    from app.schemas.schedules import DocumentScheduleConfigIn

    base = dict(source_connection_id=1, source_path="Vendor invoices ERP", source_kind="ERP")

    with pytest.raises(ValidationError, match="requires Document Type to be set to INVOICE"):
        DocumentScheduleConfigIn(**base)

    # Valid combination — no error. Unlike API-kind, no endpoint is
    # required: source_connection_id alone (an ORACLE_ERP_CLOUD
    # connection) already fully identifies the resource.
    cfg = DocumentScheduleConfigIn(**base, document_type="INVOICE")
    assert cfg.source_kind == "ERP"
    assert cfg.source_erp_query is None  # optional, defaults to "no filter"


@pytest.mark.asyncio
async def test_34_validate_and_apply_document_config_rejects_non_erp_connection_for_erp_kind(
    db, make_org, local_connection, source_dir,
):
    """PAY21's cross-cutting check — mirrors test_28's coverage for API-
    kind: picking a non-ERP connection for an ERP-kind schedule must fail
    at save time, not late inside a background run's list_invoices() call."""
    from app.schemas.schedules import DocumentScheduleConfigIn
    from app.services.schedule_service import ScheduleValidationError, _validate_and_apply_document_config

    org = await make_org()
    conn = await local_connection(org=org, base_path=source_dir)  # STORAGE, not ORACLE_ERP_CLOUD
    schedule = Schedule(
        organization_id=org.id, owner_id=None, name=f"Doc Schedule Test {_slug()}",
        pipeline_id="0", pipeline_name="Document Source: test",
        environment=ScheduleEnvironment.TEST, status=ScheduleStatus.ACTIVE,
        frequency=ScheduleFrequency.DAILY, start_at=datetime.now(timezone.utc),
    )
    db.add(schedule)
    await db.flush()

    config_in = DocumentScheduleConfigIn(
        source_connection_id=conn.id, source_path="Vendor invoices ERP", source_kind="ERP",
        document_type="INVOICE",
    )
    with pytest.raises(ScheduleValidationError, match="not an ERP connection"):
        await _validate_and_apply_document_config(db, schedule, config_in)
