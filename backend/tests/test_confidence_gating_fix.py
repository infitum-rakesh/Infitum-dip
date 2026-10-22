"""
Regression tests for the Document Intelligence confidence-gating fix.

Bug being fixed: document_service.py's accuracy_score / extraction_needs_
review gate, and the two extraction engines' overallConfidence, all used
to treat a field the canonical taxonomy proposes but the document simply
doesn't carry (e.g. order_number on an invoice that has none) exactly the
same as a genuine extraction failure. Any document missing even one
legitimately optional taxonomy field could get its accuracy_score dragged
down and get forced into NEEDS_REVIEW purely for that reason, regardless
of how accurately every field that actually exists on the page was read.

The fix threads a `required` flag (already a real, existing concept —
detect_schema()'s ad-hoc AI-schema-guess path already lets the model mark
a field required=True/False; document_taxonomies.build_seed_schema()
always seeds required=False today) through both extraction engines' field
output and into document_service._reshape_extraction_fields()'s per-field
validation_status: a missing field is "invalid" only when required=True,
otherwise the new "not_found" status — honestly absent, never counted as
a failure.

These tests call the real production functions directly (no mocks of the
code under test). Where custom_engine.py's OCR-matching needs synthetic
OCR regions, those are built from real bbox/text shapes the real
_build_rows()/_find_label_value() consume — not stubs of those functions.
"""
from app.extraction import custom_engine
from app.models.document import Document
from app.services import document_service, review_queue_service
from app.services import extraction_service
from app.services.review_queue_service import ReviewType


# ── document_service._reshape_extraction_fields ────────────────────────

def test_reshape_optional_missing_scalar_is_not_found():
    raw_fields = [{"name": "order_number", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": False}]
    fields = document_service._reshape_extraction_fields(raw_fields)
    f = fields[0]
    assert f["missing"] is True
    assert f["required"] is False
    assert f["validation_status"] == "not_found"


def test_reshape_required_missing_scalar_is_invalid():
    raw_fields = [{"name": "invoice_number", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": True}]
    fields = document_service._reshape_extraction_fields(raw_fields)
    f = fields[0]
    assert f["missing"] is True
    assert f["required"] is True
    assert f["validation_status"] == "invalid"


def test_reshape_optional_missing_table_is_not_found():
    raw_fields = [{"name": "line_items", "type": "table", "columns": [], "rows": [], "confidence": 0.0, "required": False}]
    fields = document_service._reshape_extraction_fields(raw_fields)
    assert fields[0]["validation_status"] == "not_found"
    assert fields[0]["missing"] is True


def test_reshape_required_missing_table_is_invalid():
    raw_fields = [{"name": "line_items", "type": "table", "columns": [], "rows": [], "confidence": 0.0, "required": True}]
    fields = document_service._reshape_extraction_fields(raw_fields)
    assert fields[0]["validation_status"] == "invalid"


def test_reshape_present_field_confidence_thresholds_unaffected_by_required():
    # Regression guard: a PRESENT field's validation_status must still be
    # driven purely by confidence thresholds, regardless of required —
    # the fix must never weaken genuine low-confidence detection.
    for required in (True, False):
        raw_fields = [
            {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 95.0, "page": 1, "required": required},
            {"name": "po_number", "type": "scalar", "value": "PO-1", "confidence": 85.0, "page": 1, "required": required},
            {"name": "notes", "type": "scalar", "value": "garbled", "confidence": 50.0, "page": 1, "required": required},
        ]
        fields = document_service._reshape_extraction_fields(raw_fields)
        assert fields[0]["validation_status"] == "valid"
        assert fields[1]["validation_status"] == "warning"
        assert fields[2]["validation_status"] == "invalid"
        assert all(f["missing"] is False for f in fields)


# ── document_service's accuracy_score / extraction_needs_review gate ──
#
# accuracy_score and extraction_needs_review are computed inline inside
# process_document() (a large async function with real OCR/AI/DB
# dependencies end to end, not unit-test-friendly in isolation). Per this
# project's "don't rewrite/extract code just to make it testable"
# discipline, this test instead calls the real _reshape_extraction_fields
# and then re-applies process_document()'s own small (2-line) gate
# formula verbatim, to pin down the exact integration behavior the fix
# is meant to produce.

def _accuracy_and_needs_review(fields, *, accuracy_threshold=85.0):
    scoreable_fields = [f for f in fields if f.get("type") != "table" and f.get("validation_status") != "not_found"]
    accuracy_score = round(100 * sum(1 for f in scoreable_fields if f.get("validation_status") != "invalid") / len(scoreable_fields), 1) \
        if scoreable_fields else 100.0
    needs_review = (fields and accuracy_score < accuracy_threshold) or any(f["validation_status"] == "invalid" for f in fields)
    return accuracy_score, needs_review


def test_accuracy_score_not_dragged_down_by_optional_missing_fields():
    raw_fields = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "page": 1, "required": False},
        {"name": "invoice_number", "type": "scalar", "value": "INV-001", "confidence": 96.0, "page": 1, "required": False},
        {"name": "total_amount", "type": "scalar", "value": "100.00", "confidence": 95.0, "page": 1, "required": False},
        # Legitimately-optional canonical taxonomy fields this invoice
        # simply doesn't carry — none of these should count against score.
        {"name": "order_number", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": False},
        {"name": "vendor_address_line_2", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": False},
        {"name": "shipping_amount", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": False},
    ]
    fields = document_service._reshape_extraction_fields(raw_fields)
    accuracy_score, needs_review = _accuracy_and_needs_review(fields)
    assert accuracy_score == 100.0
    assert needs_review is False


def test_needs_review_still_fires_on_required_missing_field():
    raw_fields = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "page": 1, "required": False},
        {"name": "invoice_number", "type": "scalar", "value": "", "confidence": 0.0, "page": 1, "required": True},  # mandatory, genuinely missing
    ]
    fields = document_service._reshape_extraction_fields(raw_fields)
    accuracy_score, needs_review = _accuracy_and_needs_review(fields)
    assert accuracy_score == 50.0
    assert needs_review is True


def test_needs_review_still_fires_on_genuine_low_confidence():
    raw_fields = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "page": 1, "required": False},
        {"name": "total_amount", "type": "scalar", "value": "garbled text", "confidence": 40.0, "page": 1, "required": False},  # present, but low confidence
    ]
    fields = document_service._reshape_extraction_fields(raw_fields)
    _, needs_review = _accuracy_and_needs_review(fields)
    assert needs_review is True


# ── custom_engine.py — real, end-to-end (no AI, no mocks) ──────────────

def _ocr_doc(lines: list[str]) -> Document:
    """Builds a real (unpersisted) Document with synthetic-but-real-shaped
    OCR regions — one region per line, left-to-right single column —
    consumed by the real _build_rows()/_find_label_value()."""
    regions = []
    for i, text in enumerate(lines):
        regions.append({"page": 1, "text": text, "confidence": 92.0, "bbox": [10, i * 20, 300, 18]})
    full_text = "\n".join(lines)
    doc = Document()
    doc.ocr_result = {"full_text": full_text, "regions": regions}
    return doc


def test_custom_engine_overall_confidence_excludes_optional_missing_field():
    doc = _ocr_doc(["Vendor Name: Acme Corp"])
    schema = [
        {"name": "vendor_name", "type": "string", "required": False},
        {"name": "order_number", "type": "string", "required": False},  # not on the page, legitimately optional
    ]
    result = custom_engine.extract_fields(doc, schema)
    fields_by_name = {f["name"]: f for f in result["fields"]}
    assert fields_by_name["vendor_name"]["value"] == "Acme Corp"
    assert fields_by_name["order_number"]["required"] is False
    assert fields_by_name["order_number"]["value"] == ""
    # overallConfidence reflects ONLY the field actually found.
    assert result["overallConfidence"] == fields_by_name["vendor_name"]["confidence"]


def test_custom_engine_overall_confidence_includes_required_missing_field():
    doc = _ocr_doc(["Vendor Name: Acme Corp"])
    schema = [
        {"name": "vendor_name", "type": "string", "required": False},
        {"name": "po_number", "type": "string", "required": True},  # mandatory, not on the page
    ]
    result = custom_engine.extract_fields(doc, schema)
    fields_by_name = {f["name"]: f for f in result["fields"]}
    assert fields_by_name["po_number"]["value"] == ""
    assert fields_by_name["po_number"]["required"] is True
    vendor_conf = fields_by_name["vendor_name"]["confidence"]
    # The missing-but-required field's 0.0 confidence is averaged in —
    # a genuine mandatory miss still drags overall confidence down.
    assert result["overallConfidence"] == round((vendor_conf + 0.0) / 2, 1)
    assert result["overallConfidence"] < vendor_conf


# ── extraction_service.py (AI path) — pure functions, no AI call ──────

def test_fields_from_parsed_threads_required_flag():
    schema = [
        {"name": "vendor_name", "type": "string", "required": False},
        {"name": "po_number", "type": "string", "required": True},
    ]
    parsed = {"vendor_name": {"value": "Acme Corp", "confidence": 95}}  # po_number absent from the model's response
    fields = extraction_service._fields_from_parsed(schema, parsed)
    by_name = {f["name"]: f for f in fields}
    assert by_name["vendor_name"]["required"] is False
    assert by_name["po_number"]["required"] is True
    assert by_name["po_number"]["value"] == ""
    assert by_name["po_number"]["confidence"] == 0.0


def test_fields_from_parsed_table_threads_required_flag():
    schema = [{"name": "line_items", "type": "table", "required": True, "columns": [{"name": "description"}]}]
    fields = extraction_service._fields_from_parsed(schema, {})
    assert fields[0]["required"] is True
    assert fields[0]["rows"] == []


def test_merge_extraction_fields_threads_required_flag_on_default_and_table():
    schema = [
        {"name": "po_number", "type": "string", "required": True},
        {"name": "line_items", "type": "table", "required": False, "columns": [{"name": "description"}]},
    ]
    # No chunk ever produced either field — exercises both the "chosen is
    # None" scalar default branch and the table-merge branch.
    merged = extraction_service._merge_extraction_fields(schema, chunks=[[]])
    by_name = {f["name"]: f for f in merged}
    assert by_name["po_number"]["required"] is True
    assert by_name["line_items"]["required"] is False


# ── review_queue_service._pick_primary_issue ───────────────────────────

def _doc_with_fields(fields: list[dict], *, business_rule_flag: bool = False) -> Document:
    doc = Document()
    doc.extraction_result = {"fields": fields, "business_rule_flag": business_rule_flag}
    doc.pipeline = "Invoice Pipeline"
    return doc


def test_pick_primary_issue_ignores_optional_missing_field():
    fields = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "missing": False, "required": False, "validation_status": "valid", "suggested_correction": None},
        {"name": "order_number", "type": "scalar", "value": "", "confidence": 0.0, "missing": True, "required": False, "validation_status": "not_found", "suggested_correction": None},
    ]
    doc = _doc_with_fields(fields)
    picked = review_queue_service._pick_primary_issue(doc)
    # Nothing genuinely wrong here — the optional-missing field must not
    # become (or cause) a review issue.
    assert picked is None


def test_pick_primary_issue_surfaces_required_missing_field():
    fields = [
        {"name": "vendor_name", "type": "scalar", "value": "Acme Corp", "confidence": 97.0, "missing": False, "required": False, "validation_status": "valid", "suggested_correction": None},
        {"name": "invoice_number", "type": "scalar", "value": "", "confidence": 0.0, "missing": True, "required": True, "validation_status": "invalid", "suggested_correction": None},
    ]
    doc = _doc_with_fields(fields)
    picked = review_queue_service._pick_primary_issue(doc)
    assert picked is not None
    assert picked["issue"] == "Missing Field"
    assert picked["field_name"] == "invoice_number"
    assert picked["review_type"] == ReviewType.AI_EXTRACTION


def test_pick_primary_issue_optional_missing_never_outranks_low_confidence_present_field():
    # Before the fix, the optional-missing field's confidence of 0.0 would
    # always win min(..., key=confidence) ahead of a genuinely low (but
    # present) field — masking the real issue that should be reviewed.
    fields = [
        {"name": "total_amount", "type": "scalar", "value": "garbled", "confidence": 55.0, "missing": False, "required": False, "validation_status": "invalid", "suggested_correction": None},
        {"name": "order_number", "type": "scalar", "value": "", "confidence": 0.0, "missing": True, "required": False, "validation_status": "not_found", "suggested_correction": None},
    ]
    doc = _doc_with_fields(fields)
    picked = review_queue_service._pick_primary_issue(doc)
    assert picked is not None
    assert picked["field_name"] == "total_amount"
