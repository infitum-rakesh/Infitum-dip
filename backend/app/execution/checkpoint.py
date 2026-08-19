"""
checkpoint — key-value (de)serialization for PipelineRunPartition's
persisted checkpoint fields (range_start/range_end/last_key_read, all
stored as String columns so one schema fits int/float/datetime/str keys).

The actual checkpoint WRITE (advancing last_key_read + records_read after
a chunk's staging write succeeds) lives in worker.py, right next to the
write it's guarding — see that module's docstring for why order matters
("never update the watermark before the write that made it true has
actually landed").
"""
from __future__ import annotations

from datetime import date, datetime


def infer_key_type(value) -> str:
    """Determines partition arithmetic type from a real value returned by
    key_range() — never from a parsed type-name string, which can't
    distinguish e.g. a numeric-looking VARCHAR from a real integer."""
    if isinstance(value, bool):
        return "str"  # bool is an int subclass in Python — never partition-split it as a number
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (datetime, date)):
        return "datetime"
    return "str"


def encode_key(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def decode_key(value_str: str | None, key_type: str | None):
    if value_str is None:
        return None
    if key_type == "int":
        return int(value_str)
    if key_type == "float":
        return float(value_str)
    if key_type == "datetime":
        return datetime.fromisoformat(value_str)
    return value_str
