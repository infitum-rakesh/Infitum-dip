"""
sql_export_writers — real, chunked file writers for SQL Workspace
background exports (execution/sql_export_executor.py). Each writer takes
rows as they arrive from connector.open_sql_stream()'s bounded fetchmany()
chunks and writes them straight to disk — never buffers the whole result
set in Python memory, the same "bounded memory" discipline Phase 3's
pipeline extraction and loading/staging already follow.

Row values come back from SQLAlchemy as native Python objects (Decimal,
datetime, date, bytes, ...) that json.dumps() doesn't handle by default —
_json_default() below converts them to a JSON-safe representation rather
than the writer crashing partway through a large export.
"""
from __future__ import annotations

import abc
import csv
import datetime
import decimal
import json
import re
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from openpyxl import Workbook
import pyarrow as pa
import pyarrow.parquet as pq


def _json_default(value):
    if isinstance(value, (decimal.Decimal,)):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _json_safe(value):
    """XLSX cells (openpyxl) reject the same non-primitive types json.dumps
    chokes on — Decimal/bytes in particular. Reuse one conversion for both
    writers rather than two slightly-different rules."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _json_default(value)


class ChunkWriter(abc.ABC):
    """One open file, written incrementally as chunks arrive. header_written
    tracks whether the first chunk (which carries the real column list) has
    been seen yet — the column list itself is only known once the query
    actually starts returning rows, not guessed up front."""

    def __init__(self, path: Path):
        self.path = path
        self._header_written = False

    @abc.abstractmethod
    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class CsvChunkWriter(ChunkWriter):
    def __init__(self, path: Path, delimiter: str = ",", quotechar: str = '"', encoding: str = "utf-8"):
        super().__init__(path)
        self._fh = open(path, "w", newline="", encoding=encoding)
        self._writer = csv.writer(self._fh, delimiter=delimiter, quotechar=quotechar)

    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        if not self._header_written:
            self._writer.writerow(columns)
            self._header_written = True
        for row in rows:
            self._writer.writerow([_json_default(v) if not isinstance(v, (str, int, float, bool)) and v is not None else v for v in row])

    def close(self) -> None:
        self._fh.close()


class JsonChunkWriter(ChunkWriter):
    """Streams a real JSON array — '[' at open, one object per row
    (comma-separated) as chunks arrive, ']' at close. A reader never sees
    a truncated-but-valid-looking file unless close() actually ran."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._fh = open(path, "w", encoding="utf-8")
        self._fh.write("[")
        self._first_row = True

    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        self._header_written = True
        for row in rows:
            if not self._first_row:
                self._fh.write(",")
            self._first_row = False
            self._fh.write(json.dumps(dict(zip(columns, row)), default=_json_default))

    def close(self) -> None:
        self._fh.write("]")
        self._fh.close()


class XlsxChunkWriter(ChunkWriter):
    """openpyxl's write_only Workbook — each ws.append() is compressed to
    disk-backed temp storage as it's called rather than held as Python
    objects, so memory stays bounded across a large export the same way
    CSV/JSON do. The one real difference: XLSX's zip container structure
    means the final save() is a single flush at close() — there is no
    row-by-row disk-visible file the way CSV/JSON have mid-export; a
    disclosed format limitation, not something this code pretends away."""

    def __init__(self, path: Path, sheet_name: str = "Result"):
        super().__init__(path)
        self._wb = Workbook(write_only=True)
        # Excel sheet names can't exceed 31 chars or contain []:*?/\ —
        # openpyxl raises on an invalid name rather than silently
        # truncating/sanitizing it, so do that ourselves up front instead
        # of letting a job fail on an otherwise-successful export.
        safe_name = re.sub(r"[\[\]:*?/\\]", "_", sheet_name or "Result")[:31] or "Result"
        self._ws = self._wb.create_sheet(safe_name)

    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        if not self._header_written:
            self._ws.append(columns)
            self._header_written = True
        for row in rows:
            self._ws.append([_json_safe(v) for v in row])

    def close(self) -> None:
        self._wb.save(self.path)


class ParquetChunkWriter(ChunkWriter):
    """Real, genuinely streaming Parquet via pyarrow's incremental
    ParquetWriter — each chunk becomes one real row group written straight
    to disk, never the whole result buffered in Arrow memory at once. Cell
    values go through the same _json_safe() coercion the XLSX writer uses
    (Decimal -> float, datetime/date -> ISO string, bytes -> hex) so the
    column types pyarrow infers stay simple and stable across chunks —
    the real, disclosed tradeoff: this is a genuinely working, streamable
    Parquet export, but it exports Decimal/datetime columns as float64/
    string rather than Parquet's native decimal128/timestamp logical
    types, not a full type-fidelity round trip.

    Known, disclosed edge case (same "honest, not silently broken" spirit
    as XlsxChunkWriter's own documented limitation above): pyarrow infers
    each chunk's schema from its own values, so a column that happens to
    be entirely NULL in the very first chunk but holds real values in a
    later chunk produces a genuine schema-mismatch error rather than
    silently coercing or dropping data — a real limitation of streaming
    Parquet writes without a pre-declared schema, not something worth
    guessing around."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._writer: pq.ParquetWriter | None = None

    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        self._header_written = True
        if not rows:
            return
        col_data = {c: [row[i] if i < len(row) else None for row in rows] for i, c in enumerate(columns)}
        col_data = {c: [_json_safe(v) for v in values] for c, values in col_data.items()}
        table = pa.table(col_data)
        if self._writer is None:
            self._writer = pq.ParquetWriter(str(self.path), table.schema)
        self._writer.write_table(table)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        else:
            # No rows were ever written (empty result set) — still produce
            # a real, valid, openable (if columnless) Parquet file rather
            # than leaving no file at all or a corrupt zero-byte one.
            pq.write_table(pa.table({}), str(self.path))


_INVALID_XML_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.\-]")


def _xml_tag(name: str) -> str:
    tag = _INVALID_XML_NAME_CHARS.sub("_", str(name)) or "col"
    if tag[0].isdigit() or tag[0] in ".-":
        tag = f"_{tag}"
    return tag


class XmlChunkWriter(ChunkWriter):
    """Real streaming XML writer (Data Format Transformation Engine,
    Phase 4) — one <row> element per row, written straight to disk as
    chunks arrive (root element opened at init, closed at close()), the
    same "never buffer the whole export in memory" discipline every
    writer in this module follows. Column names are sanitized into valid
    XML element names (spaces/invalid characters replaced, a leading
    digit/dot/hyphen prefixed with "_") since arbitrary column names
    aren't always valid XML tags; when sanitization actually changed the
    name, the ORIGINAL is preserved as a `name` attribute on that <col>
    element so no information is lost — a real, disclosed consequence:
    reading this file back (connectors/files/xml_format.py) recovers the
    sanitized tag as the column name, not the original, since that reader
    doesn't special-case this attribute."""

    def __init__(self, path: Path, root_element: str = "rows", row_element: str = "row"):
        super().__init__(path)
        self._row_tag = _xml_tag(row_element or "row")
        self._root_tag = _xml_tag(root_element or "rows")
        self._fh = open(path, "w", encoding="utf-8")
        self._fh.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<{self._root_tag}>\n')
        self._tags: list[str] | None = None

    def write_rows(self, columns: list[str], rows: list[list]) -> None:
        if self._tags is None:
            self._tags = [_xml_tag(c) for c in columns]
            self._header_written = True
        for row in rows:
            self._fh.write(f"  <{self._row_tag}>")
            for col, tag, value in zip(columns, self._tags, row):
                v = value if value is None or isinstance(value, (str, int, float, bool)) else _json_default(value)
                text = "" if v is None else xml_escape(str(v))
                if tag == col:
                    self._fh.write(f"<{tag}>{text}</{tag}>")
                else:
                    safe_name = xml_escape(col, {'"': "&quot;"})
                    self._fh.write(f'<{tag} name="{safe_name}">{text}</{tag}>')
            self._fh.write(f"</{self._row_tag}>\n")

    def close(self) -> None:
        self._fh.write(f"</{self._root_tag}>")
        self._fh.close()


_WRITERS = {"CSV": CsvChunkWriter, "JSON": JsonChunkWriter, "XLSX": XlsxChunkWriter, "PARQUET": ParquetChunkWriter, "XML": XmlChunkWriter}
_EXTENSIONS = {"CSV": "csv", "JSON": "json", "XLSX": "xlsx", "PARQUET": "parquet", "XML": "xml"}


def make_writer(fmt: str, path: Path, options: dict | None = None) -> ChunkWriter:
    """options (all optional, format-specific — unrecognized/inapplicable
    keys for the chosen fmt are simply ignored rather than raising, so
    callers can pass one shared options dict regardless of target format):
      CSV: delimiter, quotechar, encoding
      XLSX: sheet_name
      XML: root_element, row_element
    """
    cls = _WRITERS.get(fmt)
    if cls is None:
        raise ValueError(f"Unknown export format: {fmt}")
    options = options or {}
    if cls is CsvChunkWriter:
        kwargs = {k: v for k, v in options.items() if k in ("delimiter", "quotechar", "encoding") and v}
        return cls(path, **kwargs)
    if cls is XlsxChunkWriter:
        kwargs = {k: v for k, v in options.items() if k in ("sheet_name",) and v}
        return cls(path, **kwargs)
    if cls is XmlChunkWriter:
        kwargs = {k: v for k, v in options.items() if k in ("root_element", "row_element") and v}
        return cls(path, **kwargs)
    return cls(path)


def extension_for(fmt: str) -> str:
    return _EXTENSIONS.get(fmt, "dat")
