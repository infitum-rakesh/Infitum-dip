# Dynamic JSON Extraction, Original Key-Value Preservation & Configurable Mapping — Phase 1 Assessment

**Status: Phase 1 only (repository assessment). No code has been changed.** Per the implementation-control instructions in the master prompt, this document stops here for approval before any Phase 2 work begins.

---

## 1. Existing extraction flow, and exactly why predefined-field mapping affects confidence today

### 1.1 Pipeline stages (`document_service.py::process_document()`)

| Stage | Real or simulated | Notes |
|---|---|---|
| Image Scanner | **Simulated** | `scan_conf = round(rnd.uniform(90, 99.5), 1)` — no real image-quality analyzer exists (`document_service.py:1676-1679`, docstring admits it). |
| OCR | Real | Tesseract (default), 3 cloud providers, or AI-vision fallback — see §2. |
| Language detection | Real | Hybrid script-block + optional FastText detector, two-pass select-best-language flow. |
| AI Extraction | Real | `extraction_service.py` (LLM) or `custom_engine.py` (rule-based, zero AI) — see below. |
| Structured Dataset | Real reshape | Pure reshape of Stage-3 output into `{schema, table, json}`; no independent validation. |
| Validation | Mixed | Real per-field confidence banding + a heuristic "Total" business-rule check; timing still simulated. |

### 1.2 The schema-forcing mechanism — the exact cause of the reported problem

**`extraction_service.detect_schema()`** (`extraction_service.py:518-544`): whenever a `document_type` is known (every scheduled/typed invoice run), it **skips the ad-hoc LLM schema-guess entirely** and returns the fixed canonical taxonomy (`document_taxonomies.build_seed_schema()`). The ad-hoc "let the model propose 5-12 fields from the text" path only runs when `document_type` is absent — which is not the production path for invoices.

**`extraction_service.extract_fields()`** (`extraction_service.py:586-699`): the `EXTRACT_SYSTEM` prompt instructs the model to extract **only** the fields named in the schema — there is no instruction to report fields it finds that aren't in the schema list. A second-pass "did we miss anything" LLM call (`find_gap_fields()`) exists but is **explicitly disabled** for the canonical-taxonomy path (`document_service.py:1208-1219, 1390-1396`) after a real fabrication bug was found: the model proposed a plausible-sounding field name that didn't actually appear in the document, then hallucinated a value for it.

**`custom_engine.py`** (the zero-AI rule-based extractor) is *also* always seeded from the same fixed taxonomy — it has no ad-hoc mode at all (no LLM to ask). It only ever searches the document text **for** schema-named labels (via per-language synonym dictionaries); it never scans for labels that aren't already in the schema.

**Net effect**: for any document with a known `document_type` (the normal production path), the extraction prompt itself only ever asks about ~47 fixed field names. A field present in the document under an unfamiliar label is simply never asked about — it is invisible to the entire pipeline from the first LLM call onward, not "extracted but then dropped."

### 1.3 Where the confidence-vs-schema-coverage coupling lives today (precise, with line numbers)

This is the mechanism the master prompt is specifically targeting, and it was **partially already fixed** in one place but not three others:

1. **`extraction_service.extract_fields()`'s own `overallConfidence`** (lines 650-671) — **fixed**. A prior bug averaged confidence over *every* schema field including ones correctly reported as absent, so an invoice missing many of the ~26 legitimately-optional canonical fields scored low purely from schema coverage. The fix excludes empty/not-found fields from the average. This fix is documented in the code's own comment as a real, previously-shipped bug.

2. **`custom_engine.extract_fields()`'s `overallConfidence`** (lines 505-506) — **NOT fixed**. It only excludes empty *tables*, not empty scalar fields. Every unmatched/not-found field (value="", confidence=0) is still included in the average. This is the one engine path where an unrecognized label directly and currently lowers overall confidence today.

3. **`document_service.py`'s `accuracy_score`** (lines 1917-1919) — **NOT fixed, and this is the main live mechanism**. `scoreable_fields = [f for f in fields if type != "table"]`; `accuracy_score = 100 * count(validation_status != "invalid") / len(scoreable_fields)`. The denominator is every canonical taxonomy field, found or not. Every field that wasn't found against the schema gets `validation_status = "invalid"` (because `missing = (value in (None, ""))` at line 1062), which drags this score down regardless of which extraction engine ran or how confident the model was about the fields it *did* find.

4. **The `any(f["validation_status"] == "invalid" for f in fields)` gate** (line 1938) — **NOT fixed, and the most aggressive mechanism**. A single unmatched canonical field — no matter how many other fields are clean — trips this check by itself and forces `NEEDS_REVIEW`, independent of `accuracy_score` or `overall_conf`.

Both #3 and #4 feed directly into `document_service.py`'s `extraction_needs_review` boolean (lines 1935-1941), which gates `Document.status = NEEDS_REVIEW`. This is the precise, currently-live reason an unfamiliar-layout document gets flagged for review even when every field the engine *did* find was extracted correctly and confidently.

### 1.4 Secondary coupling in Invoice Intelligence

`invoice_decision_service.py`'s `evidence_quality` score (one of five independent confidence dimensions already computed) is `% of non-missing fields with a real bbox` — already gated on non-missing fields only, so it does not re-punish schema-breadth. `invoice_validation_service.py`'s rule engine operates entirely on the already-materialized `ApInvoice` columns (subtotal, tax, vendor_name, etc.) and has no concept of taxonomy coverage at all. So the Invoice Intelligence decision layer is **not** where the reported problem originates — the root cause is entirely upstream, in `document_service.py`'s own `accuracy_score`/`needs_review` gate (§1.3 items 3-4), which runs before Invoice Intelligence ever sees the document.

---

## 2. Current OCR and AI extraction providers

**OCR**:
- **Tesseract** (`ocr/engine.py`) — local, default. PyMuPDF rasterizes PDFs at 220 DPI; returns per-line regions with bbox + per-word-averaged confidence.
- **Cloud OCR** — AWS Textract, Google Vision, Azure Form Recognizer, each a real `Connection` of category `OCR_PROVIDER`, selected per-org via Settings. Confidence = mean of whatever the cloud API reports per region.
- **AI-vision OCR fallback** — only when Tesseract finds zero text; sends rendered page images to a vision-capable AI connection. Confidence is explicitly `None` (never fabricated — the code comment states this outright).
- **Multilingual two-pass flow** (`multilingual_ocr_service.py`) — real hybrid language detector (script-block + optional FastText); a second, narrower-language OCR pass is only kept if it measurably improves the real Tesseract confidence average.

**AI Extraction**: `ConnectionCategory.AI_PROVIDER` connections of type OpenAI, Anthropic, or Ollama. Selection precedence (`document_service._pick_ai_connections()`): per-user default → org default → org Settings type-preference → fallback through every accessible connection on failure → Custom Extraction Engine if zero AI connections exist at all. Real token-usage accounting via `ai_quota_service`, with one corrective JSON-repair retry on malformed model output.

---

## 3. Existing document, invoice, JSON, and database models

### 3.1 `Document` (`models/document.py`, `TenantBase`)

Already has **six separate JSON columns**, none of them a true "preserve every original label verbatim" store:

| Column | Contents |
|---|---|
| `timeline` | `[{stage, status, started_at, ended_at, confidence, error}]` |
| `ocr_result` | `{full_text, confidence, regions:[{page,text,bbox,confidence}], language, quality_flags}` — real, raw OCR output |
| `extraction_result` | `{fields:[{name,value,confidence,...}], business_rule_flag, accuracy_score}` — **already schema-shaped**, not raw |
| `dataset` | `{schema, table, json}` — further reshape of `extraction_result` |
| `ai_raw_response` | The model's own raw JSON payload, verbatim — closest existing thing to "raw," but still only ever contains the schema-named keys the prompt asked about, since the prompt itself is schema-constrained (§1.2). Single blob, overwritten on reprocess, no versioning. |
| `language_config` | Per-document OCR language override |

### 3.2 `ApInvoice` / `ApInvoiceLine` (`models/invoice_intelligence/core.py`)

`ApInvoice` has a **fixed set of ~25 typed columns** (invoice_number, vendor_name, totals, GST/TDS fields, etc.) and **no overflow/extra-fields JSON column**. `invoice_processing_service.materialize_invoice()` builds a `fmap` dict of every extracted field name → value, but only ever reads from it via `fmap.get("invoice_number")`-style hardcoded calls, one per `ApInvoice` column. **Any extracted field whose name isn't one of those ~25 keys is silently never read and never persisted on the mapped row** — this is the second half of the "fields get dropped" problem, confirmed in code, independent of the extraction-side schema-forcing in §1.2. `ApInvoice` also carries `rule_version`/`mapping_version` integer columns, already pinned per-row at evaluation time — directly reusable precedent for a new mapping-profile version pin.

### 3.3 Versioning precedents (two distinct patterns already live in the codebase)

- **Flat `version` + `is_current` boolean on one table** — `TallyAccountMapping`, `ApInvoiceErpMapping`, `ApInvoiceValidationRule`. Publish = insert a fresh batch at `version+1`, flip all prior rows' `is_current=False`. Never mutates a published row in place.
- **Header + immutable-version-child-table** — `ColumnMapping` (header, pointers only) + `ColumnMappingVersion` (immutable snapshot rows, `DRAFT→VALIDATED→PUBLISHED→RETIRED` lifecycle). Mirrors `Pipeline`/`PipelineVersion`.
- **Generalized config-snapshot pointer** — `ApInvoiceModelVersion` (`component`, `version_label`, `config_snapshot: JSON`, `is_active`) — explicitly built so a past row's `rule_version`/`mapping_version` integer can be traced back to a human-readable snapshot. This is the most directly reusable pattern for "record which mapping profile+version a scheduled run used," per the master prompt's §7.4 requirement.

### 3.4 JSON storage mechanics

No Postgres `JSONB` is used anywhere — every JSON column project-wide uses the portable `sqlalchemy.JSON` type, so the SQLite-vs-Postgres duality the master prompt worries about is already a non-issue; it's handled once, at the engine level, via a custom `json_serializer` that fixes a real Unicode-escaping bug (`ensure_ascii=False`) rather than per-column dialect branching. Every model in scope inherits `TenantBase` (org business data), never `PlatformBase`, and new models need an explicit `# noqa: F401` import in `main.py` for `create_all()` to pick them up (no Alembic in this project).

### 3.5 Closest existing precedent for "raw extraction run, separate from canonical/mapped model"

`TallyExtractionRun` + `TallyExtractionRecord` (Tally integration, not Document Intelligence): one header run row, many child record rows each holding one pre-canonical JSON blob, explicitly un-deduplicated ("each run's rows are an immutable snapshot"), with a separate canonical layer (`TallyCanonicalVoucher`/`Party`/`Ledger`) built *from* these raw records via GUID-keyed upsert. This Run/Record split is architecturally the closest match in the codebase to what a new "preserve everything verbatim" Document Intelligence model would need — it just doesn't exist on the Document Intelligence side yet.

---

## 4. Current mapping and transformation functionality

Two mature, independent mapping subsystems already exist and should be reused/extended rather than rebuilt:

### 4.1 Generic ETL Column Mapping (`backend/app/mapping/`, `mapping_service.py`, `api/mapping.py`)

- `auto_mapper.py` — five-tier deterministic matcher (exact → case-insensitive → normalized → curated alias dictionary → `difflib` similarity ≥0.72), global-resolution-per-tier. No AI-assisted matching — deliberately not implemented, to avoid "faking functionality."
- `datatype_mapper.py` — family-level type-compatibility matrix (NUMERIC/STRING/DATE/BOOLEAN/BINARY/JSON/OTHER).
- `mapper.py` — `validate_mapping()` (dangling refs, dup targets, type warnings, unmapped-NOT-NULL-target blocking) and `build_preview_sql()` (real SQL generation from mapping rows, with a regex-based forbidden-DML/DDL guard — **not** the primary safety mechanism, by its own docstring, since it's still raw SQL text sent to a connector).
- **`transform_engine.py` — the one genuinely reusable, non-eval'd expression evaluator in the codebase.** Hand-parsed (regex + recursive-descent-style operand resolution), pandas-vectorized, supports `TRIM/UPPER/LOWER/CAST(...AS...)/COALESCE/CONCAT/CASE WHEN...THEN...ELSE...END/||`. Fails loudly (names the exact unsupported fragment) rather than guessing outside its closed vocabulary. **No `eval()`/`exec()` anywhere in `backend/app/mapping/` — confirmed by direct grep.** This is the strongest existing candidate to extend for document-JSON field transforms, satisfying the master prompt's explicit "no eval, no arbitrary code" requirement.
- `mapping_service.py` — full CRUD + auto-map + validate + preview (preview runs against **real** sample data via the real connector, not a simulation) + publish/retire lifecycle, using the header+immutable-version-table pattern (§3.3).

### 4.2 Tally/Invoice ERP field mapping (`tally_mapping_service.py`, `tally_voucher_builder.py`, `invoice_erp_mapping_service.py`)

- Uses the flat `version`+`is_current` pattern (§3.3).
- A small, **closed enum** of named transform types in a `transform_config` JSON dict (`passthrough`, `fixed`, `voucher_type_lookup` — dict-based lookup, `date_format`) rather than a free-form expression language — simpler and more constrained than `transform_engine.py`, worth weighing as an alternative.
- `tally_voucher_builder.build_voucher()` is a **pure function** (no I/O) that takes a source row + mapping rows + resolves each target slot via `getattr`/dict lookup + `apply_transform()`, raising on an unresolved `required` slot. This "apply mapping → canonical output dict → serialize to target wire format" split is directly analogous to what a document-extraction mapping profile needs (raw JSON → target dict → target format), and is the cleanest existing template to follow.

### 4.3 Field addressing into JSON

No JSONPath library is vendored anywhere (confirmed, no dependency). The closest precedent is a minimal hand-rolled dot-path evaluator (`connectors/api/rest.py` / `soap.py`'s `_json_path_extract()`) supporting `$.data.customers`-style paths with no wildcards — deliberately scoped ("a clearly-scoped implementation beats a half-finished JSONPath dialect," per its own docstring). This is reusable as a starting point for addressing into the new original-extraction JSON structure, though it would need extending to handle the master prompt's duplicate-label/array requirements (§4.1 of the master prompt).

### 4.4 Frontend mapping editor precedent

`ObjectColumnMapping.jsx` (608 lines, full-screen page) already implements almost exactly the three-panel shape requested: a Grid/Visual/Compare-Schema view-mode toggle (Visual mode = two column lists connected by real-measured SVG Bezier curves, colored by status), a row-editor modal (target select, multi-source-column picker, transformation textarea, constant/default toggles, lookup-join sub-form), a live preview panel against real data, and a version/publish bar. This is the direct template for a new mapping-profile editor; the task is adapting its three views from "flat DB column lists" to "nested JSON tree," not building the interaction pattern from scratch.

---

## 5. Existing scheduling and worker architecture

### 5.1 Document-schedule discovery (`document_schedule_service.py`)

`DocumentScheduleConfig` already has a `document_type: str | None` field — free text, pins the static canonical taxonomy only (not a mapping profile; no such concept exists today — confirmed, zero hits for `mapping_profile`/`MappingProfile` anywhere in the backend). `DocumentSourceRegistry` is the durable per-file idempotency ledger (`UniqueConstraint(org, connection, path, filename, content_hash)`), with an explicit `ALLOWED_TRANSITIONS` dict as the sole status-change choke point (`transition()`), enforced identically to the ZIP-batch pattern below.

### 5.2 ZIP Batch worker pool (`document_batch_service.py`) — the most mature pattern to copy

- **CAS claim**: `UPDATE ... WHERE status IN (...) AND id=candidate` + rowcount check (no `SELECT FOR UPDATE SKIP LOCKED`, since SQLite has no row locking) — portable across both backends.
- **Worker pool**: fixed-size pool of coroutines, each looping claim→execute→claim until nothing claimable remains.
- **Heartbeat**: a sidecar coroutine touches `last_heartbeat` every 30s while a file is processing — chosen over a fixed timeout because OCR/AI duration is unbounded.
- **Crash recovery, two layers**: startup-only sweep of anything still `PROCESSING` when the process last died (`recover_stuck_document_batches()`, wired into `main.py` lifespan), plus a periodic (every 2 min) APScheduler job that reclaims individual stale-heartbeat files inside an otherwise-healthy batch.
- **Retry**: `retry_failed()`/`retry_selected()` reset a file row back to `REGISTERED` and let it re-enter the normal claim path. **Important finding: retry always re-runs OCR from the original stored bytes today — there is no "resume from stored OCR text, redo only AI Extraction/mapping" code path anywhere in the codebase.** This is a genuinely new capability the master prompt's §7.5 requirement would need to add, not an extension of something that exists.
- **One-bad-document-doesn't-stop-batch**: enforced by a per-file try/except inside the worker loop — a single failure only marks that row `FAILED`/`RETRY_PENDING`, never propagates.

### 5.3 `process_document()` — current parameter surface

Already accepts `document_type`, `confidence_threshold`/`accuracy_threshold` (org-default fallback), `invoice_auto_post`/`invoice_erp_connection_id`, `preextracted_fields` (DATABASE/API/ERP-kind schedules skip OCR entirely), `storage_override`. **There is no `mapping_profile_id` parameter and no existing seam that already carries one** — adding this would be a new, explicit parameter threaded through from both `schedule_service.py`'s dispatch functions and `document_batch_service._execute_one_file_task()`, the same way `document_type` already is.

### 5.4 APScheduler wiring (`main.py`)

Single in-process `AsyncIOScheduler`, started after a sequence of one-time startup recovery calls. Registered jobs include a 60s `schedule_poll` (the entry point for all scheduled document processing), a 2-min `document_batch_stale_sweep`, and a 24h retention cleanup — **no periodic stale-heartbeat sweep exists for `DocumentSourceRegistry`** (it has no `last_heartbeat` column at all today; its only recovery is the startup-only sweep). A new "mapping default" resolution would most naturally plug into `schedule_poll`'s downstream dispatch, resolved once per run before discovery starts.

### 5.5 Version-pinning precedent for scheduled runs

No config-snapshot field exists on `ScheduleRun` today, but the pattern is well-established elsewhere and directly transplantable: `TargetLoadRun.mapping_version` (pinned at run-start to the mapping's `current_published_version`, falling back to `latest_version`), `PipelineRun.pipeline_version`, and `ApInvoice.rule_version`/`mapping_version` all follow the identical "resolve once at run start, stamp the integer, never re-resolve" convention. `ApInvoiceModelVersion`'s `config_snapshot: JSON` (§3.3) is the most directly reusable existing table for recording *which* mapping profile+version a given run used, in human-traceable form.

### 5.6 Retry-without-OCR — confirmed gap

Across Review Queue (`reject()` always routes back through full `process_document()` reprocessing), document-schedule dispatch, and ZIP-batch retry, **no code path anywhere retries only a downstream stage while reusing stored OCR output**. The one near-miss is `review_queue_service.approve()`, which applies corrections directly to `doc.extraction_result` in place and re-runs only the Invoice Intelligence materialize/validate/decide chain — but that's correction-application, not a general "retry mapping without re-OCRing" mechanism, and it's Invoice-Intelligence-specific. Building this is new work, not a reuse.

---

## 6. Existing Review Queue and validation integration

### 6.1 Review Queue creation paths (`review_queue_service.py`)

Six distinct creation functions, each idempotent (no duplicate open item for the same document): `create_from_document()` (reads `doc.extraction_result`, picks the worst issue via priority order: business-rule Total flag → missing field → lowest-confidence/flagged field), `create_from_anomaly()`, `create_from_invoice_validation()` (fallback when extraction looked clean but a validation rule or overall confidence failed), `create_from_ocr_quality()` (fallback when the real problem was OCR, not extraction), `create_from_tally_exception()`, `create_manual()`. Priority (not creation) is a separate, org-configurable confidence-banding function with an age-escalation rule.

### 6.2 Validation and decision layers are already independent of taxonomy coverage

`invoice_validation_service.py`'s rule engine (SCHEMA/ARITHMETIC/REFERENCE/PO/DUPLICATE/POLICY/ERP interpreters) operates entirely on already-materialized `ApInvoice` columns — it has no concept of "how many of the 47 canonical fields were found." `invoice_decision_service.py` already computes **five separate, never-collapsed confidence dimensions** (`extractor_confidence`, `evidence_quality`, `field_consistency`, `reference_match_score`, `business_validation_score`), decided via a clean threshold matrix (`STRAIGHT_THROUGH` only if every computed score ≥90). This is good news: **Invoice Intelligence's decision layer already implements the "separate dimensions" principle the master prompt asks for** — the work needed is almost entirely in `document_service.py`'s own `accuracy_score`/`needs_review` gate (§1.3), which sits upstream of and independent from this already-correct layer.

### 6.3 `ReviewQueueDetail.jsx` — current structure

Split-screen, not tabbed: left pane is the document viewer + a toggleable raw-OCR-text panel; right pane is a flagged-field card + "All Extracted Fields" list with inline correction inputs; audit trail below. No tab mechanism exists here at all today — a new "Original Extracted JSON" view would be an additive sibling panel, not a restructuring. One real gap found: `review_queue_service.get_detail()` currently returns only the reshaped `fields` list, not `doc.extraction_result`'s raw JSON or `ai_raw_response` — a new endpoint/field would be needed to surface raw extraction data here.

---

## 7. Reusable frontend and backend components

| Need | Reusable today | Gap |
|---|---|---|
| Document detail tab shell | `DocumentDetails.jsx`'s URL-query-param tab pattern (`?tab=`) — add new tab entries the same way | None — direct extension |
| JSON tree/collapsible viewer | **Nothing** — every "JSON view" in the app today is a flat non-interactive `&lt;pre&gt;{JSON.stringify(...)}&lt;/pre&gt;` (confirmed via grep, no `react-json-view`-style dependency anywhere) | Must build new (small custom recursive component, or add a dependency) |
| Mapping editor shell | `ObjectColumnMapping.jsx` three-view (Grid/Visual/Compare) pattern | Adapt source panel from flat column list to nested JSON tree |
| Expression/transform engine | `transform_engine.py` (non-eval'd, pandas-vectorized) | Extend vocabulary or wrap for scalar (non-pandas) document-field use |
| Versioned mapping CRUD API | `api/mapping.py` — full create/version/validate/publish/retire/usage/staleness route set, literal-path-before-param-path ordering already solved | Clone the shape for a new resource |
| RBAC | `require_action(action, module=...)` coarse gate — new routes declare `require_action("VIEW"/"CREATE"/"EDIT"/"DELETE"/"EXECUTE", module="DOCUMENT_INTELLIGENCE")`, pre-built once per module like `mapping.py` does | **No new `PermissionAction` values needed** — existing 5 actions cover full CRUD + run/test |
| Fine-grained custom-Role permissions | `PERMISSION_CATEGORIES`/`CATEGORY_ACTIONS` — adding a category auto-inherits the generic 6-action set | This layer is **not currently enforced server-side anywhere** — admin-UI/data-model only, by its own docstring's explicit statement |
| Router/model registration | `main.py`'s import-pair + `# noqa: F401` convention | None — mechanical addition |
| Worker/batch processing | `document_batch_service.py`'s CAS-claim + worker-pool + heartbeat + two-layer crash recovery | Retry-without-re-OCR is new (§5.6) |
| Version-pin-per-run | `TargetLoadRun.mapping_version` / `PipelineRun.pipeline_version` / `ApInvoice.rule_version` convention; `ApInvoiceModelVersion` config-snapshot table | New column(s) on `DocumentSourceRegistry`/`ScheduleRun`, same pattern |
| Schedule form field for mapping profile | `ScheduleFormModal.jsx`'s existing "fetch a list on open, render a picker" pattern (used for every connection type already) | New field, slots naturally next to the existing `document_type` dropdown |

---

## 8. Proposed minimal schema and API changes

This section is **illustrative** (per the master prompt's own framing of its data-model section) — it reflects what the research surfaced as the lowest-risk fit against existing conventions, not a final design. Two real decisions are flagged as open questions in §9/§10 rather than resolved here.

**New tables** (all `TenantBase`, following existing conventions):
- `DocumentExtraction` (header) — one row per document, pointer to the current/latest extraction version, mirroring `ColumnMapping`'s header-only shape.
- `DocumentExtractionVersion` (child, immutable) — the actual "original key-value JSON" payload per the master prompt's §4 structure, one row per extraction run, never updated after creation (mirrors `ColumnMappingVersion`).
- `MappingProfile` (header) + `MappingProfileVersion` (immutable child) — **or** a flatter `version`+`is_current` single table, following the Tally precedent instead. This choice should be made explicitly before Phase 3 (see §10).
- `DocumentMappedOutput` — one row per (document, mapping profile version) application, holding the mapped JSON result + validation summary.
- `MappingExecution` — audit/history row per mapping application attempt (success/failure, duration, errors), analogous to `DocumentBatchEvent`'s append-only log.

**Extended existing tables** (minimal):
- `DocumentScheduleConfig` — add `mapping_profile_id: int | None` (explicit schedule override).
- `DocumentSourceRegistry` and/or `ScheduleRun` — add `mapping_profile_id`/`mapping_profile_version: int | None`, stamped once at dispatch time, following `TargetLoadRun.mapping_version`'s exact precedent.
- A new org-level "default mapping profile per document type" table, or a `document_taxonomies`-adjacent config row — needed to implement the precedence rule in master-prompt §7.2 (explicit schedule → document-type org default → no mapping).

**New API routes** — clone `api/mapping.py`'s exact shape (list/create/get/update/delete/versions/validate/publish/retire/usage/staleness) for `MappingProfile`, plus document-extraction-specific routes (`GET /documents/{id}/extraction`, `GET /documents/{id}/extraction/versions`, `POST /documents/{id}/mapping/apply`, `POST /documents/{id}/mapping/retry`). All gated via `require_action(..., module="DOCUMENT_INTELLIGENCE")` using the existing 5 `PermissionAction` values — no backend enum change needed.

**No change needed** to the `Base`/`TenantBase` split, the JSON-column type convention, the `json_serializer` Unicode fix, or the model-registration mechanism — all directly reusable as-is.

---

## 9. Backward-compatibility risks

1. **`document_service.process_document()` signature growth.** Adding `mapping_profile_id` as a new optional parameter is low-risk (every existing caller already omits newer optional params like `storage_override`), but the function is already large (600+ lines) and heavily tested; care is needed to keep the new logic additive and not touch the existing OCR/extraction branches.
2. **`accuracy_score`/`needs_review` gate changes (§1.3) are the highest-risk part of this whole project.** This logic is live in production today and directly drives every document's `NEEDS_REVIEW` status. Any change here needs a careful before/after comparison against real historical documents (not just new test fixtures) to confirm the fix doesn't flip previously-correct REVIEW/COMPLETE decisions in an unintended direction, and the master prompt's own acceptance criteria (#3, #16) demand exactly this kind of regression check.
3. **`ApInvoice`'s fixed-column model has no overflow slot.** Preserving "extra" fields without an overflow JSON column on `ApInvoice` itself means either (a) leaving `ApInvoice` untouched and keeping all original-field access on the new `DocumentExtraction`/`MappingProfile` layer (lower risk, recommended), or (b) adding a new JSON column to `ApInvoice` (touches a heavily-used, already-large model with real production rows).
4. **Custom Extraction Engine (`custom_engine.py`) divergence.** It currently has no AI model to call, so "ask the model to also report unfamiliar labels" isn't available to it the way it is for the LLM path — its label-dictionary approach would need either a genuinely new unfamiliar-label-detection mechanism (e.g., "any OCR row that didn't match a known label becomes a raw key-value candidate") or an explicit, disclosed scope limitation for this engine.
5. **Review Queue / Invoice Intelligence consumers of `Document.extraction_result`.** Multiple existing services read this column's current shape directly (`review_queue_service.py`, `invoice_processing_service.py`, `document_storage_service.py`'s six dataset types, webhook delivery). Introducing a parallel "original extraction JSON" structure must not change `extraction_result`'s existing shape/meaning — it should be purely additive (a new column/table alongside it), confirmed safe by every research agent's findings showing `extraction_result` is read in its current shape by name in several places.
6. **Scheduling dispatch changes touch two separate executors** (`schedule_service.py`'s document-schedule path and `document_batch_service.py`'s ZIP-batch path) that both ultimately call `process_document()` — a new mapping-profile parameter needs threading through both, and both already have their own test suites that must stay green.
7. **No existing stale-heartbeat sweep for `DocumentSourceRegistry`** — if mapping-stage retry is added to the document-schedule path (not just ZIP-batch), it inherits this pre-existing gap rather than creating a new one, but it's worth flagging since the master prompt's reliability requirements (§10) assume this kind of recovery exists uniformly.

---

## 10. Phased implementation plan and acceptance tests

The master prompt's own 9-phase breakdown (§13) maps cleanly onto what this assessment found, with no phase requiring a rewrite of an untouched subsystem. Suggested sequencing, matching the master prompt's phases 2-9:

- **Phase 2 (extraction + persistence)**: new `DocumentExtraction`/`DocumentExtractionVersion` tables; extend `extraction_service.py`'s prompt to request/report unmatched labels (carefully, given the fabrication risk already discovered and disclosed in §1.2 — likely needs a stricter "only report what's literally present, evidence required" instruction rather than re-enabling the disabled `find_gap_fields()` wholesale); fix `custom_engine.py`'s `overallConfidence` to match `extraction_service.py`'s existing fix; fix `document_service.py`'s `accuracy_score`/`needs_review` gate (§1.3 items 3-4) to stop scoring against full taxonomy breadth.
- **Phase 3 (mapping engine)**: decide flat-`is_current` vs. header+version-table for `MappingProfile` (recommend the header+immutable-version pattern, matching `ColumnMapping`, since mapping profiles here are closer in spirit to "a document transformation definition" than "a small fixed field list" like Tally's); extend or wrap `transform_engine.py` for scalar JSON-path-sourced values rather than pandas DataFrame columns.
- **Phase 4 (mapping editor UI)**: adapt `ObjectColumnMapping.jsx`'s three-view pattern; build the new JSON tree component (genuinely new, no existing dependency).
- **Phase 5 (scheduled mapping)**: thread `mapping_profile_id` through `process_document()`, `schedule_service.py`, and `document_batch_service.py`; add version-pinning columns following `TargetLoadRun.mapping_version`'s exact precedent; implement retry-without-re-OCR (confirmed new capability, §5.6) — likely via a new `process_document()` branch that accepts already-stored `doc.ocr_result` instead of reading the file again.
- **Phase 6 (document detail views)**: extend `DocumentDetails.jsx`'s existing tab array; add a raw-extraction-JSON toggle to `ReviewQueueDetail.jsx`'s `DocumentPane` (closest existing precedent: the current OCR-text toggle) and extend `review_queue_service.get_detail()` to return it.
- **Phase 7 (security/reliability)**: no new `PermissionAction` values needed (§7); decide whether to also wire the fine-grained `PERMISSION_CATEGORIES` layer for the new "Mapping Profiles" category, given it's currently unenforced everywhere else in the codebase (recommend: skip it for consistency with every other module, unless this is the project chosen to finally wire it up).
- **Phase 8-9**: regression per the master prompt's 24-item acceptance list (§12) is directly actionable against the specific line numbers found in this assessment — e.g. acceptance item #3 ("an unmapped field does not automatically lower extraction confidence") maps exactly to the §1.3 fixes.

### Open questions requiring your decision before Phase 2 begins

1. **MappingProfile versioning shape**: header+immutable-version-table (`ColumnMapping` pattern) vs. flat `version`+`is_current` (`TallyAccountMapping` pattern)? Recommendation: header+version-table, for richer draft/validate/publish lifecycle parity with the mapping editor UI precedent.
2. **Where does "preserve extra fields" live for Invoice Intelligence specifically?** Leave `ApInvoice`'s fixed-column model untouched and keep all original/unmapped-field access on the new `DocumentExtraction` layer (lower risk), or add an overflow JSON column to `ApInvoice` itself? Recommendation: the former.
3. **Scope of "ask the model to report unfamiliar labels" in Phase 2**: given the disclosed, already-encountered fabrication bug with the disabled `find_gap_fields()` pass, this needs a specific, narrower prompt design (evidence-required, no field invented unless a literal label+value pair was read) rather than simply re-enabling what's already there. Worth a focused design pass before implementation, not assumed solved by this assessment.
4. **Does retry-without-re-OCR extend to the document-schedule path, or ship first for ZIP-batch only?** Both executors eventually call the same `process_document()`, so the underlying capability is shared either way — but scoping the first implementation to one executor reduces Phase 5 risk.

---

*No files were modified to produce this assessment. All claims are sourced from direct code reads performed by six parallel research passes; file:line references are preserved in the full research transcripts if deeper verification is needed on any specific point.*
