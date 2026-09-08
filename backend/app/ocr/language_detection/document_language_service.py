"""
document_language_service.py — Multilingual Document Processing, Phase 3.

Makes language detection PAGE-aware, not just document-aware (explicit
requirement, Sep 2026):

    Document
     |-- Page 1 -> Kannada 96%
     |-- Page 2 -> Kannada 91% + English 9%
     |-- Page 3 -> English 88% + Kannada 12%
     `-- Page 4 -> Hindi 97%

detect_document_language() runs the injected LanguageDetector once per
OCR page (grouping app/ocr/engine.py's own `regions` list — each region
already carries a real `page` number — into per-page text) AND once more
over the whole document's `full_text`, for the document-level summary. A
single call is never made to stand in for both.

This is deliberately pipeline-agnostic: it only reads the same
`ocr_result` dict shape run_ocr() already returns (full_text + regions),
so it can be exercised and tested right now, independent of Phase 5's
"detect first, then pick per-page Tesseract lang codes" real wiring into
the live OCR pipeline. tesseract_lang_string_for_page() is provided now
as the exact building block that later wiring will call — this module
already knows how to turn "Kannada 91% + English 9%" into "kan+eng"; it
just isn't invoked from document_service.py yet.

NEVER translates — this only ever reports what language(s) each page
appears to be in, exactly the same guarantee LanguageDetector itself
makes (see language_detection/base.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ocr.language_config import tesseract_code_for
from app.ocr.language_detection.base import LanguageCandidate, LanguageDetector

# A language must clear this share of a page's scripted text to be
# counted as genuinely present on that page (and therefore folded into
# the document's detected_languages set / a page's Tesseract lang
# string) — filters out incidental noise (a single stray Latin word on
# an otherwise all-Kannada page) without hiding real, substantial
# secondary-language content (a Kannada invoice with an English address
# line).
INCLUSION_THRESHOLD = 8.0


@dataclass(frozen=True)
class PageLanguageResult:
    page_number: int
    primary_language: str | None
    languages: list[LanguageCandidate] = field(default_factory=list)
    method: str = "UNKNOWN"


@dataclass(frozen=True)
class DocumentLanguageResult:
    primary_language: str | None
    detected_languages: list[str]  # union across all pages clearing INCLUSION_THRESHOLD, ordered by overall prevalence
    language_detection_confidence: float | None  # the document-level call's own top-candidate confidence
    language_detection_method: str
    pages: list[PageLanguageResult] = field(default_factory=list)

    @property
    def is_multilingual(self) -> bool:
        return len(self.detected_languages) > 1


def _regions_by_page(ocr_result: dict) -> dict[int, str]:
    by_page: dict[int, list[str]] = {}
    for region in ocr_result.get("regions") or []:
        page = region.get("page", 1)
        text = region.get("text") or ""
        if text:
            by_page.setdefault(page, []).append(text)
    return {page: " ".join(texts) for page, texts in by_page.items()}


def detect_document_language(ocr_result: dict, detector: LanguageDetector) -> DocumentLanguageResult:
    """`ocr_result` is exactly what app/ocr/engine.py's run_ocr() (or a
    cloud OCR connector's equivalent shape) returns: {"full_text",
    "regions": [{"page", "text", ...}], ...}. Runs `detector` per page AND
    once over the whole document — real per-page detection, not a
    document-level result copy-pasted onto every page."""
    page_texts = _regions_by_page(ocr_result)

    pages: list[PageLanguageResult] = []
    for page_number in sorted(page_texts):
        result = detector.detect(page_texts[page_number])
        included = [c for c in result.candidates if c.confidence >= INCLUSION_THRESHOLD]
        pages.append(PageLanguageResult(
            page_number=page_number,
            primary_language=result.primary_language,
            languages=included or result.candidates[:1],  # keep at least the top guess even if it's below threshold, honestly
            method=result.method,
        ))

    full_text = ocr_result.get("full_text") or ""
    doc_result = detector.detect(full_text)
    doc_top_confidence = doc_result.candidates[0].confidence if doc_result.candidates else None

    # Union of every page's genuinely-present languages, ordered by how
    # many pages each appears on (ties broken by first appearance) — a
    # real prevalence ranking, not alphabetical.
    prevalence: dict[str, int] = {}
    for p in pages:
        for c in p.languages:
            if c.confidence >= INCLUSION_THRESHOLD:
                prevalence[c.language] = prevalence.get(c.language, 0) + 1
    detected_languages = sorted(prevalence, key=lambda lang: prevalence[lang], reverse=True)
    if not detected_languages and doc_result.primary_language:
        detected_languages = [doc_result.primary_language]  # single-page/no-region-data documents still get a real answer

    return DocumentLanguageResult(
        primary_language=doc_result.primary_language,
        detected_languages=detected_languages,
        language_detection_confidence=doc_top_confidence,
        language_detection_method=doc_result.method,
        pages=pages,
    )


def tesseract_lang_string_for_page(page_result: PageLanguageResult, max_languages: int = 2) -> str | None:
    """Turns "Kannada 91% + English 9%" into "kan+eng" — the exact
    building block Phase 5's real multilingual OCR provider will use to
    pick each page's Tesseract `lang=` argument (see ocr/engine.py's own
    docstring on why it currently passes none at all). Not called from
    the live OCR pipeline yet — this module is deliberately pipeline-
    agnostic (see module docstring). None if the page's primary language
    isn't configured (ocr/language_config.py) — caller decides whether
    that's a hard failure (OCR_LANGUAGE_NOT_SUPPORTED) or a fallback."""
    if page_result.primary_language is None:
        return None
    codes = []
    for candidate in sorted(page_result.languages, key=lambda c: c.confidence, reverse=True)[:max_languages]:
        tcode = tesseract_code_for(candidate.language)
        if tcode and tcode not in codes:
            codes.append(tcode)
    return "+".join(codes) if codes else None
