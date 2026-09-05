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

## 3. Connector reuse

No new connector code. `document_schedule_service.py` calls the exact
`FileConnectorBase` interface every other part of this codebase already
uses (`list_files`, `open_read_stream`, `move_file`, `file_checksum`,
`test_connection`), implemented by `LocalFileConnector`, `SftpConnector`,
`FtpConnector`, `NetworkFileConnector` (SMB), `AzureBlobConnector`,
`AwsS3Connector`. `list_files()` was extended with a `recursive: bool`
parameter across the four filesystem-hierarchy connectors (accepted and
ignored by the two flat cloud-object connectors, where "recursive" is
meaningless).

**Known gap, disclosed, not fixed by this feature:** `LOCAL_FILE` was
deliberately removed from `connection_service.FIELD_SCHEMAS` in an earlier
phase (hiding it from the Connections creation UI — a real security
decision). That removal has the side effect of also breaking
`connection_service.build_connector()` for `LOCAL_FILE`, since that function
unconditionally looks the type up in `FIELD_SCHEMAS` first. In practice this
means a Document Source schedule **cannot** target `LOCAL_FILE` through the
normal UI/API today — only SFTP, FTP, SMB, Azure, and S3 sources are usable
end to end. This was already true before this feature (nothing else could
create a `LOCAL_FILE` connection either) and is out of scope to fix here; the
automated test suite (§9) works around it locally by constructing a
`LocalFileConnector` directly, which is legitimate for tests but not
something the real API surface enables.

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
- **`LOCAL_FILE` connections are unusable end-to-end** — see §3.
- **No Pipeline Builder graphical node** for this workflow; it's configured
  entirely through the Schedules module's own UI.
- Cloud connectors (Azure Blob, AWS S3) and SFTP/FTP/SMB were live-verified
  in earlier phases of this build against real Docker-hosted services; this
  particular feature's live verification (Phase 7) used a real Docker SFTP
  server end to end (real invoice PDFs, real OCR, real Review Queue items).

## 9. Automated tests

`backend/tests/test_document_scheduling.py` — 14 cases, run via:

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

**`LOCAL_FILE` in tests vs. production:** because of the gap in §3, tests
construct a real `LocalFileConnector` directly and monkeypatch it in
wherever `schedule_service.py`/`document_schedule_service.py` would
otherwise call `connection_service.build_connector()` — the connector
itself is fully real; only the FIELD_SCHEMAS-based secret-lookup
indirection (irrelevant to `LOCAL_FILE`, which has no secrets) is bypassed.
This is a test-only affordance and does not change, and should not be read
as reversing, the production decision to keep `LOCAL_FILE` uncreatable via
the API.

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
- Restoring a `LOCAL_FILE` FIELD_SCHEMAS entry, if the product decision from
  the earlier phase that removed it is ever revisited — would need its own
  explicit sign-off, not a side effect of this feature.
- A Pipeline Builder graphical node, if visual pipeline composition is
  wanted for this workflow instead of/alongside the Schedules module UI.
- Broader confidence-routing configurability (e.g., letting a schedule opt
  out of the business-rule/invalid-field checks) if a real customer need for
  that surfaces.
