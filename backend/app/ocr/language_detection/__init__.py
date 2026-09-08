"""
language_detection package — Multilingual Document Processing, Phase 3.

Public surface: the LanguageDetector interface (base.py), each concrete
provider, and build_default_detector() — the one place that wires the
real hybrid architecture together (script-block first, FastText as a
provider-injected secondary layer). Callers (document_language_service.py,
and Phase 5's real OCR pipeline) should generally just call
build_default_detector() rather than constructing HybridLanguageDetector
by hand, so the wiring stays in one place per this project's own
"centralize it" instruction.
"""
from app.ocr.language_detection.base import (
    LanguageCandidate,
    LanguageDetectionResult,
    LanguageDetectionUnavailableError,
    LanguageDetector,
)
from app.ocr.language_detection.fasttext_detector import FastTextLanguageDetector
from app.ocr.language_detection.hybrid_detector import HybridLanguageDetector
from app.ocr.language_detection.script_block_detector import ScriptBlockLanguageDetector

__all__ = [
    "LanguageCandidate",
    "LanguageDetectionResult",
    "LanguageDetectionUnavailableError",
    "LanguageDetector",
    "FastTextLanguageDetector",
    "HybridLanguageDetector",
    "ScriptBlockLanguageDetector",
    "build_default_detector",
]


def build_default_detector(fasttext_model_path: str | None = None) -> HybridLanguageDetector:
    """Real hybrid detector: script-block always available; FastText
    wired in as an optional secondary layer that degrades honestly (see
    hybrid_detector.py's own docstring) if no model file is configured."""
    return HybridLanguageDetector(fasttext_detector=FastTextLanguageDetector(model_path=fasttext_model_path))
