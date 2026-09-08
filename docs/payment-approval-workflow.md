# Payment Invoice Approval Workflow

A configurable, multi-step approval/peer-review layer that sits between
Invoice Intelligence's decision engine and ERP posting: before a
`STRAIGHT_THROUGH` invoice can be posted, it is routed through
organization-defined approval rules — sequential or parallel approvals,
an optional peer-review pass, SLA reminders and escalation — and only a
real human sign-off (or a rule that genuinely requires none) lets it
proceed. This document describes what was built, how it fits into the
existing Invoice Intelligence pipeline, how to configure and operate it,
and its real, disclosed limitations.

## 1. What this is (and isn't)

This is **not** a new invoice pipeline. It is a gate: `invoice_decision_
service.decide()` already produces a `STRAIGHT_THROUGH` verdict from the
existing extraction/validation/confidence machinery (Invoice
Intelligence, built earlier); this feature intercepts that verdict when
the org holds the `PAYMENT_APPROVAL` license, evaluates the invoice
against the org's own approval rules, and — if a rule matches and
requires approval and/or peer review — creates a real, stateful
`ApprovalWorkflow` instead of letting the invoice go straight to
`READY_TO_POST`. `invoice_erp_service.post_invoice()` refuses to post
any invoice with a PENDING/IN_PROGRESS or REJECTED approval workflow
attached — the gate is enforced at the posting boundary, not by trusting
callers to check first.

What it deliberately does **not** do:
- Reimplement invoice extraction, validation, or the decision engine —
  it consumes their output.
- Model a manager hierarchy — `ApproverType` is ROLE / USER / DEPARTMENT
  / POOL, not MANAGER; there is no reporting-chain concept anywhere in
  this codebase's `User` model. Disclosed limitation, not a fabricated
  capability.
- Use Celery/Redis or any external job queue — reminders, escalation,
  and the notification outbox are all in-process APScheduler jobs, the
  same pattern every other background job in this codebase uses.

## 2. Data model

```
ApprovalRule (versioned — never mutated in place, republish creates v+1)
 ├── ApprovalRuleCondition   (SUPPLIER / BUSINESS_UNIT / DEPARTMENT / COST_CENTER
 │                            / COMPANY / PAYMENT_METHOD / SOURCE_SYSTEM, EQ/NEQ/IN/NOT_IN)
 ├── ApprovalRuleStep        (APPROVAL | PEER_REVIEW, ordered, approver_type snapshot,
 │                            sla_hours / reminder_after_hours / escalation_after_hours,
 │                            optional escalation_approver_type/_role_id/_user_id)
 └── ApprovalRuleAuditLog    (CREATED / VERSION_PUBLISHED / ACTIVATED / DEACTIVATED / SIMULATION_RUN)

ApprovalWorkflow                 (one per approval attempt against one invoice;
 │                                 a REJECTED invoice resubmitted creates a NEW row —
 │                                 full history is kept, never overwritten)
 ├── ApprovalWorkflowStep        (one row per APPROVAL step instance, real FK → ap_invoices.id)
 ├── PeerReviewStep              (deliberately separate table — peer review is an
 │                                 acknowledgement/flagging pass, not an approve/reject gate)
 └── ApprovalWorkflowAuditLog    (CREATED / STEP_ASSIGNED / STEP_APPROVED / STEP_REJECTED /
                                   STEP_SKIPPED / PEER_REVIEW_ASSIGNED / PEER_REVIEW_REVIEWED /
                                   PEER_REVIEW_FLAGGED / ESCALATED / REMINDER_SENT / COMPLETED /
                                   CANCELLED / RESOLUTION_FAILED)

ApprovalNotificationOutbox   (queued/retried email + Bell dual-channel; UNIQUE on
                              (workflow_id, step_id, step_kind, notification_type, channel)
                              — idempotent by construction, an approver is never emailed twice
                              for one assignment)
```

All tables are `TenantBase` (org-scoped, no cross-boundary `ForeignKey()`
wrapper on `organization_id`, but real FKs *within* the tenant boundary —
e.g. `ApprovalWorkflow.invoice_id → ap_invoices.id` — since both tables
live in the same tenant DB). `User`/`Role` are `PlatformBase`; every
lookup against them opens its own platform session
(`approver_resolution_service.py`'s own module docstring explains why).

Rule versioning follows the same convention as `ApInvoiceValidationRule`
and `ColumnMappingVersion`: **publishing never mutates a published row.**
Editing a rule in the UI always calls the same `publish_rule()` endpoint,
which creates version N+1 and flips the prior version's `is_current` off.
A platform-default rule (`organization_id IS NULL`) that an org
deactivates gets a real org-scoped **override** row at version 1 — the
platform default itself is never touched.

Migration: models registered directly in `main.py`'s `create_all()` path
(new tables, no `ALTER TABLE` needed — this feature added no columns to
pre-existing tables).

## 3. Rule engine

`services/approval/rule_engine_service.py` — pure, side-effect-free
evaluation:

- **Candidate resolution**: every currently-published, `ACTIVE` rule
  visible to the org (its own rows, plus platform-default rows merged by
  name, org override winning on a name collision).
- **Matching**: `min_amount`/`max_amount`/`currency`/`invoice_type` plus
  any number of `ApprovalRuleCondition` rows, each producing a
  `ConditionCheck` in the trace (field, operator, expected, actual,
  passed, note) — every match or non-match is fully explainable, not a
  boolean.
- **Specificity**: number of checks a rule actually defines (more
  conditions = more specific); the most specific *matching* rule wins.
- **RULE_CONFLICT**: two or more equally-specific matching rules with the
  same priority is a genuine tie — the engine raises rather than picking
  arbitrarily, surfaced to the caller as `conflicting_rule_ids`.
- **Validation** (`validate_rule_definition()`): structural checks run
  before a rule is ever persisted — amount range sanity, currency format,
  `action` vs `approval_required`/`peer_review_required` consistency,
  `approval_count`/`peer_review_count` never exceeding the actual step
  count, duplicate `step_number`s, approver-type-specific required
  fields, and an escalation config that names an `escalation_after_hours`
  with no `escalation_approver_type` (real gap this project's own
  Phase 7 build found and closed — "escalate after 24h to nobody" is
  caught at rule-authoring time, not silently skipped at sweep time).

## 4. Workflow engine

`services/approval/approval_workflow_engine_service.py`:

- **Idempotency guard (PAY16)**: before ever touching the rule engine,
  `create_workflow()` checks whether the invoice already has a workflow
  with status `PENDING`/`IN_PROGRESS`/`APPROVED`. If one exists, it's
  returned as-is (no duplicate row, a `DUPLICATE_SUPPRESSED` audit event
  is logged on it) — safe against a scheduler retry, a duplicate-processed
  source record, or any other repeated trigger for the same invoice.
  Deliberately does **not** treat `REJECTED`/`CANCELLED`/`EXPIRED` as
  "already exists" — a corrected, resubmitted invoice is still expected
  to get a genuinely new workflow (see `ApprovalWorkflow.invoice_id`'s own
  docstring). `invoice_decision_service.decide()` was updated alongside
  this: it only resets the invoice to `APPROVING` when the returned
  workflow is still open, never when the guard hands back an already-
  `APPROVED` workflow — otherwise a duplicate `decide()` call would
  regress an invoice that already reached `READY_TO_POST` back to
  `APPROVING`, silently reopening a question that was already answered.
- **Creation**: runs the rule engine, and on a genuine match materializes
  `ApprovalRuleStep` rows into `ApprovalWorkflowStep`/`PeerReviewStep`
  instances, **snapshotting** `sla_hours`/`reminder_after_hours`/
  `escalation_*` at creation time — editing the rule later never rewrites
  what an already-running workflow's steps mean. Gated on *either*
  `approval_required` or `peer_review_required` (not on `action` alone —
  see §7's real-bug note). No match, or a match needing neither, returns
  `None` — never a fabricated empty workflow.
- **Action semantics** (`act_on_approval_step` / `act_on_peer_review_step`):
  only the step's own `assigned_user_id` may act on it. `REJECTED` is an
  immediate, unconditional short-circuit — the whole workflow terminates
  `REJECTED` right there. `APPROVED`/`REVIEWED`/`FLAGGED` advance their
  own phase independently: `SEQUENTIAL` moves to the next step number and
  resolves+assigns it immediately; `PARALLEL` counts toward
  `*_count_required` and, once met, marks any still-open steps of that
  phase `SKIPPED`. Peer review's `FLAGGED` outcome does **not** block
  completion — it's prominently audited but the count is what gates
  completion, matching the "acknowledgement pass, not a gate" design.
  The workflow reaches `APPROVED` only once both required phases are
  done and neither ended `REJECTED`.
- **Invoice sync**: on a terminal outcome, `_sync_invoice_status()` moves
  the originating `ApInvoice` from `APPROVING` to `READY_TO_POST` or
  `REJECTED` — only ever touching an invoice actually still `APPROVING`,
  defensive against a workflow resolving twice.

## 5. Approver resolution

`services/approval/approver_resolution_service.py` resolves one step's
`approver_type` snapshot down to exactly one `assigned_user_id`:

- **USER**: the named user, if active and in this org.
- **ROLE** / **DEPARTMENT** / **POOL**: every active, in-org candidate,
  tie-broken by **least current open-ASSIGNED-step load** (a real,
  computed count against this org's live `ApprovalWorkflowStep`/
  `PeerReviewStep` rows — not a fabricated round-robin counter), lowest
  user id as the final tiebreaker for determinism.
- **Zero eligible candidates** (a role nobody currently holds, an empty
  pool, a deactivated named user, a department with nobody in it) is a
  real, expected outcome: a `RESOLUTION_FAILED` audit event is written
  and the step stays `PENDING` — never assigned to a fabricated
  placeholder.

The same `resolve()` primitive is reused, unmodified, by
`sla_scheduler_service.py` for escalation-target resolution — escalation
never duplicates the ROLE/USER resolution logic.

## 6. SLA reminders, escalation, and notifications

Two independent APScheduler sweeps (`main.py`, 15-minute interval):

- **Reminder**: a step `ASSIGNED` for ≥ `reminder_after_hours` gets one
  reminder notification. Naturally idempotent — the outbox's own unique
  constraint means a sweep firing again before the step is decided is a
  safe no-op.
- **Escalation**: a step `ASSIGNED` for ≥ `escalation_after_hours` and
  not yet escalated gets its `escalation_approver_type`/`_role_id`/
  `_user_id` resolved via the same resolver above. On success,
  `assigned_user_id` is reassigned, `assigned_at`/`due_at` are reset (a
  fresh SLA window, not the elapsed remainder), and an `ESCALATED` audit
  event is written. On failure (no eligible escalation target),
  `escalated_at` is still stamped — so a permanently-unresolvable
  escalation doesn't retry forever — and the step moves to a genuinely
  stuck `ESCALATED` status signaling a human needs to intervene.

The notification outbox (`notification_outbox_service.py`, 60-second
interval) is a real queued/retried email path — closing a disclosed gap
in the existing `notification_service.create_notification()`, which
sends synchronously inline with no retry, fine for a plain Bell insert
but never safe to reuse as-is for an assignment that must eventually
reach its approver through a transient SMTP outage.

## 7. Real bugs found and fixed during this build

1. **AUTO_APPROVE + peer_review_required silently discarded.**
   `create_workflow()` originally bailed out whenever `rule.action ==
   AUTO_APPROVE`, which silently dropped a legitimate "auto-approve the
   amount gate, but still route it to a peer reviewer" configuration —
   `action` and `peer_review_required` were never mutually exclusive in
   validation, only `action` vs `approval_required` was. Fixed to gate on
   *either* flag being set; regression-guarded by
   `test_create_workflow_auto_approve_action_with_peer_review_still_creates_workflow`.
2. **Notification write-lock contention.** `notify_step_assigned()` opens
   its own platform-DB connection for the Bell write while the caller's
   tenant connection still had an open, uncommitted transaction — on
   SQLite, two connections in the same call stack both wanting the write
   lock surfaced as "database is locked" once ≥2 steps were assigned in
   one call (PARALLEL mode). Fixed by committing after each per-step
   notify so the write lock releases before the next iteration's
   connection needs one.
3. **Cross-tenant invoice data leak (PAY13, the most serious finding).**
   `POST /approvals/workflows` accepted any `invoice_id` with no check
   that it belonged to the caller's own org. Since every org shares the
   same physical database until it individually migrates via
   Bring-Your-Own-Database, this let one org create a workflow pointing
   at another org's real invoice, and the enrichment code that stamps
   `invoice_number`/`vendor_name`/`total`/`currency` onto workflow
   responses would then fetch and leak that data. Fixed at both the
   write side (`create_workflow()` now validates the invoice's
   `organization_id` before doing anything) and the read side
   (`_with_invoice_fields()`/`my_approvals()`'s stamping independently
   re-checks `organization_id` — defense in depth for any pre-existing
   or future bad row). Live-verified with a real two-org attack scenario
   and regression-guarded by three tests
   (`test_create_workflow_rejects_invoice_from_a_different_org`,
   `test_with_invoice_fields_never_stamps_a_cross_org_invoice`,
   `test_with_invoice_fields_stamps_normally_for_same_org_invoice`).
4. **Action-route path-param dishonesty.** The four action routes
   (`.../workflows/{workflow_id}/steps/{step_id}/approve` etc.) carried
   `workflow_id` in the URL but never checked it against the step's real
   `workflow_id` — a mismatched value in the path was silently ignored
   rather than rejected. Not a security hole (org + assigned-user checks
   already gated correctly) but an API-honesty gap; now 404s on mismatch.
5. **No idempotency guard on workflow creation (PAY16).** `create_workflow()`
   had no check for "does this invoice already have a workflow" before
   creating one — a genuine gap against the "a scheduled payment-processing
   job must be idempotent" production requirement (a scheduler retry, a
   duplicate-processed source record, or a double reprocess/revalidate call
   could each create a second `ApprovalWorkflow` row for the same invoice,
   double-notifying approvers). Fixed by checking for an existing
   `PENDING`/`IN_PROGRESS`/`APPROVED` workflow before evaluating any rule,
   returning that existing workflow and logging a `DUPLICATE_SUPPRESSED`
   audit event instead of creating a duplicate — while deliberately still
   allowing a fresh workflow after `REJECTED`/`CANCELLED`/`EXPIRED`, so the
   "correct and resubmit" flow keeps working. A second, subtler bug was
   caught while fixing this: `invoice_decision_service.decide()`
   unconditionally reset the invoice to `APPROVING` whenever a workflow
   came back, which would have **regressed an already-`APPROVED`
   invoice's status back to `APPROVING`** on a duplicate `decide()` call,
   undoing its real `READY_TO_POST` state. Fixed alongside the guard.
   Live-verified through the real `decide()` pipeline (not just the
   engine unit tests): seeded an invoice, ran `decide()` to create and
   fully approve a workflow (invoice → `READY_TO_POST`), then called
   `decide()` again — confirmed exactly one workflow row and the invoice
   status unchanged. Regression-guarded by three tests (idempotent
   repeated call, no second workflow after approval, resubmission after
   rejection still works).
6. **Approval-outcome stats silently dropped by the API schema (PAY17).**
   `schedule_service.py` was extended to classify each INVOICE-typed
   document's real approval outcome (`auto_approved`/`workflows_created`/
   `already_in_workflow`/`rule_conflicts`/`approver_not_found`, read off
   the invoice's own persisted state and its `ApprovalWorkflowAuditLog`
   rows — never re-deriving a decision) and merge the counts into
   `ScheduleRun.document_stats`. The DB row held the real numbers
   correctly, but `schemas/schedules.py`'s `DocumentStatsOut` is a
   declared Pydantic model, not a passthrough dict — the 5 new keys
   weren't listed as fields, so Pydantic silently dropped them on
   serialization and the API always returned 0 for them regardless of
   the real DB value. Caught by browser verification (the seeded values
   3/4/1/1/1 rendered as all-zero tiles), not by the backend test suite,
   which asserts against the ORM row directly and never went through
   this schema. Fixed by adding the 5 fields to `DocumentStatsOut`;
   re-verified in the browser afterward — the same seeded run then
   rendered the real 3/4/1/1/1 correctly.

## 8. RBAC and licensing

- **Module**: `PAYMENT_APPROVAL`, registered in `license_service.
  ALL_MODULES`, `role_service.CATEGORY_MODULE_MAP` ("Payment Approvals"
  category), and the frontend's `MODULES`/`MODULE_META`.
- **Actions**: `VIEW` (reads), `CREATE` (publish a rule / admin-trigger a
  workflow), `EDIT` (rule status toggle), `EXECUTE` (simulate), `APPROVE`
  (act on any step type). Enforced via `require_action(action,
  module="PAYMENT_APPROVAL")` — a role-tier + module-license check (the
  same mechanism every other module in this platform uses); `APPROVE` is
  granted to `ORG_ADMIN`/`DATA_ENGINEER`/`SUPER_ADMIN`/`SELLER` tiers.
- **Tenant isolation**: every rule/workflow read and write is scoped to
  `organization_id`, with `SUPER_ADMIN` as the only cross-org bypass
  (consistent with every other module's break-glass convention). See §7
  item 3 for the one real gap found and fixed.
- **Known systemic limitation** (not specific to this module): the
  separate custom Role/RolePermission granular matrix (the "Roles"
  management UI) only has real server-side enforcement wired for SQL
  Workspace platform-wide; every other module, including this one, is
  gated by the coarser role-tier check above. This predates and is
  unrelated to this build — disclosed here rather than silently assumed
  fixed.

## 9. Frontend

- **My Approvals** (`/app/payment-approvals`) — the personal worklist:
  every `ASSIGNED` step/peer-review-step for the logged-in user, with
  invoice context (number/vendor/amount/currency), inline Approve, and a
  shared comment modal for Reject/Flag/Review.
- **Approval Rules** (`/app/payment-approvals/rules`) — full rule
  builder: conditions, approval/peer-review phases (mode, count),
  per-step approver picker (real user/role dropdowns, department
  free-text, pool multi-select), SLA/reminder/escalation fields, and a
  **Simulate** dry-run showing the full per-rule evaluation trace
  (matched/not, specificity, reason) plus RULE_CONFLICT detection.
- **Workflows** (`/app/payment-approvals/workflows`) — org-wide,
  read-only visibility into every workflow instance: status, approval/
  peer-review progress, rule matched. Row click opens the **Detail**
  page (`/app/payment-approvals/workflows/:id`): summary card, per-step
  tables (approver, status, comment, timestamps), and a chronological
  audit timeline built directly from the real `ApprovalWorkflowAuditLog`
  rows — nothing synthesized for display.
- **Schedules → Execution History → Run Detail** (`DocumentRunProgress.jsx`,
  outside the `/app/payment-approvals/*` routes but part of this build's
  scheduler integration) — PAY17 added a second "Approval Outcomes" tile
  row (auto-approved/workflows-created/already-in-workflow/rule-
  conflicts/approver-not-found), shown only when the schedule's own
  `document_config.document_type === "INVOICE"` — a non-invoice document
  schedule never renders it, so a receipts-only schedule doesn't show a
  permanent row of zeros.

## 10. Testing

`backend/tests/test_payment_approvals.py` — 36 tests against the real
service layer (real ORM models, real async SQLite DB, no mocking of the
code under test): rule evaluation (matching, specificity, RULE_CONFLICT,
IN/NOT_IN), rule definition validation, rule CRUD/versioning (publish/
republish/status-toggle/platform-override/simulate), approver resolution
(zero-candidate failure, least-loaded tie-break, cross-org ineligibility,
SLA stamping), workflow creation (materialization, no-op case,
RULE_CONFLICT propagation, the AUTO_APPROVE+peer-review regression
guard), action semantics (assigned-user enforcement, reject
short-circuit + invoice sync, SEQUENTIAL/PARALLEL completion, peer-review
FLAGGED-still-completes), the PAY13 tenant-isolation fix (both write- and
read-side), RBAC (role-tier denial, license denial, granted case), and
the PAY16 idempotency guard (repeated-call idempotency, no second
workflow after approval, resubmission after rejection still works), and
the PAY17 scheduled-run approval-outcome classifier (empty when no
invoice materialized, auto-approved/workflow-created/duplicate-
suppressed/rule-conflict/approver-not-found detection, and the two
counters coexisting correctly on one workflow).
`backend/tests/test_document_scheduling.py` adds one more: a real
document_type="INVOICE" schedule run through the actual OCR/extraction
pipeline (Tesseract + the Custom Extraction Engine, no network), proving
the PAY17 wiring doesn't crash and stays internally consistent end to
end through the real scheduler dispatch — not just the isolated
classifier unit tests.
Full backend suite: **112/112 passing** (68 original + 33 PAY14 + 3
PAY16 + 7 PAY17 + 1 PAY17 scheduling), no regressions introduced across
17 phases of this build.

HTTP-layer behavior (route auth wiring, request/response shapes) was
live-verified end to end via the browser during PAY8–PAY12 — real rules
published through the actual form, real workflows driven through
Approve/Reject/Review/Flag clicks, real DB inspection after each action —
not redone as a second, parallel verification pass here. The PAY17
Monitoring UI (§9's Workflows screen is unrelated — see the new
"Approval Outcomes" tile row on a document schedule's Run Detail view)
was live-verified separately: seeded a real `ScheduleRun` with non-zero
counters, confirmed the browser rendered them correctly, found and fixed
the `DocumentStatsOut` schema bug (§7 item 6) live, then re-verified.

## 11. Known limitations (disclosed, not hidden)

- **No manager-hierarchy approver type.** `ApproverType` covers ROLE/
  USER/DEPARTMENT/POOL; there is no reporting-chain concept in this
  codebase's `User` model to build a MANAGER type on top of.
- **RBAC is role-tier, not the granular custom-Role matrix**, for every
  action in this module — a pre-existing, platform-wide characteristic
  (§8).
- **Single escalation target per step** — the schema carries one
  `escalation_approver_type`/`_role_id`/`_user_id`, not an escalation
  chain; a step escalates at most once (`escalated_at` prevents a
  second).
- **In-process scheduling** — reminders/escalation/notification-outbox
  are APScheduler jobs in the same process as the API server, matching
  every other background job in this codebase; no distributed
  job-queue guarantee across multiple backend instances.
- **Scheduled intake now covers FILE and DATABASE sources; API/ERP/email
  remain unbuilt (PAY18).** A `DocumentScheduleConfig` can be
  `source_kind="DATABASE"`: the schedule runs a SQL `source_query`
  against a DATABASE-category `Connection` (via that connector's existing
  `preview_sql()`), and each returned row is mapped straight to the
  `INVOICE` taxonomy's fields (`document_service.build_fields_from_record()`)
  and materialized into a real `Document` + `ApInvoice` — no OCR, no AI
  Extraction stage, since the query's own columns already are the
  structured data (`process_document(..., preextracted_fields=...)`
  skips both stages and records `extraction_model="STRUCTURED_SOURCE"`).
  `DocumentSourceRegistry`'s existing idempotency ledger is reused
  unchanged (a synthetic `source_path=f"query:{hash}"` / `file_name=f"row_{key}"`
  identity, content-hash versioning exactly like a file). `decide()` itself
  is still fully source-agnostic — a DATABASE-sourced `ApInvoice` goes
  through the identical validation/decision/approval-workflow pipeline a
  file-sourced one does. What's still genuinely not built: a scheduled
  path that pulls invoice records from an API/webhook, ERP, or email
  ingestion source.
  **Disclosed architectural limitation found during PAY18's live
  verification:** a DATABASE-sourced (or manually-typed) invoice's
  `evidence_quality` confidence score is always 0% — `bbox` is always
  `None` for these fields because there is no OCR page to point evidence
  at (`invoice_decision_service.compute_confidence_scores()`). Since
  every computed confidence score must clear `STRAIGHT_THROUGH_THRESHOLD`
  (90%) for the STRAIGHT_THROUGH decision, **a DATABASE-sourced invoice
  can never reach STRAIGHT_THROUGH** — it always lands on
  REVIEW_REQUIRED and a human decides, exactly like a manually-structured
  invoice already does. This is honest behavior, not a bug: fabricating
  an evidence score to let these auto-post would defeat the point of the
  confidence model. The frontend's DATABASE-kind schedule form discloses
  this directly next to the SQL Query field. Confirmed live: 8 rows
  ingested end-to-end via Docker Postgres across 3 separate runs (one via
  the real Create Schedule UI), every one landed on
  `ApInvoiceDecision.REVIEW_REQUIRED` with `evidence_quality=0.0` and
  `extractor_confidence=100.0`, and 0 approval workflows were created (as
  expected — workflows only trigger for invoices en route to posting).
- **PAY19 — email inbox (IMAP) intake now covers the remaining FILE-kind
  gap; API/ERP intake remained unbuilt (API is now covered by PAY20 below;
  only ERP remains).** A new `EMAIL_IMAP` connection type
  (`connectors/email/imap_client.py`, real stdlib `imaplib`/`email`, no new
  dependency) implements the exact same `FileConnectorBase` contract
  SFTP/FTP/Network File already do — each matching email attachment is one
  "file" (`list_files`/`open_read_stream`/`move_file`/`file_checksum`).
  Because a `source_kind="FILE"` schedule (§2a's DATABASE kind is
  unaffected) is already generic across any STORAGE-category connection,
  **zero changes were needed in `schedule_service.py`** to support it — a
  Document Source schedule pointed at an `EMAIL_IMAP` connection runs
  through the real, unmodified OCR → AI Extraction → validation →
  decision → Review Queue pipeline, exactly like SFTP/FTP already do.
  Unlike DATABASE-kind above, a FILE-kind email attachment gets a real OCR
  page and therefore a real, non-zero `evidence_quality` — it CAN reach
  STRAIGHT_THROUGH given a clean-enough source document. Disclosed quirk:
  IMAP's atomic unit is the message, not the attachment — archiving after
  success moves the whole source email, so a schedule against inboxes
  with routinely multi-attachment emails should avoid "archive after
  success" or use `max_concurrency=1` (see the connector's own docstring
  for the full disclosure). Confirmed live end-to-end against a real
  Docker `greenmail-standalone` SMTP+IMAP test server: a real email with a
  real PDF invoice attachment was sent via `smtplib`, discovered/
  downloaded/OCR'd/AI-extracted (real Tesseract + real Ollama `llama3.2`)
  through the actual Scheduled Document Intelligence Workflow, correctly
  extracted `invoice_number=PAY19-EMAIL-001`, `vendor_name=Greenmail Test
  Supplies Inc.`, `total=981.0`, landed on `REVIEW_REQUIRED` at 34%
  extractor confidence (genuinely low for a synthetic test document — real
  low-confidence routing, not forced) with a real Review Queue item
  created, and correctly did NOT archive the source email (archiving only
  fires on the `success` outcome bucket, which a REVIEW_REQUIRED document
  never reaches) — confirmed the message stayed in the inbox afterward.
  What's still genuinely not built: a scheduled path for API/webhook or
  ERP intake sources.
- **PAY20 — API endpoint intake; only ERP intake now remains unbuilt.** A
  new `source_kind="API"` (alongside FILE/DATABASE) reuses the
  **already-existing** `BaseConnector.fetch_api()` capability (real,
  paginated HTTP via `httpx` in `RestApiConnector` — the same code the SQL
  Workspace/Source Dataset "preview-api" surface already calls) instead of
  writing new HTTP-fetch code; each decoded response record becomes one
  `ApInvoice` via the same `preextracted_fields` path DATABASE-kind
  already uses — no OCR/AI Extraction stage. Genuinely small new surface
  area: 5 new `DocumentScheduleConfig` columns, one new
  `discover_api_records()` function, and `schedule_service.py`'s
  `is_database_source` generalized to `is_record_source = source_kind in
  ("DATABASE", "API")` (discovery dispatch still branches by exact kind
  internally); `record_identity()`, `_process_one_record()`, and
  `document_service`'s record-materialization path needed no
  DATABASE-specific rework since they already operated on a plain `dict`.
  28/28 automated tests pass (6 new). Live-verified twice: (1) against a
  genuine local mock REST server exercising real multi-page pagination (2
  real HTTP GET requests via the real `fetch_api()`, not a mocked
  connector) — correct data across both pages, correct invoice
  materialization, correct idempotent no-op rerun; (2) through the actual
  **Create Schedule UI** end-to-end — a schedule built via the real modal
  (Connection picker, Endpoint, Method, Pagination Style, Record Key
  Column) saved via a real `POST /api/schedules` (201 Created) and ran
  with `discovered: 2, skipped: 2` — correctly deduping against the
  earlier direct-service-created schedule's already-ingested rows via the
  shared identity hash (both schedules point at the same connection).
  Real bug found and fixed along the way: the running `:8080` backend
  process was serving pre-PAY20 code (this project's recurring
  dual-backend-port staleness gotcha — no `--reload` on that process),
  causing a 422 `"Source Kind must be FILE or DATABASE"` the first time
  the real UI posted an `API`-kind config; restarting the process fixed
  it immediately, confirmed by a clean 201 on retry. Like DATABASE-kind,
  an API-sourced invoice has no source page to verify extracted values
  against, so `evidence_quality=0.0` always and every API-sourced invoice
  correctly routes to `REVIEW_REQUIRED` — consistent with PAY18/PAY19's
  architectural finding. ERP intake is now covered by PAY21 below.
- **PAY21 — ERP-sourced intake; all four planned non-file intake kinds for
  this workflow now exist (FILE incl. email, DATABASE, API, ERP).** A
  fourth `source_kind="ERP"` is the inverse direction of PAY9/INV9's
  outbound `OracleErpConnector.submit_invoice()`: `source_connection_id`
  points at an `ORACLE_ERP_CLOUD` connection and the schedule pulls
  invoices FROM Oracle's own documented AP Invoices REST resource
  (`fscmRestApi/resources/{version}/invoices`) instead of posting to it.
  Genuinely small new surface area: one new connector method
  (`OracleErpConnector.list_invoices()` — real GET with
  `limit`/`offset`/`onlyData=true`/`expand=invoiceLines` and an optional
  Oracle REST query filter), one new `DocumentScheduleConfig` column
  (`source_erp_query` — deliberately no endpoint field, since the
  connection alone identifies the resource), one new
  `discover_erp_records()` function + a small fixed field-mapping dict, and
  `is_record_source` generalized a third time to `source_kind in
  ("DATABASE", "API", "ERP")`. A deliberate design call: reusing PAY9's
  outbound `invoice_erp_mapping_service.py` mapping in reverse was
  considered and rejected — its canonical side targets `ApInvoice` ORM
  column names, which differ from the `INVOICE` taxonomy's field names
  that the scheduler's record-materialization path actually understands;
  reversing it would have introduced a second, inconsistent field-naming
  scheme. A small, explicitly disclosed fixed mapping was used instead.
  34/34 automated tests pass (6 new). Live-verified twice: (1) against a
  genuine local mock server shaped exactly like Oracle's documented
  Invoices resource (real GET with `expand=invoiceLines`, not a mocked
  connector) — correct invoice materialization, correct idempotent no-op
  rerun; (2) through the actual **Connections** and **Create Schedule**
  UIs end-to-end — a genuine new `ORACLE_ERP_CLOUD` connection was created
  via the real Connections page (Test Connection succeeded against a fresh
  mock server), a schedule was created via the real modal (ERP Connection
  picker, Source Label, Query Filter, Record Key Column) and saved via a
  real `POST /api/schedules` (201 Created), and Run Now produced
  `discovered: 1, queued: 1, processed: 1, success: 1, failed: 0`,
  materializing `ApInvoice PAY21-UI-001` (vendor `Acme ERP UI Test`, total
  `675.25`) exactly matching the mock server's fixture. The recurring
  dual-backend-port staleness gotcha (no `--reload` on the `:8080`
  process) was hit and fixed live for a third time in this project — a
  422 `"Source Kind must be FILE, DATABASE, or API"` on the first UI save
  attempt, resolved by killing and restarting the stale process; retry
  succeeded with a clean 201. Like DATABASE-kind and API-kind, an
  ERP-sourced invoice has no source page to verify extracted values
  against, so `evidence_quality=0.0` always and every ERP-sourced invoice
  correctly routes to `REVIEW_REQUIRED` — confirmed consistent with
  PAY18/PAY19/PAY20's architectural finding across all four intake kinds.
  What's still genuinely not built for this workflow: nothing disclosed as
  outstanding at this time.
- **Batch-run monitoring approval counters are per-run only, not
  aggregated across runs or schedules (PAY17 closed the per-run gap).**
  A document_type="INVOICE" schedule's Run Detail view now shows a real
  "Approval Outcomes" tile row (auto-approved/workflows-created/already-
  in-workflow/rule-conflicts/approver-not-found) for that one run. There
  is no cross-run trend view or Monitoring-module rollup of these
  counters yet — only the Schedules → Execution History → Run Detail
  path surfaces them, one run at a time.
