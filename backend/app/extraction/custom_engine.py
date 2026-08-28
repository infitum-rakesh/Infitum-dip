"""
custom_engine.py — the Custom Extraction Engine.

This is a REAL, deterministic, rule-based field extractor — layout
geometry + label/synonym matching + regex + type-aware heuristics. It is
NOT a language model of any kind: there is no model, no weights, no
inference, no external API call anywhere in this module. It is named
"Custom" (never "AI" or "LLM") everywhere it's surfaced, per this
project's standing rule to never mislabel what a feature actually is.

Why this exists: extraction_service.py's real AI extraction requires an
AI Provider connection (OpenAI/Anthropic/Ollama). An organization with
none configured previously had AI Extraction simply skip/fail — see
document_service.py's _run_ai_extraction()/run_manual_ai_extraction()
before this module existed. This engine gives that org a genuine,
zero-configuration, offline extraction path instead: no setup, no API
key, no network call, just real analysis of the document's own OCR
output (app/ocr/engine.py).

Selected via the sentinel connection_id == 0 everywhere a real AI
Provider Connection.id would otherwise be passed — Connection primary
keys are DB autoincrement starting at 1, so 0 can never collide with a
real connection. See extraction_service.py's detect_schema()/
extract_fields() and document_service.py's _run_ai_extraction()/
run_manual_ai_extraction() for the wiring.

HOW IT WORKS
  1. Schema detection reuses document_taxonomies.build_seed_schema() —
     the same comprehensive, hand-built canonical field lists the real
     AI engine seeds itself with for a known document_type. There's no
     ad-hoc "guess a schema from this one document" mode here (that
     needs a real model) — an unspecified document_type defaults to the
     wide-net GENERIC taxonomy.
  2. OCR line regions (doc.ocr_result["regions"], each with a real
     bounding box) are clustered into visual ROWS using vertical bbox
     overlap — this recovers side-by-side "Label    Value" layouts that
     Tesseract split into separate line regions, which the OCR engine's
     own whitespace-collapsed region text (" ".join(words), see
     ocr/engine.py's _extract_page()) would otherwise destroy any way of
     detecting.
  3. Each scalar field is searched for by its (humanized name + curated
     synonym) label appearing in a row's text, taking either the
     remainder of that same row after the label, or — when the label
     fills the whole row — the row immediately below it. A handful of
     type-aware fallbacks (largest currency-looking amount for a total,
     first date-shaped token, email/phone/URL/currency-code regexes)
     cover fields no label-scan finds, using only what's genuinely
     present in the text.
  4. Table fields are located via a header row that hits >= 2 column-
     name synonyms, then walked row-by-row until a stop keyword
     (subtotal/total/tax/terms/...) or a row limit, splitting each row's
     tokens with a "numeric-anchored right-to-left pop": the schema's
     number-typed columns are filled from the row's trailing tokens,
     rightmost first, only while each candidate token genuinely looks
     numeric; everything left over goes to the first column (normally
     "description"). Any other column in between (e.g. an optional
     per-row tax percentage) is left blank rather than guessed — a real,
     acknowledged limitation of not having per-cell column boundaries
     once OCR's line text has already collapsed inter-word gaps.

DEGRADATION: a field this engine can't confidently determine is left
blank (value="", confidence=0) — exactly like the real AI engine's
"field genuinely not present -> null" contract. Nothing here is ever
invented to fill a gap.
"""
from __future__ import annotations

import json
import re

from app.models.document import Document
from app.services import document_taxonomies

MODEL_LABEL = "custom-rule-engine (offline, no AI)"
MAX_TABLE_ROWS = 60  # runaway guard — real invoices/receipts never have more genuine line items than this


class CustomEngineError(Exception):
    """Real failure — currently only raised when there's no OCR text to work from."""


# ── Schema detection ─────────────────────────────────────────────────

def detect_schema(document_type: str | None, extra_field_names: set[str] | None = None) -> list[dict]:
    """No ad-hoc "guess a schema" mode (that needs a real model) — always
    seeds from a canonical taxonomy, defaulting to the wide-net GENERIC
    one when the caller doesn't name a specific document_type."""
    schema = document_taxonomies.build_seed_schema(document_type or "GENERIC", extra_field_names)
    return schema or document_taxonomies.build_seed_schema("GENERIC", extra_field_names)


# ── Label synonyms (curated, real-world label phrasing) ─────────────

FIELD_SYNONYMS: dict[str, list[str]] = {
    "invoice_number": ["invoice number", "invoice no", "invoice #", "inv no", "bill number", "bill no"],
    "invoice_date": ["invoice date", "date of invoice", "bill date", "issue date", "date issued"],
    "due_date": ["due date", "payment due", "due by", "payment due date"],
    "po_number": ["po number", "po no", "purchase order", "p.o. number", "p.o. no"],
    "order_number": ["order number", "order no", "so number", "sales order"],
    "reference_number": ["reference number", "ref no", "reference", "ref #"],
    "document_number": ["document number", "document no", "doc no", "id number"],
    "document_date": ["date", "document date", "dated"],
    "vendor_name": ["vendor", "seller", "billed by", "sold by", "supplier", "from"],
    "vendor_tax_id": ["tax id", "vat no", "vat number", "gstin", "gst no", "gst number", "ein", "tin"],
    "vendor_email": ["email"],
    "vendor_phone": ["phone", "tel", "telephone", "contact no"],
    "vendor_website": ["website", "web"],
    "bill_to_name": ["bill to", "billed to", "customer", "buyer", "invoice to"],
    "ship_to_name": ["ship to", "shipped to", "deliver to", "delivery address"],
    "party_1_name": ["from", "issued by", "seller", "first party"],
    "party_1_id_number": ["id number", "registration number", "tax id", "reg no"],
    "party_1_email": ["email"],
    "party_1_phone": ["phone", "tel", "telephone"],
    "party_2_name": ["to", "recipient", "second party", "buyer"],
    "party_2_id_number": ["id number", "registration number", "tax id", "reg no"],
    "party_2_email": ["email"],
    "party_2_phone": ["phone", "tel", "telephone"],
    "currency": ["currency"],
    "subtotal": ["subtotal", "sub total", "sub-total"],
    "subtotal_amount": ["subtotal", "sub total", "sub-total"],
    "discount_amount": ["discount", "less discount"],
    "tax_rate": ["tax rate", "gst rate", "vat rate"],
    "tax_amount": ["tax", "gst", "vat", "sales tax"],
    "shipping_amount": ["shipping", "freight", "delivery charge", "shipping charge"],
    "total_amount": ["total amount", "grand total", "total due", "amount due", "invoice total", "total"],
    "amount_paid": ["amount paid", "paid"],
    "balance_due": ["balance due", "amount due", "balance"],
    "balance_amount": ["balance due", "amount due", "balance"],
    "payment_terms": ["payment terms", "terms"],
    "payment_method": ["payment method", "paid via", "method of payment"],
    "bank_name": ["bank name", "bank"],
    "bank_account_number": ["account number", "account no", "a/c no", "a/c number"],
    "bank_routing_number": ["routing number", "ifsc", "swift", "routing no", "sort code"],
    "status": ["status"],
    "terms": ["terms", "conditions", "terms and conditions"],
    "signature_name": ["signature", "signed by", "authorized signatory"],
    "notes": ["notes", "remarks", "special instructions", "comments"],
}


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().lower()


def _label_candidates(field: dict) -> list[str]:
    name_lower = field["name"].lower()
    cands = {_humanize(field["name"])} | set(FIELD_SYNONYMS.get(name_lower, []))
    # Longest/most specific phrases first, so e.g. "grand total" is tried
    # before the far more collision-prone bare "total".
    return sorted(cands, key=len, reverse=True)


# Fields where the LAST matching occurrence in reading order is the real
# one (the actual summary-block total/tax/balance line), not the first —
# guards against an earlier, unrelated mention of the same word (e.g. a
# "Tax ID" line above a genuine "Tax" amount line further down).
PREFER_LAST_FIELDS = {
    "total_amount", "subtotal", "subtotal_amount", "tax_amount", "balance_due",
    "balance_amount", "amount_paid", "shipping_amount", "discount_amount",
}


# ── Regex building blocks ────────────────────────────────────────────

DATE_PATTERNS = [
    re.compile(r"\b\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-zA-Z]*\.?\s+\d{1,2},?\s+\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-zA-Z]*\.?,?\s+\d{2,4}\b", re.IGNORECASE),
]
MONEY_TOKEN_RE = re.compile(r"(?<![\w.])(?:[$€£₹]\s?)?\d{1,3}(?:,\d{3})*(?:\.\d{2})?(?![\w])")
NUMERIC_TOKEN_RE = re.compile(r"^[$€£₹]?-?\d[\d,]*(?:\.\d+)?%?$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Excludes a digit run glued to letters/dashes on either side (e.g. the
# "2024-0456" inside "INV-2024-0456") — a real phone number is never
# directly adjacent to alphanumeric text like that.
PHONE_RE = re.compile(r"(?<![\w\-])\+?[\d][\d\-.\s()]{7,16}\d(?![\w\-])")
# Excludes matching into (or partway into) the domain half of an email
# address (e.g. "acmesupplies.com" out of "vendor@acmesupplies.com") —
# the lookbehind blocks the match from starting right after "@" AND from
# starting anywhere else inside that same token (every other position
# there is still preceded by a word character).
URL_RE = re.compile(r"(?<![\w@])(?:https?://)?(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?")
CURRENCY_CODE_RE = re.compile(r"\b(USD|EUR|GBP|INR|AUD|CAD|JPY|CNY|SGD|AED|CHF)\b")
CURRENCY_SYMBOL_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def _first_date_match(text: str) -> str | None:
    best = None
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0))
    return best[1] if best else None


def _clean_number(raw: str) -> float | None:
    cleaned = re.sub(r"[^0-9.\-]", "", raw.replace(",", ""))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _looks_numeric(token: str) -> bool:
    return bool(NUMERIC_TOKEN_RE.fullmatch(token.strip()))


def _clean_value(raw: str, ftype: str):
    raw = raw.strip().strip("|:;,.- ").strip()
    if not raw:
        return None
    if ftype == "number":
        return _clean_number(raw[:60])
    if ftype == "date":
        return _first_date_match(raw)
    if ftype == "boolean":
        low = raw.lower()
        if any(w in low for w in ("yes", "true", "paid", "active", "checked", "approved")):
            return True
        if any(w in low for w in ("no", "false", "unpaid", "pending", "inactive", "unchecked")):
            return False
        return None
    text = raw[:200].strip()
    return text or None


def _all_money_values(text: str) -> list[float]:
    values = []
    for m in MONEY_TOKEN_RE.finditer(text):
        v = _clean_number(m.group(0))
        if v is not None:
            values.append(v)
    return values


# ── Row geometry (recovering visual layout from line-level OCR regions) ──

def _build_rows(regions: list[dict]) -> list[dict]:
    """Clusters OCR line regions into visual rows per page by real bbox
    vertical overlap, in reading order (page, then top). Two regions
    Tesseract split into separate blocks/paragraphs but which sit on the
    same visual line (a common "Label:    Value" two-column layout) end
    up in the same row here, restoring the neighbor relationship that
    the OCR engine's own whitespace-collapsed per-region text otherwise
    has no way to expose."""
    by_page: dict[int, list[dict]] = {}
    for r in regions:
        by_page.setdefault(r.get("page", 1), []).append(r)

    rows: list[dict] = []
    for page in sorted(by_page):
        page_regions = sorted(by_page[page], key=lambda r: (r["bbox"][1], r["bbox"][0]))
        current: list[dict] = []
        cur_top = cur_bottom = None
        for r in page_regions:
            top = r["bbox"][1]
            bottom = top + r["bbox"][3]
            same_row = False
            if current:
                overlap = min(bottom, cur_bottom) - max(top, cur_top)
                min_height = min(bottom - top, cur_bottom - cur_top) or 1
                same_row = overlap > 0 and (overlap / min_height) >= 0.4
            if same_row:
                current.append(r)
                cur_top = min(cur_top, top)
                cur_bottom = max(cur_bottom, bottom)
            else:
                if current:
                    rows.append({"page": page, "regions": sorted(current, key=lambda x: x["bbox"][0])})
                current = [r]
                cur_top, cur_bottom = top, bottom
        if current:
            rows.append({"page": page, "regions": sorted(current, key=lambda x: x["bbox"][0])})
    return rows


def _row_text(row: dict) -> str:
    return " ".join(r["text"] for r in row["regions"])


def _row_confidence(row: dict) -> float:
    confs = [r.get("confidence") for r in row["regions"] if r.get("confidence") is not None]
    return sum(confs) / len(confs) if confs else 50.0


def _label_pattern(label: str) -> re.Pattern:
    return re.compile(rf"(?<![a-zA-Z0-9]){re.escape(label)}(?![a-zA-Z0-9])(?:\s*[:#\-]\s*|\s+)", re.IGNORECASE)


def _value_from_row(row: dict, ftype: str):
    return _clean_value(_row_text(row), ftype)


def _find_label_value(field: dict, rows: list[dict]) -> tuple | None:
    """Scans rows in reading order for one of this field's label
    candidates. A label with real text after it on the same row uses
    that remainder as the value; a label alone on its row falls through
    to the row immediately below. Returns (value, confidence, page) or
    None if nothing usable was found anywhere."""
    ftype = field.get("type", "string")
    prefer_last = field["name"].lower() in PREFER_LAST_FIELDS
    candidates = _label_candidates(field)
    found: list[tuple] = []

    for ri, row in enumerate(rows):
        row_text = _row_text(row)
        for label in candidates:
            for m in _label_pattern(label).finditer(row_text):
                remainder = row_text[m.end():].strip()
                cleaned = _clean_value(remainder, ftype) if remainder else None
                conf_row = row
                if cleaned is None and ri + 1 < len(rows):
                    below = rows[ri + 1]
                    cleaned = _value_from_row(below, ftype)
                    conf_row = below
                if cleaned is None or cleaned == "":
                    continue
                found.append((cleaned, _row_confidence(conf_row), conf_row["page"]))
            if found and not prefer_last:
                break
        if found and not prefer_last:
            break

    if not found:
        return None
    value, conf, page = found[-1] if prefer_last else found[0]
    return value, round(min(conf, 92.0), 1), page  # 92 cap — a heuristic label match, never claimed as certain as a direct read


def _type_fallback(field: dict, full_text: str) -> tuple | None:
    """Grounded, regex-only fallbacks for fields no label-scan matched —
    every value here is something literally present in the text, never
    invented. Deliberately narrow (a handful of well-understood concepts)
    rather than a catch-all, per this engine's "leave it blank rather
    than guess" default."""
    name = field["name"].lower()
    ftype = field.get("type", "string")

    if ftype == "number" and name in ("total_amount", "total"):
        values = _all_money_values(full_text)
        if values:
            return max(values), 55.0, 1

    if ftype == "date" and name in ("invoice_date", "document_date"):
        m = _first_date_match(full_text)
        if m:
            return m, 55.0, 1

    if name.endswith("_email") or name == "email":
        m = EMAIL_RE.search(full_text)
        if m:
            return m.group(0), 60.0, 1

    if name.endswith("_phone") or name == "phone":
        m = PHONE_RE.search(full_text)
        if m and sum(c.isdigit() for c in m.group(0)) >= 7:
            return m.group(0).strip(), 50.0, 1

    if name.endswith("_website") or name == "website":
        m = URL_RE.search(full_text)
        if m and "@" not in m.group(0):
            return m.group(0), 50.0, 1

    if name == "currency":
        m = CURRENCY_CODE_RE.search(full_text)
        if m:
            return m.group(0).upper(), 55.0, 1
        for symbol, code in CURRENCY_SYMBOL_MAP.items():
            if symbol in full_text:
                return code, 45.0, 1

    return None


def _extract_scalar(field: dict, rows: list[dict], full_text: str) -> dict:
    found = _find_label_value(field, rows) or _type_fallback(field, full_text)
    if found is None:
        return {"name": field["name"], "type": "scalar", "value": "", "confidence": 0.0, "page": 1}
    value, conf, page = found
    return {"name": field["name"], "type": "scalar", "value": value, "confidence": round(conf, 1), "page": page}


# ── Table extraction ─────────────────────────────────────────────────

TABLE_COLUMN_SYNONYMS: dict[str, list[str]] = {
    "description": ["description", "item", "particulars", "product", "details"],
    "sku": ["sku", "code", "item code", "item no", "part no"],
    "quantity": ["qty", "quantity", "units"],
    "unit_price": ["unit price", "rate", "price", "unit cost"],
    "tax": ["tax", "gst", "vat"],
    "amount": ["amount", "total", "line total", "net amount"],
}
STOP_KEYWORDS = [
    "subtotal", "sub total", "sub-total", "grand total", "total due", "balance due",
    "amount due", "tax", "terms", "notes", "thank you", "signature", "page ",
]


def _find_header_row(columns: list[dict], rows: list[dict]) -> int | None:
    for idx, row in enumerate(rows):
        text = _row_text(row).lower()
        hits = 0
        for col in columns:
            syns = TABLE_COLUMN_SYNONYMS.get(col["name"].lower(), [_humanize(col["name"])])
            if any(re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", text) for s in syns):
                hits += 1
        if hits >= 2:
            return idx
    return None


def _split_row(row: dict, columns: list[dict]) -> dict | None:
    """Numeric-anchored right-to-left token popping: with no reliable
    column-gap signal left in line-level OCR text (see module docstring),
    this fills the schema's number-typed columns from the row's trailing
    tokens — rightmost token to the last numeric column, working
    backwards — stopping the moment a candidate token doesn't genuinely
    look numeric rather than guessing. Everything left over becomes the
    first column's value (normally "description"); any other column
    (e.g. an optional per-row tax percentage sitting between price and
    amount) is left blank — a real, acknowledged limitation, not a
    fabricated placement."""
    if len(row["regions"]) > 1:
        tokens = [r["text"].strip() for r in row["regions"] if r["text"].strip()]
    else:
        tokens = row["regions"][0]["text"].split()
    if not tokens:
        return None

    remaining = list(tokens)
    row_dict: dict = {c["name"]: "" for c in columns}
    numeric_idxs = [i for i, c in enumerate(columns) if c.get("type") == "number"]
    for idx in reversed(numeric_idxs):
        if remaining and _looks_numeric(remaining[-1]):
            row_dict[columns[idx]["name"]] = _clean_number(remaining.pop())
        else:
            break

    if columns and remaining:
        row_dict[columns[0]["name"]] = " ".join(remaining).strip()

    if not any(v not in ("", None) for v in row_dict.values()):
        return None
    return row_dict


def _extract_table(field: dict, rows: list[dict]) -> dict:
    columns = field.get("columns") or []
    col_names = [c["name"] for c in columns]
    header_idx = _find_header_row(columns, rows) if columns else None
    if header_idx is None:
        # No recognizable header row -> honestly report "found nothing"
        # rather than guessing at row boundaries with no anchor at all.
        return {"name": field["name"], "type": "table", "columns": col_names, "rows": [], "confidence": 0.0}

    rows_out = []
    confs = []
    for row in rows[header_idx + 1: header_idx + 1 + MAX_TABLE_ROWS]:
        if any(kw in _row_text(row).lower() for kw in STOP_KEYWORDS):
            break
        parsed = _split_row(row, columns)
        if parsed is None:
            continue
        rows_out.append(parsed)
        confs.append(_row_confidence(row))

    confidence = round(min(sum(confs) / len(confs), 75.0), 1) if confs else 0.0  # capped — heuristic row-splitting, not a verified read
    return {"name": field["name"], "type": "table", "columns": col_names, "rows": rows_out, "confidence": confidence}


# ── Entry point ───────────────────────────────────────────────────────

def extract_fields(doc: Document, schema: list[dict]) -> dict:
    """Same output contract as extraction_service.extract_fields():
    {"fields": [...], "overallConfidence": float, "model": str, "rawJson": str}."""
    if not schema:
        raise CustomEngineError("No fields to extract.")
    ocr = doc.ocr_result or {}
    full_text = ocr.get("full_text") or ""
    if not full_text.strip():
        raise CustomEngineError("This document has no OCR text yet — run OCR first.")
    regions = ocr.get("regions") or []
    rows = _build_rows(regions)

    fields_out = []
    for f in schema:
        if f.get("type") == "table":
            fields_out.append(_extract_table(f, rows))
        else:
            fields_out.append(_extract_scalar(f, rows, full_text))

    scored = [f for f in fields_out if f.get("type") != "table" or f.get("rows")]
    overall = round(sum(f["confidence"] for f in scored) / len(scored), 1) if scored else 0.0
    return {
        "fields": fields_out,
        "overallConfidence": overall,
        "model": MODEL_LABEL,
        "rawJson": json.dumps(
            {f["name"]: (f["rows"] if f.get("type") == "table" else f["value"]) for f in fields_out},
            indent=2, default=str,
        ),
    }
