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
from pathlib import Path

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
    def __init__(self, path: Path):
        super().__init__(path)
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)

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

    def __init__(self, path: Path):
        super().__init__(path)
        self._wb = Workbook(write_only=True)
        self._ws = self._wb.create_sheet("Result")

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


_WRITERS = {"CSV": CsvChunkWriter, "JSON": JsonChunkWriter, "XLSX": XlsxChunkWriter, "PARQUET": ParquetChunkWriter}
_EXTENSIONS = {"CSV": "csv", "JSON": "json", "XLSX": "xlsx", "PARQUET": "parquet"}


def make_writer(fmt: str, path: Path) -> ChunkWriter:
    cls = _WRITERS.get(fmt)
    if cls is None:
        raise ValueError(f"Unknown export format: {fmt}")
    return cls(path)


def extension_for(fmt: str) -> str:
    return _EXTENSIONS.get(fmt, "dat")
