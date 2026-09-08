"""
Multilingual Document Processing — Phase 3 tests.

Real async SQLite DB, real ORM models/services, real Unicode text (actual
Kannada/Tamil/Hindi/Devanagari characters, not placeholders) — same "no
mocks of the code under test" convention as every other file in this
suite (see conftest.py's own docstring). Covers:

  - script-block language detection (deterministic, no external model)
  - hybrid detector's graceful degradation when FastText isn't configured
  - page-aware + document-level detection (the explicit "not just
    document-aware" requirement)
  - Custom Engine's external per-language label dictionaries actually
    finding fields no English-only dictionary could
  - the real ensure_ascii=False bug fix (Unicode preserved end-to-end,
    not \\uXXXX-escaped) at both the JSON-string and real-database layers
  - backward-compatible settings merge for orgs whose stored row predates
    this phase's new document_intelligence fields
"""
import json

import pytest
from sqlalchemy import text

from app.extraction import custom_engine
from app.extraction.label_dictionaries.loader import available_languages, get_label_dictionary
from app.models.settings import DEFAULT_SETTINGS, OrganizationSettings
from app.ocr.language_config import is_script_ambiguous, tesseract_code_for
from app.ocr.language_detection import (
    FastTextLanguageDetector,
    HybridLanguageDetector,
    LanguageDetectionUnavailableError,
    ScriptBlockLanguageDetector,
)
from app.ocr.language_detection.document_language_service import detect_document_language, tesseract_lang_string_for_page
from app.services.settings_service import _effective_values

KANNADA_TEXT = "ಕನ್ನಡದಲ್ಲಿ ಇರುವ ಪಠ್ಯ"
TAMIL_TEXT = "தமிழில் உள்ள உரை"
HINDI_TEXT = "हिंदी में लिखा हुआ पाठ"
ENGLISH_TEXT = "This is plain English text about an invoice"


# ── language_config ───────────────────────────────────────────────────

def test_language_config_tesseract_codes():
    assert tesseract_code_for("kn") == "kan"
    assert tesseract_code_for("ta") == "tam"
    assert tesseract_code_for("hi") == "hin"
    assert tesseract_code_for("en") == "eng"
    assert tesseract_code_for("xx") is None  # unconfigured code -> honestly None, never a guess


def test_script_ambiguity_is_real_not_invented():
    assert is_script_ambiguous("DEVANAGARI") is True  # Hindi + Marathi genuinely share this script
    assert is_script_ambiguous("KANNADA") is False  # Kannada script maps to exactly one supported language


# ── ScriptBlockLanguageDetector ──────────────────────────────────────

def test_script_block_detects_kannada():
    result = ScriptBlockLanguageDetector().detect(KANNADA_TEXT)
    assert result.primary_language == "kn"
    assert result.method == "SCRIPT_BLOCK"
    assert result.candidates[0].confidence > 90


def test_script_block_detects_tamil():
    result = ScriptBlockLanguageDetector().detect(TAMIL_TEXT)
    assert result.primary_language == "ta"


def test_script_block_detects_devanagari_as_hindi_tentatively():
    result = ScriptBlockLanguageDetector().detect(HINDI_TEXT)
    assert result.primary_language == "hi"  # SCRIPT_LANGUAGE_DEFAULT's tentative pick
    assert ScriptBlockLanguageDetector().dominant_script_is_ambiguous(HINDI_TEXT) is True  # honestly flagged — could be Marathi


def test_script_block_mixed_kannada_and_english():
    # Real per-character counting, not a guess: the Kannada portion here
    # (repeated, as a real invoice's body text would be) genuinely has
    # more scripted characters than the short English suffix, so "kn"
    # winning is a correct count, not an assumption.
    mixed = f"{KANNADA_TEXT} {KANNADA_TEXT} Ltd"
    result = ScriptBlockLanguageDetector().detect(mixed)
    langs = {c.language for c in result.candidates}
    assert "kn" in langs and "en" in langs
    assert result.primary_language == "kn"


def test_script_block_insufficient_text_is_honest_not_a_guess():
    result = ScriptBlockLanguageDetector().detect("12345 -- ₹₹₹")
    assert result.primary_language is None
    assert result.method == "INSUFFICIENT_TEXT"


# ── HybridLanguageDetector — graceful degradation without FastText ──

def test_hybrid_detector_decisive_script_never_needs_fasttext():
    # No FastText model configured in this test environment — a decisive,
    # unambiguous script (Kannada) must still resolve correctly through
    # script-block alone, never raising or blocking on the missing layer.
    detector = HybridLanguageDetector(fasttext_detector=FastTextLanguageDetector(model_path=""))
    result = detector.detect(KANNADA_TEXT)
    assert result.primary_language == "kn"
    assert result.method == "SCRIPT_BLOCK"


def test_hybrid_detector_degrades_honestly_when_fasttext_unavailable_for_ambiguous_script():
    detector = HybridLanguageDetector(fasttext_detector=FastTextLanguageDetector(model_path=""))
    result = detector.detect(HINDI_TEXT)  # Devanagari — genuinely ambiguous, would normally escalate
    # No fasttext model present -> falls back to the still-real script-block answer, doesn't raise/crash.
    assert result.primary_language == "hi"
    assert result.method == "SCRIPT_BLOCK"


def test_fasttext_detector_reports_unavailable_honestly():
    detector = FastTextLanguageDetector(model_path="/nonexistent/path/lid.176.ftz")
    assert detector.is_available is False
    with pytest.raises(LanguageDetectionUnavailableError):
        detector.detect(KANNADA_TEXT * 3)  # long enough to pass MIN_TEXT_LENGTH


# ── document_language_service — PAGE-aware, not just document-aware ──

def _fake_ocr_result_multi_page():
    """Mirrors the exact example from the approved architecture:
    Page 1 -> Kannada, Page 2 -> Kannada+English, Page 3 -> English+Kannada,
    Page 4 -> Hindi."""
    return {
        "full_text": " ".join([KANNADA_TEXT, KANNADA_TEXT, "English text here too", "English text here too", KANNADA_TEXT, HINDI_TEXT]),
        "regions": [
            {"page": 1, "text": KANNADA_TEXT * 3, "bbox": [0, 0, 10, 10], "confidence": 95},
            {"page": 2, "text": KANNADA_TEXT * 3, "bbox": [0, 0, 10, 10], "confidence": 92},
            {"page": 2, "text": "Invoice Number PRIVATE LIMITED", "bbox": [0, 20, 10, 10], "confidence": 90},
            {"page": 3, "text": "This is an English invoice with details about the order", "bbox": [0, 0, 10, 10], "confidence": 93},
            {"page": 3, "text": KANNADA_TEXT, "bbox": [0, 20, 10, 10], "confidence": 88},
            {"page": 4, "text": HINDI_TEXT * 3, "bbox": [0, 0, 10, 10], "confidence": 97},
        ],
    }


def test_document_language_is_page_aware():
    detector = HybridLanguageDetector(fasttext_detector=FastTextLanguageDetector(model_path=""))
    result = detect_document_language(_fake_ocr_result_multi_page(), detector)

    assert len(result.pages) == 4
    by_page = {p.page_number: p for p in result.pages}
    assert by_page[1].primary_language == "kn"
    assert by_page[2].primary_language == "kn"
    assert by_page[4].primary_language == "hi"
    # Page 3 is majority-English text with a Kannada region mixed in —
    # real per-page detection, not a single document-level answer copied
    # onto every page.
    assert by_page[3].primary_language in ("en", "kn")

    assert result.is_multilingual is True
    assert "kn" in result.detected_languages
    assert "hi" in result.detected_languages


def test_tesseract_lang_string_built_from_page_result():
    from app.ocr.language_detection.document_language_service import PageLanguageResult
    from app.ocr.language_detection.base import LanguageCandidate

    page = PageLanguageResult(
        page_number=2,
        primary_language="kn",
        languages=[LanguageCandidate("kn", 91.0), LanguageCandidate("en", 9.0)],
        method="SCRIPT_BLOCK",
    )
    assert tesseract_lang_string_for_page(page) == "kan+eng"


def test_tesseract_lang_string_none_when_language_unconfigured():
    from app.ocr.language_detection.document_language_service import PageLanguageResult

    page = PageLanguageResult(page_number=1, primary_language=None, languages=[], method="INSUFFICIENT_TEXT")
    assert tesseract_lang_string_for_page(page) is None


# ── label_dictionaries — real files, real loader ────────────────────

def test_priority_language_dictionaries_exist_on_disk():
    langs = available_languages()
    for code in ("en", "hi", "kn", "ta"):
        assert code in langs


def test_label_dictionary_unicode_preserved_from_disk():
    d = get_label_dictionary("kn")
    assert d is not None
    assert "ಒಟ್ಟು ಮೊತ್ತ" in d.field_synonyms["total_amount"]


def test_missing_language_dictionary_degrades_to_none_not_a_crash():
    assert get_label_dictionary("zz") is None


# ── Custom Engine — external per-language dictionaries actually work ──

class _FakeDoc:
    """Custom Engine's extract_fields() only ever reads doc.ocr_result —
    a plain duck-typed stand-in avoids constructing a full Document ORM
    row (and its NOT NULL columns) for a pure-function test."""
    def __init__(self, ocr_result):
        self.ocr_result = ocr_result


def _row_region(page, text_value, y):
    return {"page": page, "text": text_value, "bbox": [10, y, 200, 20], "confidence": 90.0}


def test_custom_engine_finds_kannada_subtotal_only_with_kannada_language_hint():
    # "subtotal" deliberately has no script-agnostic regex fallback in
    # custom_engine.py (unlike total_amount, which does) — so finding it
    # genuinely depends on the label dictionary, not a lucky fallback.
    schema = [{"name": "subtotal", "type": "number"}]
    doc = _FakeDoc({
        "full_text": "ಉಪ ಮೊತ್ತ 25000",
        "regions": [_row_region(1, "ಉಪ ಮೊತ್ತ 25000", 0)],
    })

    without_kannada = custom_engine.extract_fields(doc, schema, languages=None)
    assert without_kannada["fields"][0]["value"] in ("", None)  # English-only dictionary can't recognize this label

    with_kannada = custom_engine.extract_fields(doc, schema, languages=["kn"])
    assert with_kannada["fields"][0]["value"] == 25000.0


def test_custom_engine_finds_tamil_invoice_number():
    schema = [{"name": "invoice_number", "type": "string"}]
    doc = _FakeDoc({
        "full_text": "விலைப்பட்டியல் எண் INV-9001",
        "regions": [_row_region(1, "விலைப்பட்டியல் எண் INV-9001", 0)],
    })
    result = custom_engine.extract_fields(doc, schema, languages=["ta"])
    assert result["fields"][0]["value"] == "INV-9001"


def test_custom_engine_finds_hindi_balance_due():
    # "balance_due" (unlike total_amount) has no script-agnostic regex
    # fallback in custom_engine.py — finding it genuinely exercises the
    # Hindi label dictionary, not a lucky number-fallback match.
    schema = [{"name": "balance_due", "type": "number"}]
    doc = _FakeDoc({
        "full_text": "शेष राशि 15000",
        "regions": [_row_region(1, "शेष राशि 15000", 0)],
    })

    without_hindi = custom_engine.extract_fields(doc, schema, languages=None)
    assert without_hindi["fields"][0]["value"] in ("", None)

    with_hindi = custom_engine.extract_fields(doc, schema, languages=["hi"])
    assert with_hindi["fields"][0]["value"] == 15000.0


def test_custom_engine_mixed_document_still_finds_english_label():
    """A Kannada-language document with an English field label (the
    approved architecture's own mixed-document scenario) must still work
    — English stays searched alongside the passed language, never
    replaced by it."""
    schema = [{"name": "invoice_number", "type": "string"}]
    doc = _FakeDoc({
        "full_text": f"{KANNADA_TEXT} Invoice Number INV-5001",
        "regions": [_row_region(1, "Invoice Number INV-5001", 0)],
    })
    result = custom_engine.extract_fields(doc, schema, languages=["kn"])
    assert result["fields"][0]["value"] == "INV-5001"


def test_custom_engine_english_only_default_unchanged():
    """Regression guard: extract_fields() with no languages argument at
    all must behave exactly as it did before Phase 3 — the whole point of
    languages defaulting to None."""
    schema = [{"name": "invoice_number", "type": "string"}]
    doc = _FakeDoc({
        "full_text": "Invoice Number INV-1234",
        "regions": [_row_region(1, "Invoice Number INV-1234", 0)],
    })
    result = custom_engine.extract_fields(doc, schema)
    assert result["fields"][0]["value"] == "INV-1234"


def test_custom_engine_never_translates_extracted_value():
    """The extracted value must be the literal original-script text, not
    a translated/transliterated one — this engine has no translation
    capability at all (by design), but this test pins that contract."""
    schema = [{"name": "vendor_name", "type": "string"}]
    doc = _FakeDoc({
        "full_text": "ಮಾರಾಟಗಾರ ಎಬಿಸಿ ಟೆಕ್ನಾಲಜೀಸ್",
        "regions": [_row_region(1, "ಮಾರಾಟಗಾರ ಎಬಿಸಿ ಟೆಕ್ನಾಲಜೀಸ್", 0)],
    })
    result = custom_engine.extract_fields(doc, schema, languages=["kn"])
    value = result["fields"][0]["value"]
    assert value is not None
    assert "ಎಬಿಸಿ" in value or "ಟೆಕ್ನಾಲಜೀಸ್" in value  # original Kannada script, never "ABC Technologies"


# ── ensure_ascii=False — the real Unicode-preservation bug fix ──────

def test_custom_engine_rawjson_preserves_unicode_not_escaped():
    schema = [{"name": "vendor_name", "type": "string"}]
    doc = _FakeDoc({
        "full_text": "ವಿಕ್ರೇತ ಕಂಪನಿ ಹೆಸರು",
        "regions": [_row_region(1, "ವಿಕ್ರೇತ ಕಂಪನಿ ಹೆಸರು", 0)],
    })
    result = custom_engine.extract_fields(doc, schema, languages=["kn"])
    raw = result["rawJson"]
    assert "\\u0c" not in raw  # no escaped Kannada Unicode block sequences
    # Round-trips to the exact same value either way — the fix is about
    # the raw string's own bytes, not correctness of parsed content.
    assert json.loads(raw) == {"vendor_name": result["fields"][0]["value"]}


@pytest.mark.asyncio
async def test_json_column_preserves_real_kannada_bytes_in_database(db, make_org):
    """The actual bug found live during repository analysis: SQLAlchemy's
    JSON column type defaults to ensure_ascii=True unless the engine's own
    json_serializer says otherwise (see database/connection.py's fix).
    This asserts the real, current app engine — not a hand-rolled one —
    stores genuine UTF-8 Kannada bytes, not \\uXXXX escapes."""
    org = await make_org(name="Multilingual Unicode Test Org")
    row = OrganizationSettings(organization_id=org.id, values={"_test": {"kannada": KANNADA_TEXT}})
    db.add(row)
    await db.commit()

    raw = (await db.execute(text('SELECT "values" FROM organization_settings WHERE organization_id = :oid'), {"oid": org.id})).scalar_one()
    assert KANNADA_TEXT in raw  # real glyphs present verbatim in the stored JSON text
    assert "\\u0c" not in raw  # not escaped


# ── Settings — backward-compatible merge for pre-Phase-3 rows ───────

def test_settings_merge_adds_new_language_fields_to_old_stored_row():
    """An org whose settings row predates this phase only has the original
    3 document_intelligence keys stored — _effective_values() must still
    produce all the new ones from DEFAULT_SETTINGS, per settings_service's
    own documented guarantee ('adding a new field later never breaks
    existing rows')."""
    pre_phase3_row = OrganizationSettings(
        organization_id=999,
        values={"document_intelligence": {"ocr_provider": "tesseract", "default_ocr_language": "en", "default_confidence_threshold": 85}},
    )
    effective = _effective_values(pre_phase3_row)
    di = effective["document_intelligence"]
    assert di["ocr_provider"] == "tesseract"  # old stored value preserved
    assert di["supported_languages"] == ["en"]  # new field, filled from DEFAULT_SETTINGS
    assert di["primary_language"] == "auto"
    assert di["translation_enabled"] is False


def test_default_settings_has_english_only_by_default():
    """Multilingual support must be opt-in — an org's default configuration
    changes nothing about today's English-only behavior until an admin
    explicitly adds languages."""
    assert DEFAULT_SETTINGS["document_intelligence"]["supported_languages"] == ["en"]
    assert DEFAULT_SETTINGS["document_intelligence"]["translation_enabled"] is False
