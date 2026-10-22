"""
Regression tests for Generic Extraction (Document Intelligence: Dynamic
JSON Extraction spec) — evidence-supported key-value pairs beyond the
known/canonical schema, behind the org's generic_extraction_enabled
setting (OFF by default).

The critical thing under test is the real, observed failure this
redesign fixes: the previous "gap-fill" only asked the model to propose a
field NAME, then fed that name into a SECOND, independent extract_fields()
call to get a value — a second blind LLM call with its own chance to
hallucinate, and in practice it did (a plausible field name that never
actually appeared in the document, given a fabricated value instead of
null). The fix requires the model to return value+verbatim evidence in
ONE call and then verifies in Python — not just by prompting — that the
quoted evidence is a genuine substring of the document's own OCR text.

find_gap_fields() calls out to a real AI connector (_get_connector +
_generate_json) — the one real external-network boundary. These tests
monkeypatch exactly that boundary (supplying a canned model response,
including a deliberately-fabricated candidate to prove it gets rejected)
while exercising every other line of real production code: OCR-text
assembly, the grounding substring check, dedup, and covered-name
filtering. This mirrors the project's own convention of mocking only the
genuine external boundary (e.g. test_document_webhook.py's real local
HTTP server standing in for a customer endpoint) rather than mocking the
logic actually under test.
"""
import pytest

from app.models.document import Document
from app.services import document_service, extraction_service


def _doc_with_text(text: str) -> Document:
    doc = Document()
    doc.ocr_result = {"full_text": text, "regions": []}
    return doc


def _patch_ai_call(monkeypatch, parsed_response):
    async def fake_get_connector(db, connection_id, organization_id, acting_user_id=None):
        return object(), object()

    async def fake_generate_json(db, connector, conn, *, organization_id, user_id, feature, prompt, system, max_tokens):
        return parsed_response

    monkeypatch.setattr(extraction_service, "_get_connector", fake_get_connector)
    monkeypatch.setattr(extraction_service, "_generate_json", fake_generate_json)


# ── extraction_service.find_gap_fields() — the real grounding fix ─────

@pytest.mark.asyncio
async def test_find_gap_fields_accepts_genuinely_grounded_evidence(monkeypatch):
    doc = _doc_with_text("Invoice Number: INV-100\nVendor Name: Acme Corp\nDelivery Note: DN-55")
    _patch_ai_call(monkeypatch, [
        {"name": "delivery_note", "type": "string", "value": "DN-55", "evidence": "Delivery Note: DN-55"},
    ])
    fields = await extraction_service.find_gap_fields(
        None, doc, 1, 1, {"invoice_number", "vendor_name"},
    )
    assert len(fields) == 1
    assert fields[0]["name"] == "delivery_note"
    assert fields[0]["value"] == "DN-55"
    assert fields[0]["evidence"] == "Delivery Note: DN-55"
    assert fields[0]["confidence"] == 70.0


@pytest.mark.asyncio
async def test_find_gap_fields_rejects_fabricated_evidence(monkeypatch):
    # The exact real, observed failure this redesign fixes: a plausible
    # field whose quoted "evidence" never actually appears in the OCR
    # text must be dropped, not trusted on the model's word.
    doc = _doc_with_text("Invoice Number: INV-100\nVendor Name: Acme Corp")
    _patch_ai_call(monkeypatch, [
        {"name": "supplier_email", "type": "string", "value": "sales@acme.com", "evidence": "Supplier Email: sales@acme.com"},
    ])
    fields = await extraction_service.find_gap_fields(
        None, doc, 1, 1, {"invoice_number", "vendor_name"},
    )
    assert fields == []


@pytest.mark.asyncio
async def test_find_gap_fields_tolerates_whitespace_formatting_differences(monkeypatch):
    # Real OCR text commonly has different line breaks/spacing than how a
    # model quotes it back — the grounding check must be whitespace/case
    # insensitive, not a brittle byte-for-byte match.
    doc = _doc_with_text("Delivery   Note:\n   DN-55  ")
    _patch_ai_call(monkeypatch, [
        {"name": "delivery_note", "type": "string", "value": "DN-55", "evidence": "delivery note: dn-55"},
    ])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert len(fields) == 1


@pytest.mark.asyncio
async def test_find_gap_fields_excludes_already_covered_and_dedupes(monkeypatch):
    doc = _doc_with_text("Vendor Name: Acme Corp\nReference Code: REF-1\nReference Code: REF-1")
    _patch_ai_call(monkeypatch, [
        {"name": "vendor_name", "type": "string", "value": "Acme Corp", "evidence": "Vendor Name: Acme Corp"},  # already covered
        {"name": "reference_code", "type": "string", "value": "REF-1", "evidence": "Reference Code: REF-1"},
        {"name": "Reference_Code", "type": "string", "value": "REF-1", "evidence": "Reference Code: REF-1"},  # duplicate, different case
    ])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, {"vendor_name"})
    assert len(fields) == 1
    assert fields[0]["name"] == "reference_code"


@pytest.mark.asyncio
async def test_find_gap_fields_rejects_missing_name_value_or_evidence(monkeypatch):
    doc = _doc_with_text("Vendor Name: Acme Corp")
    _patch_ai_call(monkeypatch, [
        {"name": "", "type": "string", "value": "x", "evidence": "Vendor Name: Acme Corp"},
        {"name": "no_value", "type": "string", "value": "", "evidence": "Vendor Name: Acme Corp"},
        {"name": "no_evidence", "type": "string", "value": "x", "evidence": ""},
        "not a dict",
    ])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert fields == []


# ── find_gap_fields() — "repeated" kind (same label, multiple occurrences,
# preserved as an array, never overwritten) ────────────────────────────

@pytest.mark.asyncio
async def test_find_gap_fields_repeated_keeps_only_grounded_occurrences(monkeypatch):
    doc = _doc_with_text("Reference No: REF-1\nReference No: REF-2")
    _patch_ai_call(monkeypatch, [{
        "kind": "repeated", "name": "reference_no", "type": "string",
        "occurrences": [
            {"value": "REF-1", "evidence": "Reference No: REF-1"},
            {"value": "REF-2", "evidence": "Reference No: REF-2"},
            # Fabricated — never appears in the text — must be dropped,
            # not the whole group.
            {"value": "REF-9", "evidence": "Reference No: REF-9"},
        ],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert len(fields) == 1
    assert fields[0]["kind"] == "repeated"
    occ_values = {o["value"] for o in fields[0]["occurrences"]}
    assert occ_values == {"REF-1", "REF-2"}


@pytest.mark.asyncio
async def test_find_gap_fields_repeated_dropped_entirely_when_nothing_grounds(monkeypatch):
    doc = _doc_with_text("Vendor Name: Acme Corp")
    _patch_ai_call(monkeypatch, [{
        "kind": "repeated", "name": "reference_no", "type": "string",
        "occurrences": [{"value": "REF-9", "evidence": "Reference No: REF-9"}],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert fields == []


@pytest.mark.asyncio
async def test_find_gap_fields_repeated_dedupes_identical_occurrences(monkeypatch):
    doc = _doc_with_text("Reference No: REF-1")
    _patch_ai_call(monkeypatch, [{
        "kind": "repeated", "name": "reference_no", "type": "string",
        "occurrences": [
            {"value": "REF-1", "evidence": "Reference No: REF-1"},
            {"value": "REF-1", "evidence": "Reference No: REF-1"},
        ],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert len(fields[0]["occurrences"]) == 1


# ── find_gap_fields() — "nested" kind (flattened into dotted "scalar"
# leaves, each independently grounded) ──────────────────────────────────

@pytest.mark.asyncio
async def test_find_gap_fields_nested_flattens_to_dotted_scalar_leaves(monkeypatch):
    doc = _doc_with_text("Ship To:\nStreet: 123 Main St\nCity: Springfield")
    _patch_ai_call(monkeypatch, [{
        "kind": "nested", "name": "shipping_address",
        "fields": [
            {"name": "street", "value": "123 Main St", "evidence": "Street: 123 Main St"},
            {"name": "city", "value": "Springfield", "evidence": "City: Springfield"},
        ],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    names = {f["name"] for f in fields}
    assert names == {"shipping_address.street", "shipping_address.city"}
    assert all(f["kind"] == "scalar" for f in fields)


@pytest.mark.asyncio
async def test_find_gap_fields_nested_keeps_only_groundable_leaves(monkeypatch):
    doc = _doc_with_text("Ship To:\nStreet: 123 Main St")
    _patch_ai_call(monkeypatch, [{
        "kind": "nested", "name": "shipping_address",
        "fields": [
            {"name": "street", "value": "123 Main St", "evidence": "Street: 123 Main St"},
            # Fabricated — never appears — must be dropped, the grounded
            # sibling leaf must still survive on its own.
            {"name": "city", "value": "Nowhere", "evidence": "City: Nowhere"},
        ],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert len(fields) == 1
    assert fields[0]["name"] == "shipping_address.street"


# ── find_gap_fields() — "table" kind (ad-hoc table, per-row evidence) ──

@pytest.mark.asyncio
async def test_find_gap_fields_table_keeps_only_grounded_rows(monkeypatch):
    doc = _doc_with_text("Payment Schedule\nDue: 2026-01-01 Amount: 100\nDue: 2026-02-01 Amount: 100")
    _patch_ai_call(monkeypatch, [{
        "kind": "table", "name": "payment_schedule", "columns": ["due_date", "amount"],
        "rows": [
            {"due_date": "2026-01-01", "amount": "100", "evidence": "Due: 2026-01-01 Amount: 100"},
            {"due_date": "2026-02-01", "amount": "100", "evidence": "Due: 2026-02-01 Amount: 100"},
            # Fabricated row — must be dropped, not the whole table.
            {"due_date": "2026-03-01", "amount": "999", "evidence": "Due: 2026-03-01 Amount: 999"},
        ],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert len(fields) == 1
    f = fields[0]
    assert f["kind"] == "table"
    assert len(f["rows"]) == 2
    assert len(f["row_evidence"]) == 2
    assert f["rows"][0] == {"due_date": "2026-01-01", "amount": "100"}


@pytest.mark.asyncio
async def test_find_gap_fields_table_dropped_entirely_when_no_row_grounds(monkeypatch):
    doc = _doc_with_text("Vendor Name: Acme Corp")
    _patch_ai_call(monkeypatch, [{
        "kind": "table", "name": "payment_schedule", "columns": ["due_date", "amount"],
        "rows": [{"due_date": "2026-03-01", "amount": "999", "evidence": "Due: 2026-03-01 Amount: 999"}],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, set())
    assert fields == []


@pytest.mark.asyncio
async def test_find_gap_fields_table_name_respects_covered_and_dedup(monkeypatch):
    doc = _doc_with_text("Due: 2026-01-01 Amount: 100")
    _patch_ai_call(monkeypatch, [{
        "kind": "table", "name": "line_items", "columns": ["due_date"],  # already covered
        "rows": [{"due_date": "2026-01-01", "evidence": "Due: 2026-01-01 Amount: 100"}],
    }])
    fields = await extraction_service.find_gap_fields(None, doc, 1, 1, {"line_items"})
    assert fields == []


# ── document_service._reshape_generic_fields() ─────────────────────────

def test_reshape_generic_fields_shape():
    raw = [{"kind": "scalar", "name": "delivery_note", "type": "string", "value": "DN-55", "evidence": "Delivery Note: DN-55", "confidence": 70.0}]
    fields = document_service._reshape_generic_fields(raw)
    f = fields[0]
    assert f["is_generic"] is True
    assert f["required"] is False
    assert f["missing"] is False
    assert f["validation_status"] == "valid"
    assert f["evidence"] == "Delivery Note: DN-55"
    assert f["value"] == "DN-55"


def test_reshape_generic_fields_repeated_becomes_one_column_table():
    raw = [{
        "kind": "repeated", "name": "reference_no", "type": "string",
        "occurrences": [
            {"value": "REF-1", "evidence": "Reference No: REF-1"},
            {"value": "REF-2", "evidence": "Reference No: REF-2"},
        ],
        "confidence": 70.0,
    }]
    fields = document_service._reshape_generic_fields(raw)
    f = fields[0]
    assert f["type"] == "table"
    assert f["is_generic"] is True
    assert f["columns"] == ["reference_no"]
    assert f["rows"] == [{"reference_no": "REF-1"}, {"reference_no": "REF-2"}]
    assert f["row_evidence"] == ["Reference No: REF-1", "Reference No: REF-2"]
    assert f["value"] == "2 occurrences"
    assert f["validation_status"] == "valid"
    assert f["required"] is False


def test_reshape_generic_fields_table_matches_canonical_table_shape():
    raw = [{
        "kind": "table", "name": "payment_schedule", "columns": ["due_date", "amount"],
        "rows": [{"due_date": "2026-01-01", "amount": "100"}],
        "row_evidence": ["Due: 2026-01-01 Amount: 100"],
        "confidence": 70.0,
    }]
    fields = document_service._reshape_generic_fields(raw)
    f = fields[0]
    assert f["type"] == "table"
    assert f["is_generic"] is True
    assert f["columns"] == ["due_date", "amount"]
    assert f["rows"] == [{"due_date": "2026-01-01", "amount": "100"}]
    assert f["value"] == "1 row"
    assert f["evidence"] == "Due: 2026-01-01 Amount: 100"


def test_generic_table_and_repeated_fields_excluded_from_accuracy_gate():
    # Same gate-simulation convention as test_confidence_gating_fix.py —
    # confirms the table/repeated extension doesn't reopen the gate GE4
    # already closed for scalar generic fields.
    canonical = document_service._reshape_extraction_fields(
        [{"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "page": 1, "required": False}],
    )
    generic_table = document_service._reshape_generic_fields([{
        "kind": "table", "name": "payment_schedule", "columns": ["due_date"],
        "rows": [{"due_date": "2026-01-01"}], "row_evidence": ["Due: 2026-01-01"], "confidence": 5.0,
    }])
    generic_repeated = document_service._reshape_generic_fields([{
        "kind": "repeated", "name": "reference_no", "type": "string",
        "occurrences": [{"value": "REF-1", "evidence": "Reference No: REF-1"}], "confidence": 5.0,
    }])
    fields = canonical + generic_table + generic_repeated

    scoreable_fields = [f for f in fields if f.get("type") != "table" and f.get("validation_status") != "not_found" and not f.get("is_generic")]
    accuracy_score = round(100 * sum(1 for f in scoreable_fields if f.get("validation_status") != "invalid") / len(scoreable_fields), 1) \
        if scoreable_fields else 100.0
    needs_review = any(f["validation_status"] == "invalid" for f in fields)

    assert accuracy_score == 100.0
    assert needs_review is False
    assert len(scoreable_fields) == 1


# ── document_service._maybe_run_generic_extraction() — the feature flag ──

@pytest.mark.asyncio
async def test_maybe_run_generic_extraction_off_by_default(db):
    from app.models.organization import Organization

    org = Organization(name="Generic Extraction Test Org", slug="gen-ext-test-org")
    db.add(org)
    await db.commit()
    await db.refresh(org)

    doc = _doc_with_text("Vendor Name: Acme Corp")
    doc.organization_id = org.id

    result = await document_service._maybe_run_generic_extraction(
        db, doc, connection_id=1, acting_user_id=None, known_fields=[{"name": "vendor_name"}],
    )
    assert result == []


@pytest.mark.asyncio
async def test_maybe_run_generic_extraction_enabled_calls_find_gap_fields(db, monkeypatch):
    from app.models.organization import Organization
    from app.services import settings_service

    org = Organization(name="Generic Extraction Enabled Org", slug="gen-ext-enabled-org")
    db.add(org)
    await db.commit()
    await db.refresh(org)

    org_settings = await settings_service.get_or_create(db, org.id)
    org_settings.values = {
        **(org_settings.values or {}),
        "document_intelligence": {**(org_settings.values or {}).get("document_intelligence", {}), "generic_extraction_enabled": True},
    }
    await db.commit()

    doc = _doc_with_text("Vendor Name: Acme Corp\nDelivery Note: DN-55")
    doc.organization_id = org.id

    async def fake_find_gap_fields(db_, doc_, connection_id, organization_id, covered_field_names, acting_user_id=None):
        return [{"name": "delivery_note", "type": "string", "value": "DN-55", "evidence": "Delivery Note: DN-55", "confidence": 70.0}]

    monkeypatch.setattr(extraction_service, "find_gap_fields", fake_find_gap_fields)

    result = await document_service._maybe_run_generic_extraction(
        db, doc, connection_id=1, acting_user_id=None, known_fields=[{"name": "vendor_name"}],
    )
    assert len(result) == 1
    assert result[0]["name"] == "delivery_note"
    assert result[0]["is_generic"] is True


@pytest.mark.asyncio
async def test_maybe_run_generic_extraction_never_raises_on_failure(db, monkeypatch):
    from app.models.organization import Organization
    from app.services import settings_service

    org = Organization(name="Generic Extraction Failure Org", slug="gen-ext-failure-org")
    db.add(org)
    await db.commit()
    await db.refresh(org)

    org_settings = await settings_service.get_or_create(db, org.id)
    org_settings.values = {
        **(org_settings.values or {}),
        "document_intelligence": {**(org_settings.values or {}).get("document_intelligence", {}), "generic_extraction_enabled": True},
    }
    await db.commit()

    doc = _doc_with_text("Vendor Name: Acme Corp")
    doc.organization_id = org.id

    async def raising_find_gap_fields(*args, **kwargs):
        raise RuntimeError("simulated connector failure")

    monkeypatch.setattr(extraction_service, "find_gap_fields", raising_find_gap_fields)

    # Best-effort: a real failure here must never propagate and break the
    # canonical extraction that already succeeded.
    result = await document_service._maybe_run_generic_extraction(
        db, doc, connection_id=1, acting_user_id=None, known_fields=[{"name": "vendor_name"}],
    )
    assert result == []


# ── document_service accuracy_score/needs_review gate excludes generics ──

def test_generic_fields_excluded_from_accuracy_score_and_needs_review_gate():
    # Mirrors test_confidence_gating_fix.py's own gate-simulation helper —
    # process_document()'s accuracy_score/extraction_needs_review formula
    # is inline in a large async function, not unit-test-friendly in
    # isolation; this re-applies that exact small (2-line) formula to the
    # real output of _reshape_extraction_fields + _reshape_generic_fields.
    canonical_raw = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "page": 1, "required": False},
    ]
    canonical = document_service._reshape_extraction_fields(canonical_raw)
    generic_raw = [
        # Deliberately low confidence — must never lower accuracy_score,
        # since it's an unrequested bonus find, not part of the schema
        # being scored.
        {"name": "delivery_note", "type": "string", "value": "DN-55", "evidence": "Delivery Note: DN-55", "confidence": 5.0},
    ]
    generic = document_service._reshape_generic_fields(generic_raw)
    fields = canonical + generic

    scoreable_fields = [f for f in fields if f.get("type") != "table" and f.get("validation_status") != "not_found" and not f.get("is_generic")]
    accuracy_score = round(100 * sum(1 for f in scoreable_fields if f.get("validation_status") != "invalid") / len(scoreable_fields), 1) \
        if scoreable_fields else 100.0
    needs_review = any(f["validation_status"] == "invalid" for f in fields)

    assert accuracy_score == 100.0
    assert needs_review is False
    assert len(scoreable_fields) == 1  # the generic field never entered the pool at all
