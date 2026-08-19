"""
jsonl — streaming reader for newline-delimited JSON (.jsonl/.ndjson).
Each line is one complete JSON object — bounded memory by construction,
no buffering of the whole document needed (unlike a single large JSON
array, which is NOT implemented here — see connectors/files/registry.py
UNSUPPORTED_FORMATS for why plain .json is out of scope for streaming).
"""
from __future__ import annotations

import json
from typing import Iterator

from app.connectors.files._streaming import iter_text_lines


def peek_header(stream) -> list[str]:
    for line in iter_text_lines(stream):
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        return list(obj.keys()) if isinstance(obj, dict) else ["value"]
    return []


def iter_rows(stream) -> Iterator[dict]:
    for line in iter_text_lines(stream):
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        yield obj if isinstance(obj, dict) else {"value": obj}
