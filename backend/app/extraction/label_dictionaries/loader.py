"""
label_dictionaries/loader.py — Multilingual Document Processing, Phase 3.

Loads a language's field-label / table-column-label synonym set from its
JSON file in this same directory. The whole point of externalizing these
(approved decision, Sep 2026: "Keep the dictionaries external/configurable
and do not hard-code language-specific business logic into the core
extraction engine") is that adding a new language's dictionary is
authoring a new JSON file here, never touching custom_engine.py's actual
matching logic.

Never crashes extraction over a missing or malformed dictionary file — a
language with no *.json here (any of the LANGUAGE_CONFIG entries beyond
the four PRIORITY_LANGUAGES shipped this phase) simply contributes no
extra label candidates; English + the field's own humanized name (always
included regardless — see custom_engine.py's _label_candidates()) still
work exactly as they did before this phase existed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_DICT_DIR = Path(__file__).parent


@dataclass(frozen=True)
class LabelDictionary:
    language: str
    field_synonyms: dict[str, list[str]] = field(default_factory=dict)
    table_column_synonyms: dict[str, list[str]] = field(default_factory=dict)


@lru_cache(maxsize=None)
def get_label_dictionary(language_code: str) -> LabelDictionary | None:
    path = _DICT_DIR / f"{language_code}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return LabelDictionary(
        language=language_code,
        field_synonyms=data.get("field_synonyms", {}),
        table_column_synonyms=data.get("table_column_synonyms", {}),
    )


def available_languages() -> list[str]:
    """Which languages actually have a real dictionary file on disk right
    now — used by tests/health-checks, never assumed."""
    return sorted(p.stem for p in _DICT_DIR.glob("*.json"))
