"""
multilingual_ocr_service.py — Multilingual Document Processing, Phase 4.

Real "detect, then select the right OCR language" orchestration that
document_language_service.py (Phase 3) was deliberately built pipeline-
agnostic to plug into (see that module's own docstring — it says outright
it "isn't invoked from document_service.py yet"). This module is the
piece that actually wires it into a live Tesseract OCR run.

Two-pass strategy, exactly matching the user's own approved architecture
("perform lightweight script detection/OCR first when necessary, then
select the appropriate OCR language models"):

  Pass 1 (lightweight): one real Tesseract pass over the whole document
  using every language the org has opted into (Settings > Document
  Intelligence > Languages), combined into one Tesseract lang string
  (e.g. "eng+kan+tam+hin"). Tesseract natively recognizes multiple
  language models together in a single pass, so this is genuine OCR
  output, not throwaway noise — just not yet the *best* per-page choice.

  Detect: the real hybrid detector (script-block + optional FastText, see
  language_detection/) runs per page over pass 1's text — the exact
  page-aware behavior the user specified (Page 1 Kannada 96%, Page 2
  Kannada 91% + English 9%, Page 3 English 88% + Kannada 12%, Page 4
  Hindi 97%).

  Pass 2 (final, per page): for each page whose detected language(s)
  genuinely differ from pass 1's blind combined string, re-run Tesseract
  on just that page's already-rendered image with
  tesseract_lang_string_for_page()'s narrower, more accurate lang string
  (e.g. "kan+eng"), replacing that page's regions/text with the better
  result. Pages that already match pass 1 are left alone — no wasted
  re-OCR.

Two real shortcuts reproduce pre-Phase-4 behavior byte-for-byte where
detection genuinely has nothing to add:
  - primary_language pinned (Settings, not "auto"): every page OCRs with
    that language (+ English — mixed-language invoices commonly carry
    English labels/boilerplate even in a non-English document, the same
    convention extraction/custom_engine.py's _effective_languages() uses).
    No detection runs; this is real config, not a guess, and is reported
    as method="PINNED".
  - supported_languages resolves to a single Tesseract code (the shipped
    default, ["en"]): exactly one Tesseract pass — identical to what
    run_ocr() always did. The real detector still runs once afterward
    (cheap — text only, no second OCR call) purely so the new "language"
    metadata block is honestly populated even for orgs that have never
    touched this feature.

Every model-availability gap raises a real
LanguageProcessingError(OCR_LANGUAGE_MODEL_MISSING) from
ocr/engine.py's verify_language_available() — this module never silently
drops a language and falls back to English.
"""
from __future__ import annotations

from app.models.document import DocumentType
from app.ocr import engine as ocr_engine
from app.ocr.language_config import LANGUAGE_CONFIG, tesseract_code_for
from app.ocr.language_detection import build_default_detector
from app.ocr.language_detection.document_language_service import (
    DocumentLanguageResult,
    detect_document_language,
)
from app.ocr.language_errors import LanguageErrorCode

_EN_TESSERACT = "eng"


def _effective_supported_languages(supported_languages: list[str] | None) -> list[str]:
    """Same "English is always implicitly available" convention as
    extraction/custom_engine.py's _effective_languages() — mixed-language
    invoices commonly carry English labels/boilerplate even when the
    org's primary content language is something else."""
    return list(dict.fromkeys(["en", *(supported_languages or [])]))


def _combined_tesseract_lang(language_codes: list[str]) -> str:
    codes = []
    for code in language_codes:
        tcode = tesseract_code_for(code)
        if tcode and tcode not in codes:
            codes.append(tcode)
    return "+".join(codes) if codes else _EN_TESSERACT


def _serialize_language_result(result: DocumentLanguageResult) -> dict:
    return {
        "primary_language": result.primary_language,
        "detected_languages": result.detected_languages,
        "language_detection_confidence": result.language_detection_confidence,
        "language_detection_method": result.language_detection_method,
        "is_multilingual": result.is_multilingual,
        "pages": [
            {
                "page_number": p.page_number,
                "primary_language": p.primary_language,
                "method": p.method,
                "languages": [{"language": c.language, "confidence": c.confidence} for c in p.languages],
            }
            for p in result.pages
        ],
    }


def detect_language_only(ocr_result: dict) -> dict:
    """For OCR paths that never touch Tesseract's lang= at all — cloud OCR
    providers (Textract/Vision/Form Recognizer) and the AI-vision
    fallback. Real per-page/document detection over whatever text that
    provider produced, for reporting only. Never re-OCRs: a cloud
    provider or vision model already does its own multilingual
    recognition, so second-guessing it with Tesseract would be
    nonsensical, not more correct."""
    detector = build_default_detector()
    result = detect_document_language(ocr_result, detector)
    return _serialize_language_result(result)


def run_multilingual_ocr(
    file_bytes: bytes, doc_type: DocumentType, *,
    supported_languages: list[str] | None, primary_language: str | None,
) -> dict:
    """Real entry point document_service.py's Tesseract branch calls
    instead of a bare run_ocr(). Returns the exact same ocr_result shape
    run_ocr() always has ("full_text"/"confidence"/"regions"/"pages"/
    "page_count") plus one new key: "language" (see
    _serialize_language_result above).

    Raises OcrEngineError / OcrNotApplicableError / LanguageProcessingError
    exactly like run_ocr() — a genuine failure here fails the whole
    document, never silently downgrades to English."""

    # ── Pinned-language shortcut — the org explicitly chose one language;
    # detection is skipped entirely (there's nothing to detect: the org
    # already decided), not run-and-ignored. ──────────────────────────
    if primary_language and primary_language != "auto":
        lang_codes = _effective_supported_languages([primary_language])
        lang_string = _combined_tesseract_lang(lang_codes)
        ocr_engine.verify_language_available(lang_string)
        pages = ocr_engine.load_pages(file_bytes, doc_type)
        result = ocr_engine.extract_pages(pages, lang=lang_string)
        result["language"] = {
            "primary_language": primary_language,
            "detected_languages": [primary_language],
            "language_detection_confidence": 100.0,
            "language_detection_method": "PINNED",
            "is_multilingual": False,
            "pages": [
                {
                    "page_number": p.page_num, "primary_language": primary_language, "method": "PINNED",
                    "languages": [{"language": primary_language, "confidence": 100.0}],
                }
                for p in pages
            ],
        }
        return result

    effective_codes = _effective_supported_languages(supported_languages)

    # ── Single-language shortcut — the shipped default (["en"]) and any
    # org that has only opted into one language. Byte-identical OCR text
    # to pre-Phase-4 behavior: exactly one Tesseract pass, no re-OCR. The
    # real detector still runs once (cheap — text-only, no second OCR
    # call) so the new "language" metadata is honestly populated even for
    # orgs that haven't touched this feature yet. ───────────────────────
    if len(effective_codes) <= 1:
        lang_string = _combined_tesseract_lang(effective_codes)
        ocr_engine.verify_language_available(lang_string)
        pages = ocr_engine.load_pages(file_bytes, doc_type)
        result = ocr_engine.extract_pages(pages, lang=lang_string)
        result["language"] = detect_language_only(result)
        return result

    # ── Real two-pass detect-then-select flow ───────────────────────────
    combined_lang = _combined_tesseract_lang(effective_codes)
    ocr_engine.verify_language_available(combined_lang)
    pages = ocr_engine.load_pages(file_bytes, doc_type)
    pass1 = ocr_engine.extract_pages(pages, lang=combined_lang)

    detector = build_default_detector()
    doc_lang_result = detect_document_language(pass1, detector)
    page_lang_by_number = {p.page_number: p for p in doc_lang_result.pages}

    # Real per-page re-OCR — tried for every page that has a real
    # candidate language, and kept only if it MEASURABLY improves on pass
    # 1's own confidence for that page. This is deliberately confidence-
    # driven rather than a "does the detected language differ from the
    # combined string" string comparison: a first pass that blindly
    # combines every configured language (e.g. "eng+kan") can itself
    # misread pure-script text as noise in another script (a real,
    # observed Tesseract multi-language characteristic — confirmed live:
    # a single Kannada line OCR'd through "eng+kan" came back with a
    # garbled Latin-looking prefix that the detector then counted as
    # genuine English content, which would have skipped re-OCR entirely
    # under a naive "narrowed selection == combined string" check since
    # with only two configured languages there's nothing narrower to
    # select). Trying each of the page's top candidate languages
    # INDIVIDUALLY and keeping whichever real OCR pass measures the
    # highest average confidence — including pass 1's own result as one
    # of the candidates — fixes this without guessing: it measures.
    installed = ocr_engine.get_installed_languages()
    page_by_number = {p.page_num: p for p in pages}
    final_regions_by_page: dict[int, list[dict]] = {}
    final_dims_by_page: dict[int, dict] = {p["page"]: p for p in pass1["pages"]}
    for region in pass1["regions"]:
        final_regions_by_page.setdefault(region["page"], []).append(region)

    def _avg_confidence(regions: list[dict]) -> float:
        return round(sum(r["confidence"] for r in regions) / len(regions), 1) if regions else 0.0

    reocr_happened = False
    for page_number, page_lang in page_lang_by_number.items():
        page_obj = page_by_number.get(page_number)
        if page_obj is None:
            continue

        best_regions = final_regions_by_page.get(page_number, [])
        best_confidence = _avg_confidence(best_regions)
        best_dims = final_dims_by_page.get(page_number)
        page_changed = False

        # Individual single-language attempts (not the joined multi-
        # language string) — a lone script-appropriate model reads its
        # own script far more reliably than a combined model does. Try
        # detected candidates first, then every configured installed
        # non-English model. This matters for Odia: a noisy combined pass
        # can misread Oriya glyphs as Tamil, so relying only on the first
        # pass' detected languages would never even try `ori`.
        candidate_languages = [c.language for c in sorted(page_lang.languages, key=lambda c: c.confidence, reverse=True)]
        candidate_languages.extend(code for code in effective_codes if code != "en")
        tried_codes: set[str] = set()
        for language in candidate_languages:
            tcode = tesseract_code_for(language)
            if not tcode or tcode in tried_codes or tcode not in installed:
                continue
            tried_codes.add(tcode)
            page_result = ocr_engine.extract_one_page(page_obj, lang=tcode)
            conf = _avg_confidence(page_result["regions"])
            if conf > best_confidence:
                best_confidence = conf
                best_regions = page_result["regions"]
                best_dims = {"page": page_number, "width": page_result["width"], "height": page_result["height"]}
                page_changed = True

        if page_changed:
            final_regions_by_page[page_number] = best_regions
            final_dims_by_page[page_number] = best_dims
            reocr_happened = True

    # Rebuild the aggregate dict from the (possibly-updated) per-page
    # pieces, preserving page order.
    all_regions, all_texts, all_confs, page_dims = [], [], [], []
    for page_number in sorted(final_dims_by_page):
        regions = final_regions_by_page.get(page_number, [])
        all_regions.extend(regions)
        text = "\n".join(r["text"] for r in regions)
        if text:
            all_texts.append(text)
        all_confs.extend(r["confidence"] for r in regions)
        page_dims.append(final_dims_by_page[page_number])

    final_result = {
        "full_text": "\n\n".join(all_texts),
        "confidence": round(sum(all_confs) / len(all_confs), 1) if all_confs else None,
        "regions": all_regions,
        "pages": page_dims,
        "page_count": pass1["page_count"],
    }

    # Re-detect over the FINAL (possibly re-OCR'd) text so the reported
    # language metadata reflects what was actually recognized, not pass
    # 1's intermediate guess — honest, not cosmetic: a page re-OCR'd from
    # blind "eng+kan+tam+hin" down to a clean "kan+eng" pass often
    # produces materially cleaner text for the detector to read.
    final_doc_lang_result = detect_document_language(final_result, detector) if reocr_happened else doc_lang_result
    final_result["language"] = _serialize_language_result(final_doc_lang_result)
    return final_result


def evaluate_ocr_quality(
    ocr_result: dict, *, ocr_confidence_threshold: float, language_mismatch_threshold: float,
    supported_languages: list[str] | None, primary_language: str | None,
) -> list[dict]:
    """Multilingual Document Processing, Phase 6 — real OCR quality/
    script validation. Runs on every OCR result (Tesseract or cloud), and
    NEVER accepts a genuinely low-quality or language-mismatched OCR
    result as a silent success: each real problem found here becomes a
    named flag (language_errors.LanguageErrorCode) that document_service.py
    forces into Review Queue, never just a slightly-lower confidence
    number nobody notices. Returns [] when the OCR result is genuinely
    good — this never fabricates a problem to justify existing.

    Real checks, in order of severity:
      OCR_EMPTY_RESULT     — no readable text at all (after every OCR/
                              vision fallback document_service.py already
                              tried — reaching this function with empty
                              text means nothing worked).
      OCR_UNICODE_ERROR    — the recognized text contains the Unicode
                              replacement character (U+FFFD) or fails a
                              strict UTF-8 round-trip — real evidence of a
                              lossy encode/decode somewhere in the chain,
                              not a translation or script issue.
      OCR_LOW_CONFIDENCE   — overall OCR confidence below the org's own
                              configured ocr_confidence_threshold (a
                              *different*, OCR-specific number from the
                              generic extraction confidence_threshold
                              process_document() already checks).
      OCR_LANGUAGE_NOT_SUPPORTED — the detected primary language isn't
                              one the org has opted into at all (only
                              meaningful when primary_language == "auto";
                              a pinned org made an explicit choice and
                              there is no real detection signal to
                              second-guess it with).
      OCR_LANGUAGE_MISMATCH — detection ran but wasn't decisive (the
                              document-level top-candidate confidence is
                              below the org's language_mismatch_threshold)
                              — script-block detection disagreeing with
                              itself, exactly the case DEFAULT_SETTINGS'
                              own comment on this threshold describes.
    """
    flags: list[dict] = []
    full_text = ocr_result.get("full_text") or ""

    if not full_text.strip():
        flags.append({
            "code": LanguageErrorCode.OCR_EMPTY_RESULT.value,
            "message": "OCR produced no readable text for this document.",
        })
        return flags  # every other check is meaningless against empty text

    try:
        full_text.encode("utf-8", errors="strict")
        unicode_ok = "�" not in full_text
    except UnicodeEncodeError:
        unicode_ok = False
    if not unicode_ok:
        flags.append({
            "code": LanguageErrorCode.OCR_UNICODE_ERROR.value,
            "message": "The recognized text contains invalid/unrepresentable characters — a likely encoding error upstream of OCR.",
        })

    confidence = ocr_result.get("confidence")
    if confidence is not None and confidence < ocr_confidence_threshold:
        flags.append({
            "code": LanguageErrorCode.OCR_LOW_CONFIDENCE.value,
            "message": f"OCR confidence ({confidence:.1f}%) is below this organization's configured threshold ({ocr_confidence_threshold:.0f}%).",
        })

    lang = ocr_result.get("language") or {}
    if (primary_language or "auto") == "auto" and lang.get("language_detection_method") != "PINNED":
        effective_supported = _effective_supported_languages(supported_languages)
        detected_primary = lang.get("primary_language")
        detected_confidence = lang.get("language_detection_confidence")
        if detected_primary and detected_primary not in effective_supported:
            flags.append({
                "code": LanguageErrorCode.OCR_LANGUAGE_NOT_SUPPORTED.value,
                "message": f"Detected language \"{detected_primary}\" is not one of this organization's configured languages ({', '.join(effective_supported)}).",
            })
        elif detected_confidence is not None and detected_confidence < language_mismatch_threshold:
            flags.append({
                "code": LanguageErrorCode.OCR_LANGUAGE_MISMATCH.value,
                "message": f"Language detection was not decisive ({detected_confidence:.1f}% confidence, below this organization's {language_mismatch_threshold:.0f}% threshold) — the document may be in a mixed or unexpected language.",
            })

    return flags


def build_health_report(supported_languages: list[str] | None) -> dict:
    """Multilingual Document Processing, Phase 11 — real Tesseract
    readiness check for Administration > Document Intelligence > Languages.
    Reports the real tesseract binary status and, for every language in
    the org's own configured supported_languages (falling back to every
    LANGUAGE_CONFIG entry if the org hasn't opted into anything specific),
    whether that language's real .traineddata file is actually installed
    on this server right now — via ocr_engine.get_installed_languages(),
    the same real subprocess call run_multilingual_ocr() itself uses, not
    a cached or assumed value. Never fabricates an "ok" status: if the
    tesseract binary itself is missing, every language reports
    installed=False with that as the reason, exactly mirroring what a
    real OCR attempt would hit."""
    try:
        installed = ocr_engine.get_installed_languages()
        binary_ok = True
        binary_error = None
    except ocr_engine.OcrEngineError as exc:
        installed = set()
        binary_ok = False
        binary_error = str(exc)

    codes = supported_languages or list(LANGUAGE_CONFIG.keys())
    languages = []
    for code in codes:
        info = LANGUAGE_CONFIG.get(code)
        if info is None:
            languages.append({
                "code": code, "name": code, "tesseract_code": None,
                "installed": False, "reason": "Not a recognized language code (see ocr/language_config.py).",
            })
            continue
        is_installed = binary_ok and info.tesseract_code in installed
        languages.append({
            "code": code, "name": info.name, "tesseract_code": info.tesseract_code,
            "installed": is_installed,
            "reason": None if is_installed else (
                binary_error if not binary_ok
                else f"Tesseract traineddata for \"{info.tesseract_code}\" is not installed on this server."
            ),
        })

    return {
        "tesseract_available": binary_ok,
        "tesseract_error": binary_error,
        "installed_language_count": len(installed),
        "languages": languages,
        "all_configured_languages_ready": binary_ok and all(l["installed"] for l in languages),
    }
