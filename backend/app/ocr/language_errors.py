"""
language_errors.py — Multilingual Document Processing, Phase 3.

The explicit, named failure states this feature must be able to raise
(spec section 51) — settled now, in Phase 3, so later phases (5/6: the
real multilingual OCR provider + OCR validation) share one consistent
vocabulary instead of each inventing its own ad-hoc error string.

Every one of these is a REAL failure state, never a stand-in for "OCR
succeeded but we're not confident" being silently reported as success.
Per the user's own explicit instruction: a missing language model must
produce a real configuration/error state, never a silent fallback to
English; low-confidence/mismatched OCR must route to Review Queue, never
be accepted as-is.
"""
from __future__ import annotations

import enum


class LanguageErrorCode(str, enum.Enum):
    LANGUAGE_DETECTION_FAILED = "LANGUAGE_DETECTION_FAILED"
    OCR_LANGUAGE_NOT_SUPPORTED = "OCR_LANGUAGE_NOT_SUPPORTED"
    OCR_LANGUAGE_MODEL_MISSING = "OCR_LANGUAGE_MODEL_MISSING"
    OCR_LANGUAGE_MISMATCH = "OCR_LANGUAGE_MISMATCH"
    OCR_UNICODE_ERROR = "OCR_UNICODE_ERROR"
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"
    OCR_EMPTY_RESULT = "OCR_EMPTY_RESULT"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"


class LanguageProcessingError(Exception):
    """Real, structured failure for the multilingual OCR/detection path —
    always carries one of the codes above so a caller (document_service.py
    in a later phase) can react specifically (e.g. route to Review Queue
    on OCR_LOW_CONFIDENCE/OCR_LANGUAGE_MISMATCH, hard-fail with a real
    admin-visible message on OCR_LANGUAGE_MODEL_MISSING) rather than
    pattern-matching a free-text message string."""

    def __init__(self, code: LanguageErrorCode, message: str, **details):
        self.code = code
        self.details = details
        super().__init__(f"{code.value}: {message}")
