"""
hybrid_detector.py — Multilingual Document Processing, Phase 3.

Orchestrates the two-layer architecture from the approved decision (Sep
2026):
  1. ScriptBlockLanguageDetector always runs first — deterministic, free,
     no model. For a script that maps unambiguously to one supported
     language at a decisive share, this is the whole answer.
  2. FastTextLanguageDetector is consulted ONLY when layer 1 alone can't
     be trusted:
       - the dominant script is genuinely shared by more than one
         supported language (Devanagari: Hindi/Marathi; Bengali: Bengali/
         Assamese — see ocr/language_config.is_script_ambiguous()), or
       - no single script clearly dominates (real mixed-language text),
         or
       - the dominant script is LATIN, where a Unicode-block count alone
         can't tell English from any other Latin-script language, and
         FastText's real classifier is the whole reason this build has a
         secondary layer at all.
     and only when there's "sufficient OCR text" (FastTextLanguageDetector's
     own MIN_TEXT_LENGTH gate) — matching the approved decision's own
     wording exactly.

Graceful, honest degradation: if FastText isn't available (no model file
configured — see fasttext_detector.py's own docstring), this returns the
script-block-only result rather than raising — script-block detection is
itself a real, complete, working layer on its own; FastText only ever
sharpens an already-ambiguous case, its absence never blocks the
pipeline. langdetect is deliberately NOT used anywhere in this module or
its callers, per the approved architecture.

NEVER translates. This module's only job is "what language is this,"
never "what does this say in English."
"""
from __future__ import annotations

from app.ocr.language_detection.base import LanguageCandidate, LanguageDetectionResult, LanguageDetectionUnavailableError, LanguageDetector
from app.ocr.language_detection.script_block_detector import ScriptBlockLanguageDetector

# Above this share, the script-block layer's top candidate is trusted
# outright without consulting FastText — a document that's, say, 95%
# Kannada characters doesn't need a second opinion.
DECISIVE_SHARE = 90.0


class HybridLanguageDetector(LanguageDetector):
    def __init__(self, fasttext_detector: LanguageDetector | None = None):
        self._script_detector = ScriptBlockLanguageDetector()
        self._fasttext_detector = fasttext_detector  # injected, not constructed here — see language_detection/__init__.py's factory

    @property
    def name(self) -> str:
        return "HYBRID"

    def detect(self, text: str) -> LanguageDetectionResult:
        script_result = self._script_detector.detect(text)
        if script_result.primary_language is None:
            return script_result  # INSUFFICIENT_TEXT — nothing more either layer can do

        top = script_result.candidates[0]
        script_ambiguous = self._script_detector.dominant_script_is_ambiguous(text)
        needs_second_opinion = script_ambiguous or top.confidence < DECISIVE_SHARE or top.language == "en"

        if not needs_second_opinion:
            return script_result

        if self._fasttext_detector is None or not self._fasttext_detector.is_available:
            # Honest degradation, not a failure — see module docstring.
            return script_result

        try:
            ft_result = self._fasttext_detector.detect(text)
        except LanguageDetectionUnavailableError:
            return script_result

        if ft_result.primary_language is None:
            return script_result  # FastText had nothing usable to add (text too short for it) — keep the script-block answer

        merged = _merge_candidates(script_result.candidates, ft_result.candidates)
        primary = merged[0].language if merged else script_result.primary_language
        return LanguageDetectionResult(primary_language=primary, candidates=merged, method="SCRIPT_BLOCK+FASTTEXT")


def _merge_candidates(a: list[LanguageCandidate], b: list[LanguageCandidate]) -> list[LanguageCandidate]:
    """Averages confidence per language across both detectors' candidate
    lists (a language only one layer reported keeps that layer's own
    number, not silently halved) — real arithmetic on two real signals,
    not a fabricated blend."""
    totals: dict[str, list[float]] = {}
    for c in a + b:
        totals.setdefault(c.language, []).append(c.confidence)
    merged = [LanguageCandidate(language=lang, confidence=round(sum(vals) / len(vals), 1)) for lang, vals in totals.items()]
    merged.sort(key=lambda c: c.confidence, reverse=True)
    return merged
