"""
parquet_format — real, genuinely batch-streaming reader for Parquet
files via pyarrow (already a dependency of this build — see
services/sql_export_writers.py's ParquetChunkWriter, which writes what
this reads). Parquet keeps its metadata footer at the END of the file,
so — like XLSX's zip container — this needs random access (seek/tell),
not a pure forward stream; paramiko's SFTPFile supports both, matching
xlsx.py's own comment on the same constraint.
"""
from __future__ import annotations

from typing import Iterator

import pyarrow.parquet as pq

_BATCH_SIZE = 5000


def peek_header(stream) -> list[str]:
    return list(pq.ParquetFile(stream).schema_arrow.names)


def iter_rows(stream) -> Iterator[dict]:
    pf = pq.ParquetFile(stream)
    for batch in pf.iter_batches(batch_size=_BATCH_SIZE):
        for record in batch.to_pylist():
            yield record
