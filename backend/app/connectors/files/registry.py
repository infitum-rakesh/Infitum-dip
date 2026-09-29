"""
Format registry — dispatches a filename to the right streaming reader by
extension. Only formats with a real implementation are registered;
anything else raises ConnectorUnsupportedError with the current
supported list, rather than being silently skipped or misinterpreted.

Data Format Transformation Engine (Phase 4): XML/Parquet/array-form JSON
readers were added here specifically so this registry becomes the ONE
place every file-reading consumer (this file connector, UploadedDatasetFile
via services/uploaded_file_service.py, and reconciliation/anomaly/
validation's FileRowSource) gains a new format — see xml_format.py's,
json_array.py's and parquet_format.py's own docstrings for each one's
real, disclosed characteristics (XML/Parquet need random access like
XLSX already did; JSON-array is real but not forward-streaming, same
disclosed shape as XLSX). Avro remains genuinely unsupported (fastavro
isn't wired up in this build) — see UNSUPPORTED_FORMATS below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterator

from app.connectors.base import ConnectorUnsupportedError
from app.connectors.files import (
    delimited, json_array, jsonl as jsonl_reader, parquet_format, xlsx as xlsx_reader, xml_format,
)


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
    ".json": FormatReader(json_array.peek_header, json_array.iter_rows),
    ".xml": FormatReader(xml_format.peek_header, xml_format.iter_rows),
    ".parquet": FormatReader(parquet_format.peek_header, parquet_format.iter_rows),
}

UNSUPPORTED_FORMATS: dict[str, str] = {
    ".avro": "Avro needs fastavro, which isn't wired up in this build yet.",
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
