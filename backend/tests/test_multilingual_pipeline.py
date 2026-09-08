"""
Multilingual Document Processing, Phase 4 — real pipeline wiring tests.

Phase 3 (tests/test_multilingual.py) covered the detection/config
infrastructure in isolation. This file covers the actual Tesseract
integration: verify_language_available()'s real error path, the
two-pass detect-then-select OCR flow against real Kannada/Tamil/Hindi
text rendered into actual PNG/PDF files with real system fonts (never
injecting expected text directly into OCR output — approved decision
#2), the English-only-default regression guarantee, and the real
document_service.process_document() wiring end to end.

Requires the `tesseract` binary AND the kan/tam/hin language packs on
PATH (macOS: `brew install tesseract tesseract-lang`). Every real-OCR
test here is skipped, not failed, if a required package is missing —
this suite verifies real behavior, it doesn't fabricate it when the
environment can't support it.
"""
import io
import shutil

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.models.document import DocumentStatus, DocumentType
from app.models.review_queue import ReviewType
from app.ocr import engine as ocr_engine
from app.ocr.language_config import LANGUAGE_CONFIG
from app.ocr.language_errors import LanguageErrorCode, LanguageProcessingError
from app.ocr.multilingual_ocr_service import build_health_report, evaluate_ocr_quality, run_multilingual_ocr
from app.services import document_service, review_queue_service

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")

KAN_FONT = "/System/Library/Fonts/NotoSansKannada.ttc"
DEV_FONT = "/System/Library/Fonts/Supplemental/DevanagariMT.ttc"
LATIN_FONT = "/System/Library/Fonts/Helvetica.ttc"

_INSTALLED = ocr_engine.get_installed_languages() if shutil.which("tesseract") else set()
_HAS_KAN_TAM_HIN = {"kan", "tam", "hin"}.issubset(_INSTALLED)
needs_indic_packs = pytest.mark.skipif(
    not _HAS_KAN_TAM_HIN,
    reason="kan/tam/hin Tesseract language packs not installed (brew install tesseract-lang)",
)


def _render_png(lines: list[tuple[str, str, int]], size=(1000, 400)) -> bytes:
    """Real image, real fonts, real rendered glyphs — never injecting
    text directly into an OCR result (approved decision #2)."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    y = 40
    for text, font_path, fsize in lines:
        font = ImageFont.truetype(font_path, fsize)
        draw.text((30, y), text, fill="black", font=font)
        y += fsize + 30
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_pdf(pages: list[list[tuple[str, str, int]]]) -> bytes:
    pdf = pymupdf.open()
    for lines in pages:
        png_bytes = _render_png(lines)
        img = Image.open(io.BytesIO(png_bytes))
        page = pdf.new_page(width=img.width, height=img.height)
        page.insert_image(pymupdf.Rect(0, 0, img.width, img.height), stream=png_bytes)
    out = pdf.tobytes()
    pdf.close()
    return out


# ── verify_language_available() — real error path ──────────────────────

def test_verify_language_available_raises_for_missing_model():
    with pytest.raises(LanguageProcessingError) as exc_info:
        ocr_engine.verify_language_available("kan+zzz_not_a_real_code")
    assert exc_info.value.code == LanguageErrorCode.OCR_LANGUAGE_MODEL_MISSING
    assert exc_info.value.details["missing"] == ["zzz_not_a_real_code"]


def test_verify_language_available_passes_for_english():
    ocr_engine.verify_language_available("eng")  # must not raise — eng ships with every Tesseract install


# ── English-only default — byte-identical regression guarantee ─────────

def test_default_org_produces_identical_ocr_to_pre_phase4_run_ocr():
    png_bytes = _render_png([("Invoice Number: INV-2026-0042", LATIN_FONT, 34)])

    old = ocr_engine.run_ocr(png_bytes, DocumentType.IMAGE)  # the exact pre-Phase-4 call signature/behavior
    new = run_multilingual_ocr(
        png_bytes, DocumentType.IMAGE, supported_languages=["en"], primary_language="auto",
    )

    assert new["full_text"] == old["full_text"]
    assert new["confidence"] == old["confidence"]
    assert new["regions"] == old["regions"]
    # The only difference: honest new metadata, never altering the real OCR output.
    assert new["language"]["primary_language"] == "en"
    assert new["language"]["language_detection_method"] == "SCRIPT_BLOCK"


# ── Pinned primary_language — no detection, real config-driven OCR ──────

@needs_indic_packs
def test_pinned_primary_language_skips_detection():
    png_bytes = _render_png([("ಸರಕುಪಟ್ಟಿ ಸಂಖ್ಯೆ", KAN_FONT, 44)])
    result = run_multilingual_ocr(
        png_bytes, DocumentType.IMAGE, supported_languages=["en", "kn"], primary_language="kn",
    )
    assert result["language"]["language_detection_method"] == "PINNED"
    assert result["language"]["primary_language"] == "kn"
    # Real OCR text — actual Kannada glyphs recognized, not empty/garbled.
    assert any("ಀ" <= ch <= "೿" for ch in result["full_text"])


# ── Real two-pass multilingual flow — page-aware, matching the user's own example ──

@needs_indic_packs
def test_multipage_document_detects_and_reocrs_per_page():
    file_bytes = _render_pdf([
        [("ಸ್ವಾಗತ ಇನ್ವಾಯ್ಸ್ ಸಂಖ್ಯೆ ಕನ್ನಡ ದಾಖಲೆ", KAN_FONT, 40)],                      # Page 1: pure Kannada
        [("ಗ್ರಾಹಕ ಹೆಸರು ವಿಳಾಸ ಸಂಖ್ಯೆ ಮಾಹಿತಿ", KAN_FONT, 36), ("Total", LATIN_FONT, 30)],  # Page 2: Kannada-dominant + English
        [("चालान संख्या तारीख ग्राहक नाम पता", DEV_FONT, 38)],                           # Page 3: Hindi
    ])
    result = run_multilingual_ocr(
        file_bytes, DocumentType.PDF, supported_languages=["en", "kn", "ta", "hi"], primary_language="auto",
    )
    lang = result["language"]
    assert lang["is_multilingual"] is True
    pages_by_number = {p["page_number"]: p for p in lang["pages"]}

    assert pages_by_number[1]["primary_language"] == "kn"
    assert pages_by_number[3]["primary_language"] == "hi"

    # Real per-page OCR text in the correct native script — proves the
    # second pass actually re-ran Tesseract with the detected language,
    # not just labeled English-model garbage as "Kannada"/"Hindi".
    by_page: dict[int, str] = {}
    for region in result["regions"]:
        by_page.setdefault(region["page"], "")
        by_page[region["page"]] += region["text"]
    assert any("ಀ" <= ch <= "೿" for ch in by_page[1]), "Page 1 text should contain real Kannada glyphs"
    assert any("ऀ" <= ch <= "ॿ" for ch in by_page[3]), "Page 3 text should contain real Devanagari glyphs"


# ── document_service.process_document() — real end-to-end wiring ───────

@pytest.mark.asyncio
async def test_process_document_stores_language_metadata_for_english_doc(db, make_org):
    """Regression guard: a plain English document must still complete
    normally and get a real, honest "language" block — no behavior
    change to the actual pipeline outcome."""
    org = await make_org()
    png_bytes = _render_png([("Invoice Number: INV-9001  Total Due: $500.00", LATIN_FONT, 32)])
    doc = await document_service.save_from_source(
        db, content=png_bytes, filename="invoice.png", organization_id=org.id,
        source_connection_id=0, source_path="test/invoice.png", source_schedule_id=None,
    )
    doc = await document_service.process_document(db, doc)

    assert doc.status in (DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW)
    assert doc.ocr_result is not None
    assert doc.ocr_result.get("language") is not None
    assert doc.ocr_result["language"]["primary_language"] == "en"
    assert "Invoice Number" in doc.ocr_result["full_text"]


@needs_indic_packs
@pytest.mark.asyncio
async def test_process_document_detects_kannada_when_org_opts_in(db, make_org):
    """Real org configuration -> real different OCR behavior: an org that
    has opted into Kannada gets real Kannada-script OCR text and a
    populated per-page language block; an org that hasn't (the default)
    still only ever gets English (see test above) — proving the setting
    genuinely drives the pipeline, not just decoration."""
    org = await make_org()
    from app.services import settings_service
    row = await settings_service.get_or_create(db, org.id)
    values = settings_service._effective_values(row)
    values["document_intelligence"]["supported_languages"] = ["en", "kn"]
    row.values = values
    await db.commit()

    # A realistic multi-line snippet, not a single short line — Tesseract's
    # combined-language recognition (and script-block detection over its
    # output) needs enough real text to work reliably, the same practical
    # reason Tesseract's own OSD script classifier refuses ultra-sparse
    # images ("too few characters"); a single ~20-character line is not
    # representative of a real invoice page.
    png_bytes = _render_png([
        ("ಸರಕುಪಟ್ಟಿ ಸಂಖ್ಯೆ: 12345", KAN_FONT, 40),
        ("ಗ್ರಾಹಕ ಹೆಸರು ಮತ್ತು ವಿಳಾಸ ಮಾಹಿತಿ", KAN_FONT, 40),
        ("ಒಟ್ಟು ಮೊತ್ತ ಬಾಕಿ ಪಾವತಿ ದಿನಾಂಕ", KAN_FONT, 40),
    ], size=(1100, 500))
    doc = await document_service.save_from_source(
        db, content=png_bytes, filename="kannada_invoice.png", organization_id=org.id,
        source_connection_id=0, source_path="test/kannada_invoice.png", source_schedule_id=None,
    )
    doc = await document_service.process_document(db, doc)

    assert doc.ocr_result is not None
    assert doc.ocr_result["language"]["primary_language"] == "kn"
    assert any("ಀ" <= ch <= "೿" for ch in doc.ocr_result["full_text"])


# ── Phase 6: evaluate_ocr_quality() — real quality/mismatch checks ─────

_DEFAULT_KW = dict(ocr_confidence_threshold=85, language_mismatch_threshold=80, supported_languages=["en"], primary_language="auto")


def test_evaluate_ocr_quality_clean_result_has_no_flags():
    ocr_result = {
        "full_text": "Invoice Number: INV-1",
        "confidence": 95.0,
        "language": {"primary_language": "en", "language_detection_confidence": 100.0, "language_detection_method": "SCRIPT_BLOCK"},
    }
    assert evaluate_ocr_quality(ocr_result, **_DEFAULT_KW) == []


def test_evaluate_ocr_quality_empty_result():
    ocr_result = {"full_text": "   ", "confidence": None, "language": None}
    flags = evaluate_ocr_quality(ocr_result, **_DEFAULT_KW)
    assert [f["code"] for f in flags] == [LanguageErrorCode.OCR_EMPTY_RESULT.value]


def test_evaluate_ocr_quality_unicode_error():
    ocr_result = {
        "full_text": "Invoice � Number",
        "confidence": 95.0,
        "language": {"primary_language": "en", "language_detection_confidence": 100.0, "language_detection_method": "SCRIPT_BLOCK"},
    }
    flags = evaluate_ocr_quality(ocr_result, **_DEFAULT_KW)
    codes = [f["code"] for f in flags]
    assert LanguageErrorCode.OCR_UNICODE_ERROR.value in codes


def test_evaluate_ocr_quality_low_confidence():
    ocr_result = {
        "full_text": "Invoice Number: INV-1",
        "confidence": 40.0,
        "language": {"primary_language": "en", "language_detection_confidence": 100.0, "language_detection_method": "SCRIPT_BLOCK"},
    }
    flags = evaluate_ocr_quality(ocr_result, **_DEFAULT_KW)
    assert any(f["code"] == LanguageErrorCode.OCR_LOW_CONFIDENCE.value for f in flags)


def test_evaluate_ocr_quality_language_not_supported():
    ocr_result = {
        "full_text": "ஒரு கடிதம்",
        "confidence": 95.0,
        "language": {"primary_language": "ta", "language_detection_confidence": 96.0, "language_detection_method": "SCRIPT_BLOCK"},
    }
    # org only supports English — a decisively-detected Tamil document is a
    # real mismatch, not just low-confidence detection.
    flags = evaluate_ocr_quality(ocr_result, **_DEFAULT_KW)
    assert any(f["code"] == LanguageErrorCode.OCR_LANGUAGE_NOT_SUPPORTED.value for f in flags)


def test_evaluate_ocr_quality_language_mismatch_ambiguous():
    ocr_result = {
        "full_text": "some ambiguous text",
        "confidence": 95.0,
        "language": {"primary_language": "en", "language_detection_confidence": 55.0, "language_detection_method": "SCRIPT_BLOCK"},
    }
    flags = evaluate_ocr_quality(ocr_result, **_DEFAULT_KW)
    assert any(f["code"] == LanguageErrorCode.OCR_LANGUAGE_MISMATCH.value for f in flags)


def test_evaluate_ocr_quality_pinned_language_skips_mismatch_check():
    # Pinned primary_language -> no real detection ran -> nothing to
    # second-guess; method="PINNED" must suppress the mismatch/not-supported
    # checks even though "auto" is not literally passed here.
    ocr_result = {
        "full_text": "ಕೆಲವು ಪಠ್ಯ",
        "confidence": 95.0,
        "language": {"primary_language": "kn", "language_detection_confidence": 100.0, "language_detection_method": "PINNED"},
    }
    flags = evaluate_ocr_quality(
        ocr_result, ocr_confidence_threshold=85, language_mismatch_threshold=80,
        supported_languages=["en"], primary_language="kn",
    )
    assert flags == []


# ── Phase 6: end-to-end Review Queue routing ────────────────────────────

@pytest.mark.asyncio
async def test_low_confidence_ocr_creates_ocr_type_review_item(db, make_org):
    """A document whose OCR confidence is genuinely below the org's own
    ocr_confidence_threshold, ocr_confidence_threshold ) must land in
    Review Queue with a real ReviewType.OCR item — the exact case
    create_from_document() alone can't handle: every extracted field is
    superficially "valid" (clean, high-confidence fields; nothing missing,
    nothing invalid), so _pick_primary_issue() finds nothing to flag and
    returns None. This deterministically builds that exact combination
    (real Document row via save_from_source(), real OCR quality flags
    from evaluate_ocr_quality(), a clean extraction_result set directly —
    the same shape process_document() itself produces) rather than
    relying on a live Tesseract pass' field-level output, which can
    non-deterministically ALSO trip an extraction-field flag depending on
    minor real OCR noise (verified live: create_from_document() correctly
    takes priority over the OCR fallback whenever a real field-level issue
    exists — this test isolates the fallback path itself)."""
    org = await make_org()
    png_bytes = _render_png([("Invoice Number: INV-9002  Total Due: $250.00", LATIN_FONT, 32)])
    doc = await document_service.save_from_source(
        db, content=png_bytes, filename="low_conf_invoice.png", organization_id=org.id,
        source_connection_id=0, source_path="test/low_conf_invoice.png", source_schedule_id=None,
    )
    quality_flags = evaluate_ocr_quality(
        {"full_text": "Invoice Number: INV-9002", "confidence": 40.0,
         "language": {"primary_language": "en", "language_detection_confidence": 100.0, "language_detection_method": "SCRIPT_BLOCK"}},
        ocr_confidence_threshold=85, language_mismatch_threshold=80, supported_languages=["en"], primary_language="auto",
    )
    assert any(f["code"] == LanguageErrorCode.OCR_LOW_CONFIDENCE.value for f in quality_flags)
    doc.ocr_result = {"full_text": "Invoice Number: INV-9002", "confidence": 40.0, "regions": [], "pages": [], "quality_flags": quality_flags}
    doc.extraction_result = {
        "fields": [{"name": "Invoice Number", "value": "INV-9002", "confidence": 98.0, "missing": False, "validation_status": "valid"}],
        "business_rule_flag": False,
    }
    doc.status = DocumentStatus.NEEDS_REVIEW
    await db.commit()
    await db.refresh(doc)

    # Same real fallback chain _process_in_background uses.
    item = await review_queue_service.create_from_document(db, doc)
    assert item is None, "a document with only clean/valid fields should give create_from_document() nothing to flag"
    item = await review_queue_service.create_from_ocr_quality(db, doc, quality_flags)
    assert item is not None
    assert item.review_type == ReviewType.OCR
    assert item.document_id == doc.id
    assert "confidence" in item.issue.lower()
    assert item.document_id == doc.id


# ── Phase 11: OCR health-check report ───────────────────────────────────

@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_health_report_all_installed_on_this_dev_machine():
    # This dev environment has every LANGUAGE_CONFIG traineddata installed
    # (verified separately) — real, not mocked: exercises the "everything
    # ready" branch against the actual server state.
    report = build_health_report(["en", "kn"])
    assert report["tesseract_available"] is True
    assert report["all_configured_languages_ready"] is True
    codes = {l["code"]: l for l in report["languages"]}
    assert codes["en"]["installed"] is True
    assert codes["kn"]["installed"] is True
    assert codes["kn"]["tesseract_code"] == "kan"


def test_health_report_flags_missing_language_pack(monkeypatch):
    # A real dev/CI machine may have every pack installed (this one does),
    # so the "missing" branch is exercised here by making
    # get_installed_languages() report a real-shaped but incomplete set —
    # the exact scenario an admin hits when they enable a language in
    # Settings before installing its traineddata file.
    monkeypatch.setattr(ocr_engine, "get_installed_languages", lambda: {"eng"})
    report = build_health_report(["en", "kn", "ta"])
    assert report["tesseract_available"] is True
    assert report["all_configured_languages_ready"] is False
    codes = {l["code"]: l for l in report["languages"]}
    assert codes["en"]["installed"] is True
    assert codes["kn"]["installed"] is False
    assert "kan" in codes["kn"]["reason"]
    assert codes["ta"]["installed"] is False


def test_health_report_binary_missing_reports_every_language_unavailable(monkeypatch):
    def _raise():
        raise ocr_engine.OcrEngineError("The tesseract OCR binary is not available on this server.")
    monkeypatch.setattr(ocr_engine, "get_installed_languages", _raise)
    report = build_health_report(["en", "kn"])
    assert report["tesseract_available"] is False
    assert report["all_configured_languages_ready"] is False
    assert all(l["installed"] is False for l in report["languages"])
    assert "not available" in report["languages"][0]["reason"]


def test_health_report_unrecognized_language_code():
    report = build_health_report(["en", "zz"])
    codes = {l["code"]: l for l in report["languages"]}
    assert codes["zz"]["installed"] is False
    assert codes["zz"]["tesseract_code"] is None


def test_health_report_defaults_to_every_configured_language_when_none_given():
    report = build_health_report(None)
    assert len(report["languages"]) == len(LANGUAGE_CONFIG)
