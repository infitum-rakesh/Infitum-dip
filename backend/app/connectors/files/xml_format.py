"""
xml_format — real streaming reader for generic XML files via
ElementTree.iterparse, genuinely bounded memory: each record element is
cleared (and its already-processed siblings pruned from the root) the
moment it's been yielded, never materializing the whole document tree at
once — the same discipline delimited.py/jsonl.py already follow, applied
to iterparse's own documented low-memory recipe.

Record boundary convention (needed because generic XML has no built-in
notion of "one row" the way a CSV line or JSONL line does): the root
element's direct children are the records, e.g.
<root><record>...</record><record>...</record></root>. Each record's own
direct child elements become {tag: text} entries; the record element's
own attributes are included as {"@attr": value} entries. A grandchild
element (deeper nesting) is flattened to its own direct text content —
matching the flat-row assumption every reader in this package already
makes (CSV/JSONL/XLSX are all flat too); genuinely hierarchical XML is a
real, disclosed limitation, not something this silently mangles.
"""
from __future__ import annotations

from typing import Iterator
from xml.etree import ElementTree as ET


def _flatten_record(elem) -> dict:
    row = {f"@{k}": v for k, v in elem.attrib.items()}
    for child in elem:
        row[child.tag] = (child.text or "").strip()
    if not row:
        row["value"] = (elem.text or "").strip()
    return row


def _iter_record_elements(stream):
    depth = 0
    root = None
    for event, elem in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1:
                root = elem
            continue
        depth -= 1
        if depth == 1:
            yield elem
            elem.clear()
            if root is not None:
                root.clear()


def peek_header(stream) -> list[str]:
    for elem in _iter_record_elements(stream):
        return list(_flatten_record(elem).keys())
    return []


def iter_rows(stream) -> Iterator[dict]:
    for elem in _iter_record_elements(stream):
        yield _flatten_record(elem)
