"""
script_block_detector.py — Multilingual Document Processing, Phase 3.

The deterministic FIRST layer of the hybrid detection architecture
(approved decision, Sep 2026): counts how many of a text's letter
characters fall into each script's Unicode block (ocr/language_config.py
SCRIPT_UNICODE_RANGES), with zero external dependency and zero model —
just real Unicode code-point arithmetic on the actual OCR text. This is
what tells Kannada/Tamil/Devanagari text apart from Latin/English
reliably and cheaply, and it's what HybridLanguageDetector always runs
first (see that module's own docstring for when it's sufficient on its
own vs. when it escalates to FastText).

Genuinely cannot resolve on its own: a script shared by more than one
supported language (DEVANAGARI: Hindi vs Marathi; BENGALI: Bengali vs
Assamese — see ocr/language_config.is_script_ambiguous()). This detector
reports its best tentative single-language guess for those via
SCRIPT_LANGUAGE_DEFAULT (so it never returns nothing), but also exposes
that a script was ambiguous so the hybrid layer above knows to seek a
second opinion rather than trusting this guess blindly.
"""
from __future__ import annotations

from app.ocr.language_config import SCRIPT_LANGUAGE_DEFAULT, SCRIPT_UNICODE_RANGES, is_script_ambiguous
from app.ocr.language_detection.base import LanguageCandidate, LanguageDetectionResult, LanguageDetector


def _script_of_char(ch: str) -> str | None:
    cp = ord(ch)
    for script, ranges in SCRIPT_UNICODE_RANGES.items():
        for lo, hi in ranges:
            if lo <= cp <= hi:
                return script
    return None


class ScriptBlockLanguageDetector(LanguageDetector):
    @property
    def name(self) -> str:
        return "SCRIPT_BLOCK"

    def detect(self, text: str) -> LanguageDetectionResult:
        counts: dict[str, int] = {}
        scripted_total = 0
        for ch in text:
            script = _script_of_char(ch)
            if script is None:
                continue  # digits, punctuation, whitespace — not script-bearing, excluded from the denominator
            counts[script] = counts.get(script, 0) + 1
            scripted_total += 1

        if scripted_total == 0:
            # Real, honest "nothing scripted here to go on" — e.g. an
            # all-numeric/punctuation snippet. Never guessed at.
            return LanguageDetectionResult(primary_language=None, candidates=[], method="INSUFFICIENT_TEXT")

        candidates: list[LanguageCandidate] = []
        for script, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            share = round(100.0 * count / scripted_total, 1)
            language = SCRIPT_LANGUAGE_DEFAULT.get(script)
            if language is None:
                continue  # a script this build has no LANGUAGE_CONFIG entry for at all
            candidates.append(LanguageCandidate(language=language, confidence=share))

        primary = candidates[0].language if candidates else None
        return LanguageDetectionResult(primary_language=primary, candidates=candidates, method=self.name)

    def dominant_script_is_ambiguous(self, text: str) -> bool:
        """True when the single most common script in `text` is one more
        than one supported language shares (Devanagari/Bengali) — the
        exact signal HybridLanguageDetector uses to decide whether this
        layer's guess needs a FastText second opinion before being
        trusted as the final answer."""
        counts: dict[str, int] = {}
        for ch in text:
            script = _script_of_char(ch)
            if script:
                counts[script] = counts.get(script, 0) + 1
        if not counts:
            return False
        dominant = max(counts, key=counts.get)
        return is_script_ambiguous(dominant)
