"""
datatype_mapper — classifies a reflected column type string (e.g.
"VARCHAR(200)", "BIGINT", "TIMESTAMP") into a broad family, then scores
source -> target compatibility for the mapping grid's Data Type column
and the Compare Schema view (spec sections 18, 36).

This is intentionally a family-level heuristic, not a full per-dialect
cast-legality check (which would mean encoding every driver's implicit
conversion rules) — it's the same class of judgment call a human reviewing
a mapping makes at a glance: "numeric into text is always safe, text into
numeric might blow up on bad data, numeric into a date makes no sense."
"""
from __future__ import annotations

import re

NUMERIC = "NUMERIC"
STRING = "STRING"
DATE = "DATE"
BOOLEAN = "BOOLEAN"
BINARY = "BINARY"
JSON_ = "JSON"
OTHER = "OTHER"

COMPATIBLE = "COMPATIBLE"
WARNING = "WARNING"
INCOMPATIBLE = "INCOMPATIBLE"

_FAMILY_PATTERNS = [
    (NUMERIC, re.compile(r"INT|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL|MONEY|SERIAL")),
    (DATE, re.compile(r"DATE|TIME")),  # covers DATE, DATETIME, TIMESTAMP, TIME
    (BOOLEAN, re.compile(r"^BOOL")),
    (BINARY, re.compile(r"BLOB|BINARY|BYTEA|VARBINARY|IMAGE")),
    (JSON_, re.compile(r"^JSON")),
    (STRING, re.compile(r"CHAR|TEXT|STRING|CLOB|UUID|ENUM")),
]

# (source_family, target_family) -> compatibility. Anything not listed
# defaults to WARNING (unknown combination, not asserted safe).
_MATRIX = {
    (NUMERIC, STRING): COMPATIBLE,
    (DATE, STRING): COMPATIBLE,
    (BOOLEAN, STRING): COMPATIBLE,
    (JSON_, STRING): COMPATIBLE,
    (BOOLEAN, NUMERIC): COMPATIBLE,
    (STRING, NUMERIC): WARNING,
    (STRING, DATE): WARNING,
    (STRING, BOOLEAN): WARNING,
    (STRING, JSON_): WARNING,
    (NUMERIC, BOOLEAN): WARNING,
    (NUMERIC, DATE): INCOMPATIBLE,
    (DATE, NUMERIC): INCOMPATIBLE,
    (NUMERIC, JSON_): INCOMPATIBLE,
    (DATE, JSON_): INCOMPATIBLE,
    (BINARY, STRING): WARNING,
    (STRING, BINARY): WARNING,
}


def classify(data_type: str) -> str:
    upper = (data_type or "").upper()
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(upper):
            return family
    return OTHER


def compatibility(source_type: str, target_type: str) -> dict:
    """Returns {"status": ..., "source_family": ..., "target_family": ..., "reason": ...}."""
    src_family = classify(source_type)
    tgt_family = classify(target_type)

    if src_family == tgt_family and src_family != OTHER:
        status, reason = COMPATIBLE, f"Both are {src_family.lower()} types."
    elif src_family == OTHER or tgt_family == OTHER:
        status, reason = WARNING, "Could not classify one or both types — verify manually."
    else:
        status = _MATRIX.get((src_family, tgt_family), WARNING)
        reason = {
            COMPATIBLE: f"{src_family.title()} widens safely into {tgt_family.lower()}.",
            WARNING: f"{src_family.title()} into {tgt_family.lower()} can fail on data that doesn't convert cleanly.",
            INCOMPATIBLE: f"{src_family.title()} and {tgt_family.lower()} have no sensible implicit conversion.",
        }[status]

    return {"status": status, "source_family": src_family, "target_family": tgt_family, "reason": reason}
