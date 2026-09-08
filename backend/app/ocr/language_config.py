"""
language_config.py — Multilingual Document Processing, Phase 3.

The SINGLE centralized place every part of this codebase looks up
language metadata from: ISO 639-1 code -> Tesseract traineddata code,
display name, Unicode script family, and FastText language-ID label.
Per the user's own explicit instruction ("Centralize it. Do not hardcode
this mapping throughout the application" — spec section 49), nothing
outside this module should ever hand-write a language-code mapping.

PRIORITY_LANGUAGES are the four this build ships real, curated Custom
Engine label dictionaries and test fixtures for (see app/extraction/
label_dictionaries/ and tests/). Every other entry in LANGUAGE_CONFIG is
genuinely present and usable for OCR language selection/script
detection — Tesseract's own traineddata for e.g. Telugu or Bengali works
exactly the same way as Kannada's does — but is disclosed as
"configured, not yet verified against a real document" until a later
phase actually exercises it end-to-end. This mirrors the project's
existing convention for e.g. the SAP/Fabric/Salesforce connectors: real,
working code, honestly labeled as unverified where nothing has proven it
live yet.

SCRIPT_UNICODE_RANGES backs language_detection/script_block_detector.py.
Ranges are the standard Unicode block boundaries (inclusive), not
hand-tuned. A script that maps to more than one language in this table
(DEVANAGARI -> hi/mr; BENGALI -> bn/as) is a genuine ambiguity a
character-range count alone cannot resolve — see SCRIPT_LANGUAGE_DEFAULT
and hybrid_detector.py's own docstring for how the FastText layer earns
its keep specifically for that case.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageInfo:
    code: str  # ISO 639-1, e.g. "kn"
    name: str  # display name, e.g. "Kannada"
    tesseract_code: str  # traineddata basename, e.g. "kan"
    script: str  # key into SCRIPT_UNICODE_RANGES
    fasttext_label: str  # FastText lid.176 model's own label, e.g. "__label__kn"


LANGUAGE_CONFIG: dict[str, LanguageInfo] = {
    "en": LanguageInfo("en", "English", "eng", "LATIN", "__label__en"),
    "hi": LanguageInfo("hi", "Hindi", "hin", "DEVANAGARI", "__label__hi"),
    "kn": LanguageInfo("kn", "Kannada", "kan", "KANNADA", "__label__kn"),
    "ta": LanguageInfo("ta", "Tamil", "tam", "TAMIL", "__label__ta"),
    "te": LanguageInfo("te", "Telugu", "tel", "TELUGU", "__label__te"),
    "ml": LanguageInfo("ml", "Malayalam", "mal", "MALAYALAM", "__label__ml"),
    "mr": LanguageInfo("mr", "Marathi", "mar", "DEVANAGARI", "__label__mr"),
    "bn": LanguageInfo("bn", "Bengali", "ben", "BENGALI", "__label__bn"),
    "gu": LanguageInfo("gu", "Gujarati", "guj", "GUJARATI", "__label__gu"),
    "pa": LanguageInfo("pa", "Punjabi", "pan", "GURMUKHI", "__label__pa"),
    "or": LanguageInfo("or", "Odia", "ori", "ORIYA", "__label__or"),
    "as": LanguageInfo("as", "Assamese", "asm", "BENGALI", "__label__as"),
    "ur": LanguageInfo("ur", "Urdu", "urd", "ARABIC", "__label__ur"),
}

# The four languages this build ships real Custom Engine label
# dictionaries and real test fixtures for (approved decision, Sep 2026).
# Every other LANGUAGE_CONFIG entry is real and usable for OCR/script
# detection but has no curated label dictionary yet — Custom Engine
# extraction for those falls back to English-only labels + the
# script-agnostic regex fallbacks (see extraction/custom_engine.py).
PRIORITY_LANGUAGES: list[str] = ["en", "hi", "kn", "ta"]

# Inclusive Unicode code-point ranges per script family. Multiple ranges
# for one script (e.g. Urdu/ARABIC) cover the base block plus its
# Supplement block. LATIN intentionally covers only bare a-z/A-Z (no
# Latin-1 Supplement accented letters) — good enough for "is this token
# plain English/ASCII text," which is all script-block detection needs
# it for; full Latin-script coverage isn't this layer's job.
SCRIPT_UNICODE_RANGES: dict[str, list[tuple[int, int]]] = {
    "LATIN": [(0x0041, 0x005A), (0x0061, 0x007A)],
    "DEVANAGARI": [(0x0900, 0x097F)],
    "KANNADA": [(0x0C80, 0x0CFF)],
    "TAMIL": [(0x0B80, 0x0BFF)],
    "TELUGU": [(0x0C00, 0x0C7F)],
    "MALAYALAM": [(0x0D00, 0x0D7F)],
    "BENGALI": [(0x0980, 0x09FF)],
    "GUJARATI": [(0x0A80, 0x0AFF)],
    "GURMUKHI": [(0x0A00, 0x0A7F)],
    "ORIYA": [(0x0B00, 0x0B7F)],
    "ARABIC": [(0x0600, 0x06FF), (0x0750, 0x077F)],
}

# Script -> language code(s) it can indicate. Most scripts map to exactly
# one language in LANGUAGE_CONFIG; DEVANAGARI (hi/mr) and BENGALI (bn/as)
# are genuinely ambiguous at the script-block layer alone — real language
# families share a script, this isn't a modeling gap. SCRIPT_LANGUAGE_DEFAULT
# is script_block_detector.py's tentative pick when only one layer runs;
# hybrid_detector.py escalates to FastText specifically to disambiguate
# these before committing to a final answer.
SCRIPT_TO_LANGUAGES: dict[str, list[str]] = {}
for _code, _info in LANGUAGE_CONFIG.items():
    SCRIPT_TO_LANGUAGES.setdefault(_info.script, []).append(_code)
del _code, _info

SCRIPT_LANGUAGE_DEFAULT: dict[str, str] = {
    "LATIN": "en",
    "DEVANAGARI": "hi",
    "KANNADA": "kn",
    "TAMIL": "ta",
    "TELUGU": "te",
    "MALAYALAM": "ml",
    "BENGALI": "bn",
    "GUJARATI": "gu",
    "GURMUKHI": "pa",
    "ORIYA": "or",
    "ARABIC": "ur",
}


def get_language_info(code: str) -> LanguageInfo | None:
    return LANGUAGE_CONFIG.get(code)


def tesseract_code_for(code: str) -> str | None:
    info = LANGUAGE_CONFIG.get(code)
    return info.tesseract_code if info else None


def is_script_ambiguous(script: str) -> bool:
    """True for a script family more than one supported language shares
    (DEVANAGARI: hi/mr; BENGALI: bn/as) — the exact case script-block
    detection alone cannot resolve, per this module's own docstring."""
    return len(SCRIPT_TO_LANGUAGES.get(script, [])) > 1


def all_language_codes() -> list[str]:
    return list(LANGUAGE_CONFIG.keys())
