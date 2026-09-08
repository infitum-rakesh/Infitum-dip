"""
language_detection/base.py — Multilingual Document Processing, Phase 3.

The provider/interface this feature's hybrid detection architecture is
built on (approved decision, Sep 2026: "Design the implementation using
a provider/interface pattern so additional languages and detection
providers can be added later without modifying the core document-
processing pipeline"). A caller (document_language_service.py, and
eventually document_service.py in Phase 5) only ever talks to a
LanguageDetector — never to ScriptBlockLanguageDetector or
FastTextLanguageDetector directly — so a future third detection layer
(a different classifier, a cloud language-ID API, ...) is a new class
implementing this same interface, not a change to every call site.

Detection NEVER translates — every detector here only ever reports what
language(s) a span of text appears to be, never rewrites the text
itself. See extraction/custom_engine.py and extraction_service.py's own
docstrings for the separate, explicit "never translate the extracted
value" guarantee this project makes elsewhere in the pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageCandidate:
    """One language guess with its share/confidence, 0-100. For
    ScriptBlockLanguageDetector this is "% of scripted characters in this
    script's Unicode block"; for FastTextLanguageDetector it's the
    model's own softmax probability * 100 — both are genuine, computed
    numbers, never a fabricated placeholder."""
    language: str  # ISO 639-1 code, e.g. "kn" — see ocr/language_config.py
    confidence: float  # 0-100


@dataclass(frozen=True)
class LanguageDetectionResult:
    primary_language: str | None  # None is a real, honest "couldn't tell" — never a guess
    candidates: list[LanguageCandidate] = field(default_factory=list)
    method: str = "UNKNOWN"  # e.g. "SCRIPT_BLOCK", "FASTTEXT", "SCRIPT_BLOCK+FASTTEXT", "INSUFFICIENT_TEXT"


class LanguageDetectionUnavailableError(Exception):
    """Raised by a detector whose real dependency isn't present right now
    (e.g. FastTextLanguageDetector with no lid model file on disk) —
    distinct from LanguageDetectionResult(primary_language=None), which
    means "the detector ran but genuinely couldn't tell." This means the
    detector didn't run at all. Callers (HybridLanguageDetector) catch
    this to degrade gracefully to the layer(s) that ARE available, never
    to fabricate a result."""


class LanguageDetector(ABC):
    @abstractmethod
    def detect(self, text: str) -> LanguageDetectionResult: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def is_available(self) -> bool:
        """True by default — detectors with a real external dependency
        (FastTextLanguageDetector) override this to report whether that
        dependency is actually present, without needing detect() to be
        called (and possibly raise) just to find out."""
        return True
