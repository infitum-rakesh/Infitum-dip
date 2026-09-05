"""
Data-type / format anomaly detection — flags values that don't match a
configured regex pattern (e.g. an email column with a non-email value, a
phone column with the wrong shape, a code column that should always be
exactly N digits). The pattern is per-column config
(AnomalyJob.detection_config[column]["format_pattern"]) — this module
has no built-in notion of "what an email looks like"; it only evaluates
whatever pattern the job was configured with. A column with no configured
pattern is simply never checked by this detector (not an error).
"""
from __future__ import annotations

import re

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, resolve_identifier

DEFAULT_PARAMS = {"FORMAT": {"pattern": None, "max_hits": 200}}


def detect_format(
    row_source, column: str, reference_stats: dict, *,
    id_column: str | None = None, pattern: str | None = None, max_hits: int = 200,
    cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    if not pattern:
        return []  # no pattern configured for this column — nothing to check
    try:
        compiled = re.compile(pattern)
    except re.error:
        return []  # a malformed pattern shouldn't crash the whole run — the job editor validates this at save time

    fetch_cols = [column] if not id_column or id_column == column else [column, id_column]
    hits: list[AnomalyHit] = []
    position = 0
    for chunk in row_source.iter_rows(fetch_cols, chunk_size=5000):
        for row in chunk:
            position += 1
            v = row.get(column)
            if v is None:
                continue
            s = str(v)
            if not compiled.fullmatch(s):
                ident, is_pos = resolve_identifier(row, id_column, position)
                hits.append(AnomalyHit(
                    anomaly_type="FORMAT", record_identifier=ident, value=s,
                    expected_range=f"matches /{pattern}/", raw_score=1.0,
                    identifier_is_position=is_pos,
                ))
                if len(hits) >= max_hits:
                    return hits
        if position >= cap:
            break
    return hits


DETECTORS = {"FORMAT": detect_format}
