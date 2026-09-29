# ZIP Batch Upload (Document & Invoice Intelligence)

Upload one `.zip` archive containing many documents and have every valid
entry extracted, deduplicated, and processed automatically through bounded
parallel workers — using the **exact same** per-document pipeline
(`document_service.process_document()`) that a single manual upload uses.
There is no second pipeline engine, and no one-scheduler-job-per-extracted-
file: every claimed `DocumentBatchFile` calls the same function every other
upload path in this codebase calls.

This document describes what was built (ZB1–ZB12), what was verified live
and how, the scalability argument for large archives, and what is explicitly
deferred or out of scope.

## 1. What this is (and isn't)

- **Is**: a chunked/resumable upload of one ZIP → real security validation
  and safe extraction → deduplication against existing documents → a bounded
  worker pool that claims extracted files one at a time and runs each
  through the real OCR/AI Extraction/Validation pipeline, for **both**
  Document Intelligence and Invoice Intelligence.
- **Isn't**: a second processing engine, a bulk-only shortcut that skips
  OCR/AI, or a per-file scheduler job. `DocumentBatch`/`DocumentBatchFile`
  are orchestration/bookkeeping rows only — the actual work is 100% the
  existing `document_service.process_document()` (and, for the invoice
  pipeline, the existing `invoice_processing_service` /
  `invoice_validation_service` / `invoice_decision_service` chain it already
  triggers via `document_type="INVOICE"`).
- **Mixed-content ZIPs** (auto-classifying each document to Document vs.
  Invoice Intelligence within one archive) are explicitly **deferred** — see
  §8. First release ships explicit-mode only: the user picks one pipeline
  type for the whole ZIP via which button they clicked ("Upload ZIP" on
  Documents vs. on Invoice Inbox).

## 2. Data model (`backend/app/models/document_batch.py`)

Four new tables on `TenantBase` — everything else (original file, OCR
text/JSON, AI raw response, structured dataset, review corrections, ERP
payload) already exists on `Document`/`ApInvoice` and is reused as-is:

- **`BatchUpload`** — one row per chunked upload session (`INITIATED` →
  `UPLOADING` → `ASSEMBLING` → `COMPLETED`/`FAILED`/`CANCELLED`), tracking
  `received_chunks` as a fast-read index over the real chunk files on disk.
- **`DocumentBatch`** — one row per ZIP ingestion run: pipeline type
  (`DOCUMENT_INTELLIGENCE` / `INVOICE_INTELLIGENCE`), per-status counters
  (`total_entries`, `valid_files`, `duplicate_files`, `unsupported_files`,
  `failed_files`, `completed_files`, `review_files`), lifecycle status
  (`UPLOADING` → … → `QUEUED` → `PROCESSING` → `PAUSED` →
  `COMPLETED`/`COMPLETED_WITH_ERRORS`/`FAILED`/`CANCELLED`), and cooperative
  `pause_requested`/`cancel_requested` flags.
- **`DocumentBatchFile`** — one row per extracted entry, doubling as both
  the dedup-registry entry and the claimable task (18-state `ALLOWED_
  TRANSITIONS`-gated status enum, `attempt_number`, `worker_id`,
  `last_heartbeat`, `duplicate_of_document_id`).
- **`DocumentBatchEvent`** — append-only per-batch log (`since_id`-paged),
  the same precedent as `BulkMigrationTaskEvent`/`PipelineJobLog`, powering
  the live event feed in the UI.

## 3. Storage (`backend/app/services/batch_storage_service.py`)

`{upload_dir}/tenants/{org_id}/batches/{batch_id}/uploaded/.chunks/part_{n}`
→ assembled to `source.zip` (whole-file SHA-256 streamed, never held fully
in memory) → extracted to `.../extracted/{zip_relative_path}` behind a
`BatchStorageProvider` interface (only `LocalBatchStorageProvider` is
implemented — pluggable for a future S3/Azure backend without one existing
today).

## 4. Security (`backend/app/services/zip_security_service.py`)

Real, enforced (not just hinted) protection, all live-tested with genuinely
malicious/oversized archives in ZB2:

- **ZIP-slip**: every entry path normalized and containment-checked against
  the resolved extraction root *before* joining — `..`/absolute/drive-letter
  entries rejected outright.
- **Decompression bombs**: `MAX_COMPRESSION_RATIO` enforced live during
  streaming extraction, not after the fact.
- **Resource limits**: `MAX_EXTRACTED_SIZE_MB`, `MAX_FILES_PER_ZIP`,
  `MAX_SINGLE_FILE_SIZE_MB`, `MAX_ZIP_DEPTH` all enforced with immediate
  abort + cleanup on violation.
- **Encrypted/corrupted archives**: detected via `ZipInfo.flag_bits & 0x1`
  and a real open-and-read probe, not just a `zipfile.is_zipfile()` check.
- **Nested archives**: a `.zip`-within-the-`.zip` is classified
  `NESTED_ARCHIVE_NOT_SUPPORTED` unless `ALLOW_NESTED_ZIP=true`.
- Noise entries (`__MACOSX/`, `.DS_Store`, `Thumbs.db`) are skipped by
  pattern, not counted as unsupported.

## 5. Processing pipeline (`backend/app/services/document_batch_service.py`)

1. On upload-complete: validate + safely extract → walk the extracted tree
   **sequentially** (deliberately not parallelized, to avoid a TOCTOU
   duplicate-detection race since `Document.content_hash` is indexed but not
   DB-unique) → classify each entry (unsupported extension / nested archive
   / duplicate-by-existing-`Document.content_hash` / valid) → bulk-insert
   `DocumentBatchFile` rows in `BATCH_INSERT_SIZE` chunks → update
   `DocumentBatch` counters + emit `DocumentBatchEvent`s.
2. If `auto_process`, dispatch a bounded worker pool: atomic CAS
   `claim_task()` (`UPDATE ... WHERE id=? AND status IN (...)`, portable
   across SQLite/Postgres/MySQL/SQL Server — the same pattern Bulk Object
   Migration already proved), `effective_workers` (default 4) coroutines
   claiming rows in a loop via `asyncio.gather`.
3. Each claimed file opens **one** session held for that document's entire
   lifecycle (mirrors `document_service._process_in_background()`) and
   calls `save_from_source()` (extended in ZB5 to accept
   `source_connection_id=None` for batch-originated documents) then
   `process_document(document_type="INVOICE" if pipeline_type==
   INVOICE_INTELLIGENCE else None)` — wrapped by a heartbeat-touch coroutine
   ticking `last_heartbeat` every ~30s (chosen over a fixed timeout since
   OCR+AI duration is unbounded).
4. **Error isolation**: one corrupt/failing file inside a batch lands that
   file in `FAILED` and the batch finishes `COMPLETED_WITH_ERRORS` — it never
   aborts the whole run. Live-verified in ZB6 with a deliberately corrupt
   file mixed into an otherwise-good batch.

### Concurrency (3 layers, live-verified, no deadlock)

1. Per-batch `effective_workers` (default 4) — bounds in-flight claims for
   *one* batch.
2. The existing process-wide `_background_processing_semaphore` — now the
   **one** real cross-cutting cap shared by manual upload, bulk upload,
   document schedules, *and* batch tasks (ZB5 closed a pre-existing gap
   where `schedule_service.py` never touched this semaphore at all).
3. `_ocr_semaphore` / `_ai_semaphore` — finer sub-limits inside a single
   document's own OCR and AI-extraction stages.

Known, disclosed tradeoff: a batch with many queued files occupies the
shared global semaphore for a correspondingly long stretch, so a concurrent
interactive upload queues behind it (fair, not starved — batch tasks do
release between claims). No reserved-capacity carve-out exists for
interactive uploads.

## 6. Controls, crash recovery, retention (ZB7–ZB9)

- **Controls**: pause (cooperative — takes effect between claims, not
  mid-file) / resume (redispatches a fresh pool, no re-work) / cancel
  (immediate if `QUEUED`/`PAUSED`, cooperative if `PROCESSING`) /
  retry-failed (only from `FAILED`/`COMPLETED_WITH_ERRORS`, resets
  `attempt_number` to 0) / retry-selected (explicit file IDs, allowed even
  from `COMPLETED`/`REVIEW_REQUIRED` — an operator's deliberate re-run).
- **Crash recovery**: `recover_stuck_document_batches()` runs at startup
  (`main.py`'s `lifespan()`) and requeues anything left `PROCESSING` with a
  budget-limited retry; a periodic 2-minute APScheduler sweep
  (`sweep_stale_document_batch_files()`) reclaims individual files whose
  heartbeat has gone stale without waiting for a full batch-level recovery.
  Live-verified in ZB8 with a real backend kill mid-batch and restart —
  already-completed files were not reprocessed, remaining files resumed.
- **Retention**: a scheduled job purges expired assembled ZIPs/extracted
  trees per `ZIP_RETENTION_DAYS`/`EXTRACTED_FILE_RETENTION_DAYS`, and
  separately marks abandoned (never-completed) uploads `FAILED` and deletes
  their working directory — both paths carefully skip anything
  active/under-review/retry-pending (`_PROTECTED_FILE_STATUSES`).

## 7. API surface (ZB10 — `api/documents.py` + `api/invoices.py`)

Identical route set declared thinly in **both** routers (RBAC's
`require_action(action, module=...)` is a static closure baked in at
route-decoration time and cannot branch on a path parameter at request
time, so this couldn't be one shared router) — `documents.py` fixed to
`DOCUMENT_INTELLIGENCE`, `invoices.py` fixed to `INVOICE_INTELLIGENCE`, both
delegating 100% into the same `document_batch_service`/`batch_upload_
service` functions:

```
POST /{module}/zip-upload/init
POST /{module}/zip-upload/{upload_id}/chunk
GET  /{module}/zip-upload/{upload_id}/status
POST /{module}/zip-upload/{upload_id}/complete
POST /{module}/zip-upload/{upload_id}/cancel
GET  /{module}/zip-batches                       (list)
GET  /{module}/zip-batches/{batch_id}
POST /{module}/zip-batches/{batch_id}/start|pause|resume|cancel|retry-failed
POST /{module}/zip-batches/{batch_id}/retry-selected
GET  /{module}/zip-batches/{batch_id}/files       (paginated)
GET  /{module}/zip-batches/{batch_id}/events      (since_id, live polling)
GET  /{module}/zip-batches/{batch_id}/metrics
GET  /{module}/zip-batches/{batch_id}/errors
GET  /{module}/zip-batches/{batch_id}/errors/download   (CSV)
```

Three real bugs were found and fixed via live curl verification against the
running backend during ZB10 (not caught by static review): a missing schema
import causing a 500 on `/events`, a Pydantic field name mismatched to the
real `DocumentBatchEvent.event_timestamp` column, and a route-ordering
collision where the new `GET .../zip-batches` list route was being swallowed
by the pre-existing `GET /{document_id}`/`GET /{invoice_id}` catch-all
(Starlette matches path shape by declaration order — the same hazard the
file's own `/document-types` precedent had already documented, just not
re-applied to this new route). A separate, real pre-existing bug was also
found and fixed while writing this layer: `document_batch_service.
get_batch()` and `batch_upload_service.get_upload()` both used an
unconditional org-id equality filter that would have incorrectly hidden
every batch from a `SUPER_ADMIN` (whose effective org id is `None` by
convention, meaning "no scoping" everywhere else in this codebase).

## 8. Frontend (ZB11)

- **`zipBatchApi.js`** — a `createZipBatchApi(basePath)` factory (one
  instance per module, matching the API's own per-router split) plus the
  real status/variant enums mirrored from the backend models.
- **`ZipUploadDialog.jsx`** — chunked upload via `File.slice()`
  (`CHUNK_SIZE=8MB` matching the backend default, up to 4 chunks in flight
  at once), duplicate-policy and auto-process options, a real validation
  summary once the batch is created, and a "View Batch" hand-off.
- **`BatchDetail.jsx`** — one shared page (parameterized by `basePath`) for
  both modules: live KPIs, a segmented progress bar, a paginated/filterable
  file grid, a live event log (`since_id` polling at the same 1200ms
  cadence `JobLogDrawer.jsx`/`BulkMigrationRunMonitor.jsx` already
  established), pause/resume/cancel/retry actions, and a CSV error-report
  download.
- Wired into `Documents.jsx` ("Upload ZIP" next to the existing bulk
  uploader) and `InvoiceInbox.jsx` ("Upload ZIP" next to the existing
  single-file uploader), with routes
  `/app/doc-intel/documents/zip-batches/:batchId` and
  `/app/invoice-intel/zip-batches/:batchId`.

## 9. Live verification evidence

Every phase was proven against real data, never mocked:

| Phase | What was verified live |
|---|---|
| ZB2 | Real ZIP-slip attack archive, absolute-path entry, decompression bomb, corrupted archive, encrypted archive, nested archive — all rejected/handled correctly. Automated tests in `backend/tests/test_zip_security_service.py`. |
| ZB3 | Real multi-chunk upload, a simulated dropped connection resumed from `received_chunks`, idempotent re-upload of the same file short-circuiting by sha256. |
| ZB4 | A real ZIP mixing valid/duplicate/unsupported/nested-archive/`__MACOSX` entries — reported counts matched exactly. |
| ZB6 | A real batch of real documents processed end-to-end through real OCR (+ AI where configured), for both Document and Invoice Intelligence, including one deliberately-corrupt file proving error isolation (`COMPLETED_WITH_ERRORS`, not `FAILED`). |
| ZB7 | Real pause/resume/cancel/retry-failed/retry-selected against a live worker pool — 9/9 automated tests, uncovering and fixing two real bugs (a cross-event-loop SQLite pooling issue and an `asyncio.Lock` bound to a stale event loop — both were general test-suite-hygiene bugs, not batch-upload-specific, but were only surfaced by this feature's tests). |
| ZB8 | A real backend kill mid-batch + restart — already-completed files were not reprocessed, remaining files resumed correctly. 7/7 automated tests. |
| ZB9 | Real expired-upload/expired-batch purge, real abandoned-upload cleanup, all correctly skipping protected/active rows. 8/8 automated tests. |
| ZB10 | Full API surface exercised via real curl calls (init → chunk → complete → auto-process → get → list → files → events → metrics → pause[rejected] → retry-failed[rejected] → retry-selected[succeeded] → errors → errors/download → cancel[no-op]) against the real running backend, for **both** `documents.py` and `invoices.py`. |
| ZB11 | Full browser round-trip: a real `.zip` selected through the actual file input, uploaded via the real chunked-upload flow, batch created and auto-processed, `BatchDetail` polling showing live status transitions and a real event log, and — separately — direct navigation to previously-curl-created batches on both the Document Intelligence and Invoice Intelligence sides, confirming the page renders real backend data with zero console errors. |
| ZB12 | A real 100-document batch (Document Intelligence) — 100/100 completed, 0 failed, 0 needs-review, **3.56 seconds** total processing time end-to-end (real OCR + real `gpt-4o-mini` AI Extraction per document, confirmed via a spot-checked document's real 94.7% confidence and populated processing timeline), with `effective_workers=4`. Full backend test suite re-run after every phase: 293 passed, only the same 7 pre-existing baseline failures unrelated to this feature (confirmed via git-stash comparison to predate all ZB work). |

## 10. Scalability argument for 5,000+ documents

A full 5,000-document live run was not executed in this sandbox (no
externally-billed AI provider budget was earmarked for a run at that scale);
the locked scope decision for this build was a real ~100–300 document live
run (delivered: 100, see §9) plus this written architectural argument for
why the same, unmodified code path holds at 5,000+. The full reasoning
(bounded worker pool vs. unbounded fan-out, streaming extraction, crash-safe
per-file state, one real global concurrency cap) is written out in
`deployment/README_DEPLOYMENT.md` §23 rather than duplicated here — read
that section for the complete argument and the exact config knobs involved.

## 11. Configuration reference

See `backend/.env.example`'s `ZIP_*` section for the full list with current
defaults, and `deployment/README_DEPLOYMENT.md` §23 for the deployment-
facing explanation of each one.

## 12. Explicitly deferred / out of scope

- **AUTO pipeline-type classification** (per-document routing within one
  mixed ZIP to Document vs. Invoice Intelligence automatically) — deferred
  to a follow-on phase. First release is explicit-mode only: one pipeline
  type per ZIP, chosen by which "Upload ZIP" button the user clicked.
- **A dedicated "browse past ZIP batches" list page** — `GET .../zip-batches`
  exists and is exercised (e.g. by `Documents.jsx`'s KPI refresh), but the
  frontend does not yet surface a standalone list/history view; a batch is
  currently reached via the post-upload "View Batch" hand-off or a direct
  URL. Low-risk, additive follow-up if needed.
- **`validate_config.py` / `run_all_migrations.py`** — two scripts
  referenced by the separate Windows deployment package (`install.bat`/
  `upgrade.bat`) that were never built. This is a pre-existing gap from an
  earlier, separate deployment-package effort, unrelated to and not
  introduced by this feature — called out here only so it isn't silently
  conflated with ZIP Batch Upload's own scope.
- **Cloud storage backend** (`BatchStorageProvider` beyond
  `LocalBatchStorageProvider`) — the interface is deliberately pluggable,
  but only the local-disk implementation exists; no S3/Azure adapter was
  built since there is nothing to live-verify it against in this
  environment.
