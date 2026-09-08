# Scheduled Document Intelligence Workflow

Automatic discovery and processing of documents from an external source (local
filesystem, SFTP, FTP, SMB network share, Azure Blob, AWS S3) on a recurring
schedule, using the exact same OCR/AI extraction pipeline as manual document
upload and the Document Intelligence wizard — never a second, parallel
implementation.

This document describes what was built, how it fits into the existing
Schedules module, how to configure and operate it, and what its real,
disclosed limitations are.

## 1. What this is (and isn't)

A **Document Source schedule** is a `Schedule` row (the same table every
Pipeline Builder / Anomaly Job schedule uses) with an attached
`DocumentScheduleConfig`. When a schedule has that config, `execute_schedule()`
takes a different dispatch branch: instead of running a simulated pipeline
job, it:

1. Lists files at the configured source path via the connection's real
   connector (`FileConnectorBase.list_files()`).
2. Computes a real streamed SHA-256 hash of each file.
3. Looks up (or creates) a durable **idempotency ledger** row keyed on
   `(organization_id, source_connection_id, source_path, file_name,
   content_hash)` — never reprocesses a file that identity already covers.
4. For every genuinely new/changed file: downloads it, creates a real
   `Document` row, and runs it through
   `document_service.process_document()` — **the same function** manual scan,
   bulk upload, and Review Queue reprocess all call. There is no second OCR
   or AI extraction implementation anywhere in this feature.
5. Routes the result to either `COMPLETED` or `NEEDS_REVIEW` (→ a real
   `ReviewQueueItem`) based on the schedule's own configurable
   confidence/accuracy thresholds.

What it deliberately does **not** do:

- Reimplement OCR, AI extraction, or Review Queue routing.
- Use a second execution engine — it's a new dispatch branch inside the
  existing `schedule_service.execute_schedule()`, following the exact
  precedent AD12 (Anomaly Job scheduling) set.
- Require Redis or any external job queue — concurrency is a single
  `asyncio.Semaphore` per run, and the "only one run at a time per schedule"
  guarantee is a DB compare-and-swap on `Schedule.status`. This is a
  single-process limitation, disclosed in §8 below.

## 2. Data model

```
Schedule (existing table)
 └── DocumentScheduleConfig   (1:1, optional — presence = "this is a document schedule")
      source_connection_id, source_path, file_pattern, recursive_scan,
      batch_size, max_concurrency, confidence_threshold, accuracy_threshold,
      archive_after_success, archive_path, move_failed_to_review_path,
      duplicate_content_behavior, max_file_size_mb

DocumentSourceRegistry          (the idempotency ledger — spec §7/8)
 UNIQUE(organization_id, source_connection_id, source_path, file_name, content_hash)
      status: DISCOVERED → QUEUED → PROCESSING → {OCR_PROCESSING, AI_PROCESSING,
              VALIDATING} → {PROCESSED, REVIEW_REQUIRED, FAILED} | SKIPPED | UNSUPPORTED
      processing_run_id  → ScheduleRun.id   (which run actually acted on this file —
                                              see §7 for why this is subtle)
      document_id        → Document.id

Document (existing table, additive columns)
      source_connection_id, source_path, source_schedule_id

ScheduleRun (existing table, additive columns)
      cancel_requested: bool
      document_stats: JSON  {discovered, queued, processed, success, review,
                              failed, skipped, unsupported, current_file, current_stage}
```

`DocumentSourceRegistry` is deliberately **not** scoped by `schedule_id`:
two different schedules pointed at the same connection+path share dedup
state, matching the spec's identity key exactly. `schedule_id` is stamped
for informational/filtering purposes only.

Migration: `backend/scripts/migrate_document_schedules.py` (idempotent
`ALTER TABLE` script for the additive columns above — run once against
SQLite, then restart the backend). `create_all()` picks up the two new
tables automatically.

## 2a. DATABASE source kind (PAY18)

Everything above describes the original **FILE**-kind schedule. PAY18 added
a second source kind, `DocumentScheduleConfig.source_kind = "DATABASE"`,
for intake that already lives as structured rows in a database rather than
as scanned documents:

```
DocumentScheduleConfig (additive columns)
      source_kind: "FILE" | "DATABASE"           (default "FILE")
      source_query: str | None                    (DATABASE only — a SELECT/WITH statement)
      source_record_key_column: str | None         (DATABASE only — optional business key column)
```

For a DATABASE-kind schedule:

1. `source_connection_id` must point at a DATABASE-category `Connection`
   (Postgres/MySQL/SQL Server/etc.) — enforced by
   `schedule_service._validate_and_apply_document_config()`.
2. `document_schedule_service.discover_database_records()` runs
   `source_query` through that connector's **existing**
   `preview_sql()` (the same dialect-agnostic SQLAlchemy execution the SQL
   Workspace preview surface already uses — `ConnectorValidationError`
   enforces SELECT/WITH-only), bounded by `batch_size`, and returns one
   `dict` per row.
3. Each row gets a synthetic identity so the **same**
   `DocumentSourceRegistry` idempotency ledger (§2) works unchanged: no
   real file path exists, so `record_identity()` builds
   `source_path = f"query:{sha256(source_query)[:16]}"` and
   `file_name = f"row_{record_key}"` (the value of
   `source_record_key_column` in that row, or a content-hash prefix if no
   key column is configured). `content_hash` is
   `sha256(json.dumps(row, sort_keys=True, default=str))` — a content
   change under the same key is a new version, exactly like a changed file
   would be.
4. `document_service.save_database_record()` persists the row as a real
   `Document` (JSON-serialized to disk, `mime_type="application/json"`),
   then `document_service.build_fields_from_record()` maps each column
   straight onto the `INVOICE` taxonomy's field shape (a column matching
   the taxonomy's `line_items` table name becomes a real table field), and
   `process_document(..., preextracted_fields=fields)` **skips the Image
   Scanner/OCR/AI Extraction stages entirely** (there is no image to scan
   and no model call to make — the query's own columns already are the
   structured data) and records `extraction_model = "STRUCTURED_SOURCE"` at
   100% extractor confidence. Every stage from validation onward — business
   rules, `invoice_validation_service`, `invoice_decision_service`, Review
   Queue routing, PAY9's approval-workflow trigger — is the **same code
   path**, unmodified, regardless of where the fields came from.
5. Currently document_type is restricted to `"INVOICE"` for DATABASE-kind
   schedules (enforced by a pydantic model validator on
   `DocumentScheduleConfigIn`) — this is where the taxonomy/mapping/
   decision pipeline already exists end-to-end; a generic-document DATABASE
   intake was out of scope for PAY18.

**Disclosed limitation:** a DATABASE-sourced invoice's `evidence_quality`
confidence score is always 0% (there is no OCR page for a `bbox` to point
at), so it can never clear `STRAIGHT_THROUGH_THRESHOLD` and always lands on
`REVIEW_REQUIRED` — see [payment-approval-workflow.md §11](payment-approval-workflow.md)
for the full analysis and live-verification numbers. This is intentional,
honest routing, not a gap to be "fixed" by fabricating a confidence score.

**ERP-sourced intake is now built too** — see §2d below. As of PAY21, all
four planned non-file intake kinds for this workflow (FILE incl. email
inbox §2b, DATABASE §2a, API §2c, ERP §2d) exist; nothing further is
disclosed as outstanding for this specific workflow.

## 2b. Email inbox (IMAP) source (PAY19)

Unlike DATABASE-kind above, email intake needed **no new `source_kind`
and no `document_schedule_service.py`/`schedule_service.py` changes at
all** — it's a new `ConnectionType.EMAIL_IMAP`
(`connectors/email/imap_client.py`, real stdlib `imaplib`/`email`)
implementing the exact same `FileConnectorBase` contract SFTP/FTP/Network
File already implement (`list_files`/`open_read_stream`/`move_file`/
`file_checksum`/`test_connection`). Since a `source_kind="FILE"` schedule
(the original, still-default kind — §1) is already generic across any
STORAGE-category connection, a Document Source schedule pointed at an
`EMAIL_IMAP` connection runs through the **real, unmodified** OCR → AI
Extraction → validation → decision → Review Queue pipeline — the same
code every SFTP/FTP schedule already uses, with zero new dispatch branch.

Identity model: one "file" = one attachment on one message matching the
schedule's `file_pattern`. `object_name` encodes
`"{uid}_{part_index}_{filename}"` (IMAP UID, not sequence number — stable
across sessions); `record`/file identity for the existing
`DocumentSourceRegistry` ledger (§5) is otherwise unchanged. `list_files()`
runs a real IMAP `SEARCH` (default `UNSEEN`, configurable per-connection
via `search_criteria`) then a real `FETCH RFC822` per matching message to
enumerate attachments; `open_read_stream()` re-fetches by UID and returns
the specific part's bytes.

**Disclosed quirk:** IMAP's atomic unit is the message, not the
attachment — `move_file()` (used by "archive after success") moves the
**whole** source email. A schedule against a mailbox with routinely
multi-attachment emails should disable archiving or set
`max_concurrency=1`; this is disclosed in the connector's own docstring,
not silently papered over.

Live-verified end-to-end against a real Docker `greenmail-standalone`
SMTP+IMAP test server: a real email with a real PDF invoice attachment,
sent via `smtplib`, was discovered/downloaded/OCR'd (Tesseract)/AI-
extracted (real Ollama `llama3.2`) through the actual scheduled workflow —
correct `invoice_number`/`vendor_name`/`total`, landed on
`REVIEW_REQUIRED` at a genuine 34% extractor confidence (not forced), a
real Review Queue item was created, and the source email was correctly
**not** archived (archiving only fires on the `success` outcome bucket,
which a NEEDS_REVIEW document never reaches — confirmed the message
stayed in the inbox afterward). Unlike DATABASE-kind, a FILE-kind email
attachment has a real OCR page, so `evidence_quality` is real and non-zero
— an email-sourced invoice CAN reach STRAIGHT_THROUGH given a
clean-enough source document, unlike a DATABASE-sourced one.

## 2c. API endpoint source (PAY20)

A third `source_kind="API"`, structurally a close mirror of DATABASE-kind
(§2a): `source_connection_id` points at an API-category connection and
each decoded response record becomes one `ApInvoice` via the same
`preextracted_fields` path as DATABASE-kind — no OCR/AI Extraction stage.
The genuinely new surface area is small because it reuses the
**already-existing** `BaseConnector.fetch_api()` capability (real,
paginated HTTP fetch via `httpx` in `RestApiConnector` — the same code
the SQL Workspace/Source Dataset "preview-api" surface already calls),
supporting `none`/`page`/`offset`/`cursor`/`token` pagination styles,
`json_path` envelope extraction, and any of that connector's existing
auth types.

New: 5 columns on `DocumentScheduleConfig` (`source_api_endpoint`,
`source_api_method`, `source_api_query_params`, `source_api_pagination`,
`source_api_json_path`); `document_schedule_service.discover_api_records()`
(mirrors `discover_database_records()`); `is_database_source` in
`schedule_service.py` generalized to `is_record_source = source_kind in
("DATABASE", "API")`, with discovery dispatch still branching internally
by exact kind. `record_identity()` branches on `source_kind` to build a
distinct `source_path` prefix (`"api:{hash}"` vs `"query:{hash}"`, the
hash seeded by the endpoint/method/query_params/pagination dict) while
reusing the same `source_record_key_column` field and content-hash
fallback as DATABASE-kind. `_process_one_record()` and
`document_service.save_database_record()`/`build_fields_from_record()`
needed **no changes at all** — both were already source-kind-agnostic,
operating on a plain `dict`.

Live-verified end-to-end against a real local `http.server` mock REST API
exercising genuine multi-page pagination (2 real HTTP GET requests via
the real `RestApiConnector.fetch_api()`, not a mocked connector): correct
data across both pages, correct invoice materialization, and a correct
idempotent no-op on rerun (`queued: 0`). Separately live-verified through
the actual **Create Schedule UI** — a schedule built via the real modal
(Connection picker, Endpoint, Method, Pagination Style + Page Size, JSON
Path, Record Key Column) saved via a real `POST /api/schedules` (201) and
ran with `discovered: 2, skipped: 2` — correctly deduping against the
prior schedule's already-ingested rows via the shared identity hash. Like
DATABASE-kind, an API-sourced invoice has no source page to verify
extracted values against, so `evidence_quality` is always 0.0 and the
decision engine correctly routes every API-sourced invoice to
`REVIEW_REQUIRED` — confirmed consistent with PAY18/PAY19's findings.

## 2d. ERP source (PAY21)

A fourth `source_kind="ERP"`, the inverse direction of INV9's outbound
`OracleErpConnector.submit_invoice()`: `source_connection_id` points at an
`ORACLE_ERP_CLOUD` connection and the schedule pulls invoices FROM Oracle's
own documented AP Invoices REST collection resource
(`fscmRestApi/resources/{version}/invoices`) rather than posting to it. Like
DATABASE-kind and API-kind, each decoded invoice becomes one `ApInvoice` via
`preextracted_fields` — no OCR/AI Extraction stage.

New: `OracleErpConnector.list_invoices(limit, offset, q=None)` — a real GET
with `limit`/`offset`/`onlyData=true`/`expand=invoiceLines` (Oracle's
documented child-resource inlining) and an optional Oracle REST
query-language filter string, passed through verbatim; returns Oracle's own
response shape (`items`/`count`/`hasMore`/`offset`/`limit`) unmodified —
reshaping is `discover_erp_records()`'s job, matching the
`fetch_api()`/`discover_api_records()` division of labor. One new column,
`DocumentScheduleConfig.source_erp_query` (optional filter string) —
deliberately **no** endpoint field, unlike API-kind, since
`source_connection_id` alone (an `ORACLE_ERP_CLOUD` connection) fully
identifies the one resource pulled.

**Deliberate non-reuse of the outbound ERP mapping.** INV9's
`invoice_erp_mapping_service.py` already has a versioned canonical→Oracle
field mapping for the outbound posting direction, and reversing it looked
like the obvious reuse — but its canonical side targets `ApInvoice` ORM
column names (e.g. `"total"`, `"purchase_order"`), which differ from
`document_taxonomies.py`'s `INVOICE_FIELDS` names (e.g. `"total_amount"`,
`"po_number"`) that `build_fields_from_record()` actually understands.
Reversing the outbound mapping would have produced a second, inconsistent
field-naming scheme feeding the same pipeline. Instead, PAY21 uses a small,
explicitly disclosed **fixed** dict (`_ERP_HEADER_FIELD_MAP`/
`_ERP_LINE_FIELD_MAP` in `document_schedule_service.py`) — the same
"only one ERP connector type ships today" disclosure pattern
`invoice_erp_mapping_service`'s own default mapping already uses.

`is_record_source` generalized a third time to
`source_kind in ("DATABASE", "API", "ERP")`, with discovery dispatch still
branching internally by exact kind. `record_identity()` gained a third
branch: `source_path = f"erp:{hash}"`, hashed from `source_erp_query`.
`_validate_and_apply_document_config()` rejects a non-`ORACLE_ERP_CLOUD`
connection for `source_kind="ERP"`. The frontend's ERP-kind Connection
picker reuses the **already-existing** `erpConnections` state in
`ScheduleFormModal.jsx` (fetched since INV11 for the Invoice ERP Posting
auto-post picker) — zero new frontend state/fetch needed.

Live-verified twice. First, backend-only against a real local `http.server`
mock shaped exactly like Oracle's documented Invoices resource (real GET
with `expand=invoiceLines`, not a mocked connector): correct invoice
materialization and a correct idempotent no-op on rerun (`queued: 0`).
Second, through the actual **Connections** and **Create Schedule** UIs
end-to-end: a genuine new `ORACLE_ERP_CLOUD` connection was created via the
real Connections page and Test Connection succeeded against a fresh mock
server; a schedule was created via the real modal (ERP Connection picker,
Source Label, Query Filter, Record Key Column) and saved via a real
`POST /api/schedules` (201); Run Now produced `discovered: 1, queued: 1,
processed: 1, success: 1, failed: 0` and materialized `ApInvoice`
`PAY21-UI-001` (vendor `Acme ERP UI Test`, total `675.25 USD`) matching the
mock server's fixture exactly. Like DATABASE-kind and API-kind, an
ERP-sourced invoice has no source page to verify extracted values against,
so `evidence_quality` is always 0.0 and the decision engine correctly
routed it to `REVIEW_REQUIRED` (issue text: "Below straight-through
threshold (90%): Evidence quality 0.0%...") — confirmed consistent with
PAY18/PAY19/PAY20's findings across all four intake kinds.

## 3. Connector reuse

No new connector code (except PAY19's `ImapConnector` — see §2b, itself
just another implementation of this same interface; PAY20's API-kind
reuses the pre-existing `RestApiConnector.fetch_api()` verbatim; PAY21's
ERP-kind reuses `OracleErpConnector`, extended with one new read method,
`list_invoices()`).
`document_schedule_
service.py` calls the exact `FileConnectorBase` interface every other
part of this codebase already uses (`list_files`, `open_read_stream`,
`move_file`, `file_checksum`, `test_connection`), implemented by
`LocalFileConnector`, `SftpConnector`, `FtpConnector`,
`NetworkFileConnector` (SMB), `AzureBlobConnector`, `AwsS3Connector`, and
now `ImapConnector` (Email Inbox). `list_files()` was extended with a
`recursive: bool` parameter across the four filesystem-hierarchy
connectors (accepted and ignored by the flat/non-hierarchical connectors
— cloud object stores and IMAP mailboxes alike — where "recursive" is
meaningless).

**Resolved (was a known gap):** `LOCAL_FILE` was deliberately removed from
`connection_service.FIELD_SCHEMAS` in an earlier phase (hiding it from the
Connections creation UI), which had the side effect of also breaking
`connection_service.build_connector()` for `LOCAL_FILE`, since that function
unconditionally looks the type up in `FIELD_SCHEMAS` first — in practice a
Document Source schedule could not target `LOCAL_FILE` through the normal
UI/API. Restored on user request: a `LOCAL_FILE` FIELD_SCHEMAS entry (one
field, `base_path`) is back, with an explicit help-text disclosure that this
points at a folder on the **backend server's own filesystem**, not the
browser's machine, and that it's only appropriate when the operator controls
that server (self-hosted/on-prem) — on shared/multi-tenant hosting it would
expose whatever the backend process itself can read. `LocalFileConnector`
(§3 above) already sandboxes every path to stay inside `base_path`; nothing
about the connector itself changed, only its reachability from the
Connections UI and `build_connector()`. Live-verified: a real `LOCAL_FILE`
connection created via the Connections page, a Document Source schedule
pointed at it, and a real invoice PDF sitting in that folder discovered,
OCR'd, extracted, and decisioned exactly like any other STORAGE-category
source — see payment-approval-workflow.md §11 for the run's numbers.

## 4. Confidence/accuracy routing

`document_service.process_document()` gained two new optional parameters:

```python
async def process_document(
    db, doc, *, reprocess: bool = False,
    confidence_threshold: float = 90.0, accuracy_threshold: float = 90.0,
) -> Document
```

Defaults (90/90) exactly match what manual scan/bulk upload/Review Queue
reprocess already hardcoded, so none of those callers changed behavior.
Only the document-schedule dispatch passes a per-schedule configured value.

A document is routed to `NEEDS_REVIEW` (and gets a real `ReviewQueueItem`)
when **any** of:

- overall confidence < `confidence_threshold`, or
- accuracy score < `accuracy_threshold` (accuracy = % of extracted fields
  whose own `validation_status` isn't `"invalid"`), or
- any extracted field's `validation_status == "invalid"` (this includes
  fields the schema expected but the document genuinely didn't contain —
  see the note in §9 about why this makes `NEEDS_REVIEW` the realistic
  outcome for a sparse/partial document even at threshold 0), or
- the business-rule check trips (a "total amount"-shaped field exceeding
  `settings.review_business_rule_total_threshold`).

There is no way to disable the "invalid field" and business-rule checks via
schedule config today — they apply uniformly, same as manual scan.

## 5. Idempotency

`document_schedule_service.needs_processing(entry)` returns `False` when the
registry row's status is `PROCESSED` **or** `REVIEW_REQUIRED` — a file
already sitting unresolved in Review Queue is never re-queued by a later
run. (An earlier version of this only excluded `PROCESSED`, which caused a
real, live-found bug: an unresolved low-confidence file got reprocessed on
every subsequent run, piling up duplicate Review Queue entries. Fixed and
covered by `test_02` in the test suite.)

`FAILED` and `SKIPPED` rows **do** get retried on the next run (their
content is presumably still sitting at the source, unresolved).

**Content-hash change = new version.** If a file's bytes change, its SHA-256
changes, so it gets a *new* `DocumentSourceRegistry` row (a new identity) —
the old row's history is preserved, not overwritten. This is deliberate:
"same path, different content" is treated as a new version of a document per
spec §7, not silently reprocessed in place.

**Duplicate content under a different path.** When
`duplicate_content_behavior == "SKIP_DUPLICATE"` (the default), a file whose
content hash already exists under a *different* path/filename is flagged
`SKIPPED` rather than processed again — `find_duplicate_content()`.

## 6. `processing_run_id` attribution (a real bug found and fixed this session)

`processing_run_id` on `DocumentSourceRegistry` answers "which run actually
acted on this file" — it backs the per-run "Files" breakdown table
(`GET /schedules/{id}/runs/{run_id}/documents`).

**The bug:** every run's discovery loop re-discovers every file still
present at the source, whether or not it ends up doing anything with it
(most files, on most runs, are already `PROCESSED`/`REVIEW_REQUIRED` and get
skipped by `needs_processing()`). The original `upsert_registry_entry()`
unconditionally re-stamped `processing_run_id` to the *current* run's id on
every re-discovery — so a later run that touched nothing still silently
stole a file's history off the run that actually processed it. Found live:
a run's "Files" table went empty after a later run merely re-scanned the
same source.

**The fix:** `upsert_registry_entry()` no longer touches `processing_run_id`
on re-discovery of an existing row. `schedule_service.py`'s discovery loop
now stamps it itself, and only immediately before it's about to act on the
file (queue/skip/fail/mark-unsupported) — i.e., only the run that is
genuinely doing something with the file claims attribution for it. Covered
by `test_07` (unit-level) and `test_09` (full-integration) in the test
suite.

## 7. Scheduler dispatch, run-lock, cancellation, recovery

- **Firing.** APScheduler (already used for the billing scheduler) runs a
  new `schedule_poll` job every 60s (`scheduler_poller.run_poll()`), which
  finds `ACTIVE` schedules whose `next_run_at` has passed and calls
  `execute_schedule()` — closing the "nothing fires automatically" gap
  `schedule_service.py`'s own prior docstring called out.
- **Run-lock.** `execute_schedule()` raises `ScheduleAlreadyRunningError`
  (→ HTTP 409) if `Schedule.status == RUNNING`. This is a DB compare-and-swap
  on a single row, not a distributed lock — correct for one backend process,
  **not** safe across multiple horizontally-scaled backend instances without
  further work (disclosed limitation, not fixed by this feature).
- **Async execution.** The document-schedule branch is fire-and-forget
  (`asyncio.create_task`), matching `document_service.start_processing()`'s
  existing convention — the triggering HTTP request returns immediately with
  the new `RUNNING` run; the frontend polls `GET .../runs/{run_id}` for live
  progress (no WebSocket/SSE infrastructure exists in this codebase; polling
  is the spec's own explicitly-allowed fallback).
- **Cancellation.** `POST /schedules/{id}/runs/{run_id}/cancel` sets
  `ScheduleRun.cancel_requested`. This is checked **only between files**
  (never mid-file) inside the bounded-concurrency loop — a file already
  being processed always finishes cleanly rather than being torn mid-write.
  Queued-but-not-yet-started files are transitioned to `SKIPPED`.
- **Stuck-run recovery.** `recover_stuck_document_schedules()` runs once at
  backend startup (`main.py`'s `lifespan()`) and marks any `RUNNING` run
  left over from a backend crash as `FAILED`, freeing its schedule's lock.

## 8. Disclosed scope limitations

These were explicitly scoped out during the build and are not fixed by this
change:

- **No distributed locking.** The run-lock is a single-row DB flag, correct
  for one backend process. Multi-instance deployments need a real
  distributed lock (Redis, Postgres advisory lock, etc.) before running more
  than one backend replica against document schedules.
- **No WebSocket/SSE.** Live progress is 3-second polling from the frontend.
  Acceptable per spec, but means progress updates are not instantaneous.
- **No Pipeline Builder graphical node** for this workflow; it's configured
  entirely through the Schedules module's own UI.
- **DATABASE-kind, API-kind, and ERP-kind schedules are INVOICE-only** and
  can never reach STRAIGHT_THROUGH — see §2a/§2c/§2d.
- **All four planned non-file intake kinds now exist** — FILE (incl. email
  inbox, §2b), DATABASE (§2a), API (§2c), and ERP (§2d). Nothing further is
  disclosed as outstanding for this workflow at this time.
- **Email inbox archiving is per-message, not per-attachment** — see §2b's
  disclosed quirk (IMAP has no smaller atomic unit than the whole email).
- Cloud connectors (Azure Blob, AWS S3) and SFTP/FTP/SMB were live-verified
  in earlier phases of this build against real Docker-hosted services; this
  particular feature's live verification (Phase 7) used a real Docker SFTP
  server end to end (real invoice PDFs, real OCR, real Review Queue items).

## 9. Automated tests

`backend/tests/test_document_scheduling.py` — 22 cases, run via:

```bash
cd backend && python -m pytest tests/test_document_scheduling.py -v
```

Real (isolated) SQLite DB, real service-layer functions — no mocking of the
code under test, matching this codebase's existing test conventions
(`test_billing_platform.py`). Grouped:

| # | Covers |
|---|---|
| 1 | `upsert_registry_entry()` idempotency on source identity |
| 2 | `needs_processing()` excludes PROCESSED and REVIEW_REQUIRED, not FAILED (regression) |
| 3 | `find_duplicate_content()` — same bytes, different path |
| 4 | `is_supported_file()` extension matching |
| 5 | `transition()` enforces the state machine |
| 6 | `transition()` clears stale `last_error` on a later success (regression) |
| 7 | `processing_run_id` not overwritten by a run that takes no action (regression — §6) |
| 8 | Full run: real OCR + real Custom Extraction Engine process a new file; an unsupported file in the same batch doesn't block it (partial-failure handling) |
| 9 | Second run over an unchanged source: zero new documents, first run's `processing_run_id` attribution preserved (full-integration version of #7) |
| 10 | Confidence-threshold routing against real (not stubbed) OCR/extraction output |
| 11-12 | `_download_with_retry()` — real retry-then-succeed and retry-exhaustion, against a connector made to fail transiently |
| 13 | `request_cancel()` sets the cooperative-cancellation flag |
| 14 | `execute_schedule()` rejects a second run while one is already `RUNNING` |
| 16 | `discover_database_records()` returns one dict per row (PAY18) |
| 17 | `record_identity()` uses the key column when present, else falls back to a content hash (PAY18) |
| 18 | `record_content_hash()` changes when row content changes, not on key order (PAY18) |
| 19 | `build_fields_from_record()` maps scalars/line-items/JSON-unsafe types (`date`, `Decimal`) correctly (PAY18) |
| 20 | Full DATABASE-kind run materializes one `ApInvoice` per row with correct fields, `extraction_model="STRUCTURED_SOURCE"`, no OCR (PAY18) |
| 21 | A DATABASE-kind rerun with unchanged rows is a true no-op (`queued: 0`) (PAY18) |
| 22 | `DocumentScheduleConfigIn` requires `source_query` and `document_type="INVOICE"` for `source_kind="DATABASE"` (PAY18) |
| 23 | `discover_api_records()` returns one dict per decoded API record (PAY20) |
| 24 | `record_identity()` for API-kind uses the key column and is independent of DATABASE-kind's identity hash (PAY20) |
| 25 | Full API-kind run materializes one `ApInvoice` per row with correct fields (PAY20) |
| 26 | An API-kind rerun with unchanged rows is a true no-op (`queued: 0`) (PAY20) |
| 27 | `DocumentScheduleConfigIn` requires `source_api_endpoint` and `document_type="INVOICE"` for `source_kind="API"` (PAY20) |
| 28 | `_validate_and_apply_document_config()` rejects a non-API connection for `source_kind="API"` (PAY20) |
| 29 | `discover_erp_records()` returns one mapped dict per invoice (PAY21) |
| 30 | `record_identity()` for ERP-kind uses the key column and is independent of other kinds' identity hashes (PAY21) |
| 31 | Full ERP-kind run materializes one `ApInvoice` per row with correct fields (PAY21) |
| 32 | An ERP-kind rerun with unchanged rows is a true no-op (`queued: 0`) (PAY21) |
| 33 | `DocumentScheduleConfigIn` requires `document_type="INVOICE"` for `source_kind="ERP"` (PAY21) |
| 34 | `_validate_and_apply_document_config()` rejects a non-ERP connection for `source_kind="ERP"` (PAY21) |

Tests 8-10 exercise the **real** Tesseract OCR engine and the **real**
deterministic Custom Extraction Engine (`app/extraction/custom_engine.py`,
auto-selected whenever an org has no AI Provider connection configured — no
network call, no API key). A small PNG with real drawn text
(`PIL.ImageDraw`) is fed through the actual pipeline; nothing about the OCR
or extraction result is stubbed.

**Why these tests reliably land on `NEEDS_REVIEW`, not `COMPLETED`:** the
automatic pipeline's AI Extraction stage falls back to the comprehensive
~35-field GENERIC invoice schema. A minimal 5-field test document
genuinely doesn't populate the other ~30 fields, which the accuracy/
validation-status check correctly flags as `"invalid"` — this trips
`NEEDS_REVIEW` independently of both numeric thresholds, even at
`confidence_threshold=0`. This is real, correct routing behavior (a
sparse/uncertain extraction should get human review), and the test suite
is written to assert the real, reliable outcome rather than force an
artificial "success" path.

**`LOCAL_FILE` in the automated test suite:** predates §3's fix and still
constructs a real `LocalFileConnector` directly, monkeypatched in wherever
`schedule_service.py`/`document_schedule_service.py` calls
`connection_service.build_connector()` — the connector itself was always
fully real; only the FIELD_SCHEMAS-based secret-lookup indirection
(irrelevant to `LOCAL_FILE`, which has no secrets) was bypassed. Left as-is
since it's a legitimate, lower-setup way to exercise the connector in tests;
`base_path` is now also independently reachable through the real API/UI
(§3), and the live verification there used the real path, not this
monkeypatch.

## 10. Configuration reference

Per-schedule (`DocumentScheduleConfig`, set via the Schedule Form's
"Document Source" type or `POST/PUT /schedules` with a `document_config`
body):

| Field | Default | Notes |
|---|---|---|
| `source_connection_id` | required | Must be a `STORAGE`-category connection this org owns |
| `source_path` | required | |
| `file_pattern` | `"*"` | glob, or `re:<pattern>` for a regex |
| `recursive_scan` | `false` | |
| `batch_size` | 500 | Max files **one run** takes on; excess stays `DISCOVERED` for the next run |
| `max_concurrency` | 3 | Concurrent per-file processing within one run |
| `confidence_threshold` | 90.0 | 0-100 |
| `accuracy_threshold` | 90.0 | 0-100 |
| `archive_after_success` | `false` | Requires `archive_path` if `true` |
| `archive_path` | — | |
| `move_failed_to_review_path` | — | |
| `duplicate_content_behavior` | `SKIP_DUPLICATE` | or `PROCESS_AS_SEPARATE_DOCUMENT` |
| `max_file_size_mb` | 100 | Files over this are marked `FAILED` (`FILE_SIZE_EXCEEDED`), not silently skipped |

Schedule-level (existing `Schedule` fields, reused as-is): `retry_count`,
`retry_interval_seconds` (exponential backoff: `retry_interval *
3^(attempt-1)`, capped at 5 minutes), `frequency`/`cron_expression`,
`timezone`, `environment`.

No new environment variables were introduced by this feature — it reuses
the existing `DATABASE_URL`, `SECRET_KEY`, and connector-specific
credentials already configured per-connection.

## 11. Run / deploy

Nothing new to install beyond what earlier phases of this codebase already
require (Tesseract for OCR, `paramiko`/`smbprotocol`/`boto3`/
`azure-storage-blob` for the relevant connector types — see
`requirements.txt`). After pulling this change:

```bash
cd backend
python scripts/migrate_document_schedules.py   # one-time, idempotent
# restart the backend — APScheduler's schedule_poll job and the startup
# recovery sweep both start automatically from main.py's lifespan()
```

## 12. Remaining/future work

- Distributed run-lock for multi-instance deployments (§8).
- A Pipeline Builder graphical node, if visual pipeline composition is
  wanted for this workflow instead of/alongside the Schedules module UI.
- Broader confidence-routing configurability (e.g., letting a schedule opt
  out of the business-rule/invalid-field checks) if a real customer need for
  that surfaces.
