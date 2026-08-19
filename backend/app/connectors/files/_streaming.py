"""
_streaming — a byte-stream-to-text-lines decoder that only requires the
underlying object to support .read(size) -> bytes.

Deliberately NOT built on io.TextIOWrapper: TextIOWrapper requires its
underlying stream to satisfy Python's buffered-IO protocol fairly
strictly, and paramiko's SFTPFile doesn't reliably guarantee that across
versions. Every format reader in this package (delimited.py, jsonl.py)
needs is a bounded-memory line iterator that works identically whether
the stream came from paramiko or a plain local `open(..., "rb")` — this
is that, implemented once.
"""
from __future__ import annotations

from typing import Iterator


def iter_text_lines(stream, chunk_bytes: int = 65536, encoding: str = "utf-8") -> Iterator[str]:
    buffer = b""
    while True:
        chunk = stream.read(chunk_bytes)
        if not chunk:
            break
        buffer += chunk
        lines = buffer.split(b"\n")
        buffer = lines.pop()  # last, possibly-partial line carried into the next read
        for line in lines:
            yield line.decode(encoding, errors="replace").rstrip("\r")
    if buffer:
        yield buffer.decode(encoding, errors="replace").rstrip("\r")
