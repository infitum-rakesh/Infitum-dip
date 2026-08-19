"""
Format registry — dispatches a filename to the right streaming reader by
extension. Only formats with a real streaming implementation are
registered; anything else raises ConnectorUnsupportedError with the
current supported list, rather than being silently skipped or
misinterpreted. XML/Parquet/Avro/array-form JSON are NOT implemented —
see UNSUPPORTED_FORMATS below for exactly why each one — matching the
same honest-gap pattern as connectors/registry.py's UNVERIFIED database
types.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterator

from app.connectors.base import ConnectorUnsupportedError
from app.connectors.files import delimited, jsonl as jsonl_reader, xlsx as xlsx_reader


@dataclass
class FormatReader:
    peek_header: Callable[[object], list[str]]
    iter_rows: Callable[[object], Iterator[dict]]


_REGISTRY: dict[str, FormatReader] = {
    ".csv": FormatReader(lambda s: delimited.peek_header(s, ","), lambda s: delimited.iter_rows(s, ",")),
    ".tsv": FormatReader(lambda s: delimited.peek_header(s, "\t"), lambda s: delimited.iter_rows(s, "\t")),
    ".txt": FormatReader(lambda s: delimited.peek_header(s, ","), lambda s: delimited.iter_rows(s, ",")),
    ".dat": FormatReader(lambda s: delimited.peek_header(s, ","), lambda s: delimited.iter_rows(s, ",")),
    ".jsonl": FormatReader(jsonl_reader.peek_header, jsonl_reader.iter_rows),
    ".ndjson": FormatReader(jsonl_reader.peek_header, jsonl_reader.iter_rows),
    ".xlsx": FormatReader(xlsx_reader.peek_header, xlsx_reader.iter_rows),
}

UNSUPPORTED_FORMATS: dict[str, str] = {
    ".xml": "XML streaming needs a schema-aware record-boundary reader that isn't built yet.",
    ".parquet": "Parquet needs pyarrow, which isn't wired up in this build yet.",
    ".avro": "Avro needs fastavro, which isn't wired up in this build yet.",
    ".json": "A single large JSON array can't be streamed without a dedicated incremental parser "
             "(ijson) — use JSONL (newline-delimited JSON) for streaming-safe extraction instead.",
}


def supported_extensions() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_reader(filename: str) -> FormatReader:
    ext = os.path.splitext(filename)[1].lower()
    if ext in UNSUPPORTED_FORMATS:
        raise ConnectorUnsupportedError(f"{ext} files are not supported yet — {UNSUPPORTED_FORMATS[ext]}")
    reader = _REGISTRY.get(ext)
    if reader is None:
        raise ConnectorUnsupportedError(f"Unrecognized file extension '{ext}' — supported: {', '.join(supported_extensions())}.")
    return reader
