"""
evidence_service.py — real, on-document field-to-bounding-box linking for
the Documents module's extracted fields (used by Invoice Intelligence's
evidence viewer, INV15, and any other consumer of a field's "where on the
page did this value come from" — genuinely shared infrastructure, not
invoice-specific, per this build's own "reuse existing infrastructure"
instruction).

Two real sources, never a third fabricated one:

1. Custom Extraction Engine (app/extraction/custom_engine.py) — a value
   found by label-matching already knows exactly which OCR row(s) it came
   from; that row's regions carry real bboxes (from ocr/engine.py's
   Tesseract/cloud-provider output). custom_engine.py now threads that
   bbox straight through in each scalar field's own "bbox" key — this
   module is not involved in that path at all, only in wiring it through
   document_service.py's _reshape_extraction_fields() unchanged.

2. Real AI extraction (extraction_service.py) — the model returns a
   value and a confidence, but no bounding box (no vision-grounded LLM
   call is made here, just a text prompt over already-OCR'd text — see
   extraction_service.py's EXTRACT_SYSTEM prompt). find_region_bbox()
   below recovers a REAL bbox for that value via heuristic text matching
   against the document's own real OCR regions: the box itself is always
   a genuine OCR region's bbox, never invented geometry — only the
   MATCH between "the model said this value" and "which region contains
   it" is heuristic, and a field this can't confidently match to a
   region is left with bbox=None rather than guessing.
"""
from __future__ import annotations

import re

MIN_MATCH_LEN = 3  # values shorter than this match too many unrelated regions to be a trustworthy anchor


def merge_bboxes(bboxes: list[list[float]]) -> list[float] | None:
    """Union bounding box (x, y, width, height) covering every box in
    `bboxes` — used to collapse a matched OCR row's several regions (e.g.
    a "Label:" region plus a separate "Value" region on the same visual
    line — see custom_engine.py's _build_rows()) into the one rectangle a
    document viewer should highlight. None for an empty input."""
    if not bboxes:
        return None
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[0] + b[2] for b in bboxes)
    y1 = max(b[1] + b[3] for b in bboxes)
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def find_region_bbox(value, regions: list[dict]) -> list[float] | None:
    """Heuristic evidence lookup for the real AI-extraction path: searches
    `regions` (Document.ocr_result["regions"], each a real
    {"text", "bbox", "page", "confidence"} from ocr/engine.py) for one
    whose OWN text contains `value` (case/whitespace-insensitive), and
    returns that region's real bbox. Tries an exact-substring match
    first (most reliable — the value appears verbatim in one OCR line),
    then falls back to a merged bbox across a short run of ADJACENT
    regions on the same page whose concatenated text contains it (covers
    a value Tesseract split across two nearby line regions). Returns
    None — never a guessed/first-region/synthetic box — when nothing
    genuinely matches, which is the common, honest outcome for a value
    the model normalized/reformatted (e.g. "1,234.56" vs OCR's "1234.56")
    beyond what a text match can still recognize."""
    needle = _normalize(str(value)) if value not in (None, "") else ""
    if len(needle) < MIN_MATCH_LEN or not regions:
        return None

    # Pass 1: single region containing the value verbatim.
    for r in regions:
        if needle in _normalize(r.get("text", "")):
            return r.get("bbox")

    # Pass 2: value split across up to 3 consecutive same-page regions in
    # reading order — a real, observed OCR line-splitting pattern (see
    # custom_engine.py's module docstring), checked here too since the
    # AI-extraction path has no row-clustering step of its own.
    by_page: dict[int, list[dict]] = {}
    for r in regions:
        by_page.setdefault(r.get("page", 1), []).append(r)
    for page_regions in by_page.values():
        for window in (2, 3):
            for i in range(len(page_regions) - window + 1):
                span = page_regions[i:i + window]
                combined = _normalize(" ".join(r.get("text", "") for r in span))
                if needle in combined:
                    return merge_bboxes([r["bbox"] for r in span if r.get("bbox")])
    return None
