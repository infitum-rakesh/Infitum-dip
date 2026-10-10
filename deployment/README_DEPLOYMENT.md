# Data Intelligence Platform — Server Deployment

This folder ships alongside `backend/` and `frontend/` as the root of the
`DataIntelligencePlatform/` package. Everything here resolves paths from
its own location (`%~dp0` on Windows, `$SCRIPT_DIR` on Linux) — never the
caller's current directory, never a developer-specific path.

Two parallel script sets are provided, one per platform — same steps,
same safety guarantees (idempotency, never silently deleting data,
PID-exact process targeting, graceful-then-forceful shutdown), adapted to
each OS's conventions:

| Step | Windows | Linux |
|---|---|---|
| Install | `install.bat` | `install.sh` |
| Start | `start.bat` | `start.sh` |
| Stop | `stop.bat` | `stop.sh` |
| Status | `status.bat` | `status.sh` |
| Upgrade | `upgrade.bat <path>` | `upgrade.sh <path>` |
| Uninstall | `uninstall.bat` | `uninstall.sh` |

The rest of this document shows the Windows command; the Linux
equivalent is always the same filename with `.sh` instead of `.bat`, run
as `./script.sh` (after `chmod +x *.sh` once, or they're already
executable if extracted from the release archive).

## 1. Architecture — read this first

**One process** (`uvicorn app.main:app`) serves the REST API, the built
React frontend (static files, mounted directly by FastAPI), the embedded
scheduler, and the embedded async worker pools. There are **no separate
Backend / Worker / Scheduler operating-system processes** — that's a
deliberate, existing architectural choice in this codebase (see
`backend/app/execution/pipeline_executor.py`'s own docstring), not a
simplification made for this installer. status.bat/status.sh report
Worker/Scheduler health as **real, in-process subsystem status** (via
`GET /api/ops-status`), clearly labeled as such.

A consequence: **`WORKER_COUNT` in `.env` should stay at `1`.** Setting
it above 1 makes uvicorn fork multiple OS processes, and this app keeps
some state (SQL Workspace's query-cancellation registry, in-flight
background-task tracking) in one process's memory — a second worker
would have its own, disconnected copy of that state. `install.bat`/
`install.sh` warn about this if you try to raise it.

**Alembic is listed in `requirements.txt` but is not used anywhere in
this codebase.** Schema evolution is a set of small, idempotent,
hand-written scripts in `backend/scripts/migrate_*.py`, all run in the
correct order by the single entrypoint `backend/scripts/run_all_migrations.py`
(called by install/upgrade on both platforms). This is a real, disclosed
architectural choice for this release — adopting Alembic properly is a
reasonable future improvement, out of scope here. **Known gap, disclosed
rather than hidden:** every `migrate_*.py` script is hardcoded to
`sqlite3` — `run_all_migrations.py` actually runs them for a SQLite
deployment, but for an *existing* Postgres/MySQL database that predates
a specific feature, it prints a clear disclosure and exits 0 rather than
pretending to migrate it. A brand-new Postgres/MySQL database is
unaffected — `init_db()`'s `create_all()` builds the fully current
schema on first startup regardless of database engine.

## 2. System requirements

**Windows:**
- Windows 10/11 or Windows Server 2016+
- Python 3.11 or 3.12 (added to PATH)
- PowerShell (ships with every supported Windows version — used for a
  few things batch can't do cleanly: masked password entry, random
  secret generation, reliable child-PID capture)

**Linux:**
- Any modern distribution with `bash` (3.2+ — the scripts deliberately
  avoid bash-4-only syntax like `${var,,}` for the widest compatibility),
  `python3.11`/`python3.12` + the matching `venv` module (e.g.
  `apt-get install python3.12 python3.12-venv` on Debian/Ubuntu), and
  `curl`. `rsync` is required only for `upgrade.sh`.

**Both platforms:**
- Node.js 20 LTS + npm — only needed if you're building the frontend on
  *this* machine. See "Deploying without Node.js" below for the normal
  production path, which needs none of this on the server.
- ~2 GB disk for the Python virtual environment + dependencies, plus
  storage for uploaded documents and your database

### Deploying without Node.js

`frontend/dist/` is a set of static files (HTML/JS/CSS) — nothing about
running the app requires Node.js or npm on the server. The intended
release workflow is: build the frontend **once**, on a developer machine
or CI runner that has Node.js (`npm ci && npm run build` inside
`frontend/`), then ship the resulting `frontend/dist/` folder as part of
the deployment package alongside `backend/` and this `deployment/`
folder. `install.bat`/`install.sh` check for `frontend/dist/index.html`
first: if it's already there, they skip the entire Node.js check and
build step and print that they're using the pre-built bundle — the
target server never needs Node.js/npm installed at all. Only fall back
to installing Node.js on the server if you intend to build from source
there instead.

## 3. Database prerequisites

- **SQLite** (default): none — a file (`backend/dip.db`) is created
  automatically.
- **PostgreSQL / MySQL** (recommended for production): a reachable
  server, a database, and a user with `CREATE TABLE`/`ALTER TABLE`
  rights on it. The installer's wizard collects host/port/db/user/
  password.
- **SQL Server**: NOT supported as *this platform's own* database (no
  async driver is installed in this build). It can still be used as a
  Bring-Your-Own-Database *tenant* target, configured from inside the
  running app later — that's a separate, per-customer feature.

## 4. OCR / Tesseract installation requirements

This app calls the real `tesseract` binary via `pytesseract` — it does
**not** ship or install Tesseract itself. Install it separately:

- **Windows**: `https://github.com/UB-Mannheim/tesseract/wiki`
  (installer), ensure `tesseract.exe` is on PATH (or set `TESSERACT_CMD`
  in `backend/.env` to its full path).
- **Linux**: your distribution's package, e.g.
  `sudo apt-get install tesseract-ocr tesseract-ocr-eng` on
  Debian/Ubuntu, or `sudo dnf install tesseract` on Fedora/RHEL.

Additional language packs (`hin`, `kan`, `tam`, etc.) install as
`.traineddata` files into Tesseract's `tessdata` directory — on Windows
see `https://github.com/tesseract-ocr/tessdata_fast`; on Linux, install
the matching package (e.g. `tesseract-ocr-hin`, `tesseract-ocr-kan`) or
drop the `.traineddata` file into `/usr/share/tesseract-ocr/*/tessdata/`.
`validate_config.py` and `status.bat`/`status.sh` all report exactly
which languages are actually installed on this server right now (a live
subprocess call, not a cached value); the app never silently falls back
to English if a requested language model is missing — it raises a clear
error instead.

## 5. AI provider configuration

AI providers (OpenAI / Anthropic / Ollama) are configured as
**Connection rows inside the running app** (Settings → Connections),
not via `.env`. There's nothing for this installer to collect up front.

### 5a. AI Connection Subscription & organisation-level access

Two independent ownership shapes exist for an AI Provider connection, both
enforced by the same server-side check on every real AI call — picking a
connection in a UI dropdown is never sufficient on its own, not even for an
org admin:

- **Customer-owned** — an org creates its own connection with its own API
  key. No Seller billing relationship is involved; access is governed purely
  by that connection's `is_public` flag or an explicit per-user/role grant
  (Connections page → **Manage AI Access**). There is no "org admin" or
  "connection owner" bypass — an admin who created a connection still needs
  to either make it public or grant themselves access before they (or anyone
  else in the org) can use it for a real extraction/SQL-assist call.
- **Seller-managed** — a connection with no `organization_id` that the
  Seller sells to customer orgs as a subscription (Seller Admin → Purchased
  Modules → Configure the `AI_CONNECTION` module for a customer, setting
  `max_tokens` / `token_period` / `ai_provider_bundle` / `ai_rate_limit` /
  `ai_concurrency_limit`). Holding an active subscription is **not** enough
  by itself — per spec, "a subscription must not automatically grant every
  user permission." The connection also needs either `is_public` or an
  explicit grant before any user in that org can use it, same as above.

No new `.env` variables exist for this feature — every limit above is
configured per-customer through the Seller/org-admin UI, not environment
configuration. The one schema migration this feature needs
(`backend/scripts/migrate_ai_connection.py`, adds `connections.is_public`
and the new `ai_quota_reservations`/`ai_token_usage_events`/
`ai_connection_permissions`/`ai_connection_defaults` tables) runs
automatically as part of `run_all_migrations.py`'s existing `migrate_*.py`
auto-discovery — no separate install step.

**SQLite version requirement.** Real token-usage/quota enforcement
(`ai_quota_service.check_and_reserve()`) uses an atomic
`INSERT ... SELECT ... WHERE ... RETURNING` statement so that concurrent
requests against the same connection's concurrency/rate/token-budget limits
cannot race past each other — this needs **SQLite 3.35.0 or newer** (March
2021) for `RETURNING` support. Check with `python3 -c "import sqlite3;
print(sqlite3.sqlite_version)"`; anything from a reasonably current Python
3.9+ install already clears this. A production deployment on Postgres is
unaffected (Postgres has supported `RETURNING` since 8.2).

## 6. Storage configuration

`UPLOAD_STORAGE_PATH` in `.env` (default `./uploads`, i.e. a sibling of
this README) is where uploaded documents/pipeline files land.
`MAX_UPLOAD_SIZE_MB` bounds individual file size. `validate_config.py`
verifies this directory (and `logs/`) is actually writable, not just
that the path string is set.

## 7. Installation

```
install.bat                    (Windows)
./install.sh                   (Linux)
```

Interactive wizard: pre-flight checks → Python venv → backend deps →
frontend deps + production build (skipped if `frontend/dist` is already
present — see "Deploying without Node.js" above) → directories → `.env`
(DB type, credentials, secret key, worker count) → `validate_config.py`
→ `run_all_migrations.py` → Seller Admin creation → health check →
summary. **Safe to rerun** — every step is idempotent or skips cleanly
if already done (see §11 below).

## 8. Database setup

Handled inside install.bat/install.sh (see §3, §7). To reconfigure the
database later, edit `backend/.env`'s `DATABASE_URL` directly, then run:
```
.venv\Scripts\python backend\scripts\validate_config.py        (Windows)
.venv\Scripts\python backend\scripts\run_all_migrations.py

.venv/bin/python backend/scripts/validate_config.py             (Linux)
.venv/bin/python backend/scripts/run_all_migrations.py
```
`validate_config.py` also accepts `--db-only` to run just the database
connectivity check (what `status.bat`/`status.sh` use for their
Database row).

## 9. Seller Admin creation

A default Seller Portal login (`seller@platform-admin.com` /
`Seller@1234!`) is auto-seeded on every backend startup if it doesn't
already exist — you always have *a* way in, out of the box. Both
installers also offer to run `backend/scripts/create_seller_account.py`
interactively, which lets you set your **own** email/password (hidden
input, never echoed or logged) instead. Password requirements: 8+
characters, upper, lower, digit, special character.

Re-running the creation script (or the installer) against an email that
already exists is a safe no-op unless you explicitly pass
`--reset-password` — it will never create a duplicate account.

## 10. Starting the application

```
start.bat                      (Windows)
./start.sh                     (Linux)
```
Resolves everything from its own folder, refuses to start a second copy
if one is already running (checks `run/backend.pid`), launches
`uvicorn` with no `--reload` and `WORKER_COUNT` workers, and prints the
URLs. On Linux it's launched via `nohup` + `disown` so it keeps running
after the shell that started it exits — no systemd unit is required,
though wrapping `start.sh`/`stop.sh` in one is a reasonable alternative
if your distribution standardizes on systemd for service management.

## 11. Stopping the application

```
stop.bat                       (Windows)
./stop.sh                      (Linux)
```
Targets the exact PID start.bat/start.sh recorded (never a generic
window-title/process-name match). Tries a graceful stop first (Windows:
`Stop-Process`; Linux: `SIGTERM`) — FastAPI's own shutdown handler stops
the scheduler cleanly — and only force-kills (`taskkill /F` / `SIGKILL`)
if it doesn't exit within 10 seconds.

## 12. Status checking

```
status.bat                     (Windows)
./status.sh                    (Linux)
```
Reports Backend (PID/port), Scheduler (real, in-process — via
`/api/ops-status`), OCR (real Tesseract check via `/ocr-health`),
Database (real connectivity check via `validate_config.py --db-only`),
and Storage (directory presence).

## 13. Logs

```
logs/backend.log      — everything (rotated, 10MB x 5 backups by default)
logs/scheduler.log    — just the 6 in-process scheduler jobs' activity
logs/worker.log       — just the pipeline/bulk-migration/schedule executors
logs/uvicorn.out.log  — raw stdout from the uvicorn process itself
logs/uvicorn.err.log  — raw stderr from the uvicorn process itself
```
scheduler.log/worker.log are real, separate files (Python's logger
hierarchy, not fabricated) even though they come from the same process
as backend.log — see §1. Rotation size/count are `LOG_MAX_BYTES`/
`LOG_BACKUP_COUNT` in `.env`.

## 14. Backup

This installer does not automate backups — back up these three things
on whatever schedule fits your data:
- **Database**: `backend/dip.db` (SQLite) or your PostgreSQL/MySQL
  server's own backup tooling.
- **Configuration**: `backend/.env` (contains your `SECRET_KEY` — losing
  it invalidates every issued session token).
- **Document storage**: the `uploads/` directory.

Both upgrade scripts take their own timestamped backup of `.env` and (if
SQLite) `dip.db` into `backup/` before touching anything, as a safety
net — that is not a substitute for your own regular backup schedule.

## 15. Upgrade

```
upgrade.bat <path-to-extracted-new-package>      (Windows)
./upgrade.sh <path-to-extracted-new-package>     (Linux)
```
Backs up config/DB → stops the app → replaces `backend/app`,
`backend/scripts`, `frontend/dist`, `requirements.txt` from the new
package (Windows: `robocopy /MIR`; Linux: `rsync -a --delete` — both an
exact mirror, so files removed in the new release are removed here too)
→ `pip install` → `run_all_migrations.py` → restarts → health check.
**Never overwrites** `backend/.env`, `dip.db`, `uploads/`, or `logs/`.
`upgrade.sh` requires `rsync` on PATH; it errors clearly if missing
rather than silently falling back to a non-mirroring copy.

## 16. Uninstall

```
uninstall.bat                  (Windows)
./uninstall.sh                 (Linux)
```
Stops the running service. **Defaults to keeping** your database,
uploads, and logs — deletion requires typing `YES` to confirm.
Optionally removes the Python virtual environment.

## 17. Troubleshooting

- **Installer fails at a step**: the error names the step, the reason,
  and tells you to fix it and rerun — rerunning never duplicates
  users/roles/tables/seed data (every step is idempotent).
- **Backend won't start**: check `logs/backend.log` and
  `logs/uvicorn.err.log`.
- **"Configuration INVALID" / FAIL lines**: `validate_config.py`'s
  output tells you exactly which of Secret Key/DB/storage/OCR/origins
  failed and why, without ever printing a password.
- **Port already in use**: another process (or a previous, un-stopped
  instance) is on the configured `PORT`. Run status.bat/status.sh first.
- **Linux: `bad substitution` or similar shell error**: you're likely
  running the scripts with `sh script.sh` instead of `bash script.sh` /
  `./script.sh` — they require bash (even an old 3.2, which macOS and
  some minimal containers ship), not a bare POSIX `sh`.

## 18. Firewall

| Component | Port | Expose publicly? |
|---|---|---|
| Backend (API + frontend) | 8080 (configurable via `PORT` in `.env`) | Yes, behind HTTPS (§19) |
| Database | driver-specific (5432 Postgres, 3306 MySQL, etc.) | **No** — keep it on a private network/VPN; only the backend should reach it |

```
Internet → HTTPS → Reverse Proxy → Backend (8080) → Database (private)
```

## 19. HTTPS

This installer does not terminate TLS itself — put a reverse proxy
(IIS, nginx, Caddy, or your load balancer) in front of port 8080 and
terminate HTTPS there. Never expose port 8080 directly to the internet
over plain HTTP: credentials, uploaded documents, OCR text, AI
extraction results, and session tokens must never cross the network
unencrypted.

## 20. Domain configuration

The application never hardcodes a domain. Set `FRONTEND_BASE_URL` /
`BACKEND_PUBLIC_BASE_URL` in `.env` to wherever this server is actually
reached — `localhost`, a bare server IP, an internal hostname, or a
real public domain all work identically; these two values are only used
to build correct links in outbound emails and SSO URLs.

## 21. CORS

`ALLOWED_ORIGINS` in `.env` is a comma-separated allow-list of browser
origins permitted to call the API. It is never `["*"]` for this
authenticated API — set it to the exact origin(s) your frontend is
served from.

## 22. Upload security limits

`MAX_UPLOAD_SIZE_MB` in `.env` bounds individual file size (default
25 MB). Allowed file types (PDF, PNG, JPG/JPEG, TIFF/TIF, BMP, DOCX,
XLSX, CSV, TXT) are enforced server-side in
`backend/app/services/document_service.py`, not just hinted at in the
UI.

## 23. ZIP Batch Upload (Document & Invoice Intelligence)

A user can upload one `.zip` archive of many documents at once (via
"Upload ZIP" on the Documents page or the Invoice Inbox). The archive is
validated, safely extracted (ZIP-slip / decompression-bomb / encrypted-
archive protection — see `backend/app/services/zip_security_service.py`),
deduplicated against existing documents, and every valid file is processed
through the **same** per-document pipeline (`document_service.process_document()`)
that a single manual upload uses — there is no second pipeline engine and no
one-scheduler-job-per-file. All limits below are enforced server-side, not
just hinted at in the UI; see `backend/.env.example` for the full list with
current defaults:

| Variable | Default | Purpose |
|---|---|---|
| `ZIP_UPLOAD_ENABLED` | `true` | Kill switch for the whole feature |
| `MAX_ZIP_SIZE_MB` | `10240` (10 GB) | Whole uploaded archive |
| `MAX_EXTRACTED_SIZE_MB` | `51200` (50 GB) | Running total across all extracted entries |
| `MAX_FILES_PER_ZIP` | `10000` | Entry count per archive |
| `MAX_SINGLE_FILE_SIZE_MB` | `5120` (5 GB) | Per extracted entry |
| `MAX_ZIP_DEPTH` | `20` | Directory nesting depth of an entry's path |
| `MAX_COMPRESSION_RATIO` | `100` | Uncompressed:compressed ratio flagged as a decompression bomb |
| `ALLOW_NESTED_ZIP` | `false` | A `.zip`-within-the-`.zip` is rejected unless enabled |
| `ZIP_RETENTION_DAYS` / `EXTRACTED_FILE_RETENTION_DAYS` | `30` | Scheduled cleanup window for assembled/extracted files |
| `UPLOAD_CHUNK_SIZE_MB` | `8` | Client chunk size for the resumable upload |
| `DOCUMENT_WORKER_COUNT` / `OCR_CONCURRENCY` / `AI_CONCURRENCY` | `4` / `4` / `4` | Shared process-wide caps — manual upload, bulk upload, document schedules, and ZIP batches all honor the same one |

**Scaling beyond a few hundred documents per batch.** This deployment's own
live verification (ZB12) ran real batches through the full pipeline; the
architecture that made that work also holds at much larger scale, without
code changes:

- **Bounded worker pool, not unbounded fan-out.** A batch never spawns one
  task per file up front — `document_batch_service.py` claims `effective_workers`
  (default 4) rows at a time via an atomic `UPDATE ... WHERE status IN (...)`
  claim (`claim_task`), the same portable pattern used by Bulk Object
  Migration. A 5,000-file batch and a 20-file batch place the same constant
  memory pressure on the process; only wall-clock time differs.
- **Streaming extraction, not load-then-process.** The ZIP is walked and
  extracted entry-by-entry to disk (`zip_security_service.safe_extract()`),
  never fully materialized in memory, and `DocumentBatchFile` rows are
  bulk-inserted in `BATCH_INSERT_SIZE` (default 500) chunks — a 10,000-entry
  archive performs ~20 insert statements, not 10,000.
- **Crash-safe by construction.** Every claimed file's progress lives in the
  database (`DocumentBatchFile.status`, `last_heartbeat`), not in process
  memory. If the backend restarts mid-batch, `recover_stuck_document_batches()`
  (run at startup and every 2 minutes) requeues anything left claimed past a
  missed-heartbeat threshold and resumes — already-completed files are never
  reprocessed. This was live-verified in ZB8 with a real kill-and-restart.
- **One real global concurrency cap.** `DOCUMENT_WORKER_COUNT` bounds actual
  concurrent OCR/AI work across the whole process regardless of how many
  batches, manual uploads, or document schedules are in flight simultaneously
  — a large batch queues fairly against everything else rather than starving
  it or exceeding the host's real resource limits.
- **What actually changes at 5,000+ scale** is wall-clock time (bounded by
  `DOCUMENT_WORKER_COUNT` × per-document OCR/AI latency, the same cost a
  5,000-document manual-upload run would pay) and disk space for the
  extracted tree (bounded by `MAX_EXTRACTED_SIZE_MB`) — not memory, not
  database row-count behavior, and not a different code path. Raising
  `MAX_FILES_PER_ZIP` past its 10,000 default is safe from an architecture
  standpoint; it is a deliberately conservative default, not a hard ceiling
  the design breaks at.
- **Known, disclosed limitation:** a batch with thousands of queued files
  will occupy the shared `DOCUMENT_WORKER_COUNT` semaphore for a
  correspondingly long time, so a concurrent interactive single-document
  upload queues behind it (FIFO-ish, not starved — batch tasks do release
  between claims). No reserved-capacity carve-out for interactive uploads
  exists yet; size `DOCUMENT_WORKER_COUNT` and batch scheduling around this
  if concurrent interactive use during a very large batch matters to your
  deployment.
