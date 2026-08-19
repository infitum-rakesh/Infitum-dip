"""
delimited — streaming reader for CSV/TSV/TXT/DAT (any single-character-
delimited text format with a header row). Bounded memory: reads the
stream in 64KB blocks via _streaming.iter_text_lines and hands Python's
own csv.reader one line at a time — never the whole file.
"""
from __future__ import annotations

import csv
from typing import Iterator

from app.connectors.files._streaming import iter_text_lines


def peek_header(stream, delimiter: str = ",") -> list[str]:
    lines = iter_text_lines(stream)
    first_line = next(lines, None)
    if first_line is None:
        return []
    return next(csv.reader([first_line], delimiter=delimiter), [])


def iter_rows(stream, delimiter: str = ",") -> Iterator[dict]:
    lines = iter_text_lines(stream)
    header_line = next(lines, None)
    if header_line is None:
        return
    header = next(csv.reader([header_line], delimiter=delimiter), [])
    for row in csv.reader(lines, delimiter=delimiter):
        if not row:
            continue
        yield dict(zip(header, row))
