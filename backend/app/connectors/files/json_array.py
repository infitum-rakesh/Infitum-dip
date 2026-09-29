"""
json_array — reader for a plain top-level JSON array of objects
(".json"). Unlike every other reader in this package, this one is NOT
forward-streaming: it decodes the whole file into memory before yielding
rows, because a top-level JSON array can't be split into records without
either an incremental parser (ijson, not a dependency of this build) or
hand-rolled bracket/quote scanning (fragile on escaped characters). This
mirrors xlsx.py's own disclosed non-forward-streaming limitation (XLSX's
zip container needs random access too) — a real, working implementation
with a known, stated memory characteristic, appropriate for JSON files of
realistic size. A genuinely large export should use JSONL (newline-
delimited JSON) instead, which this package already streams properly —
see jsonl.py.
"""
from __future__ import annotations

from typing import Iterator
import json


def _load_records(stream) -> list:
    data = json.loads(stream.read())
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return [{"value": data}]


def peek_header(stream) -> list[str]:
    records = _load_records(stream)
    if not records:
        return []
    first = records[0]
    return list(first.keys()) if isinstance(first, dict) else ["value"]


def iter_rows(stream) -> Iterator[dict]:
    for record in _load_records(stream):
        yield record if isinstance(record, dict) else {"value": record}
