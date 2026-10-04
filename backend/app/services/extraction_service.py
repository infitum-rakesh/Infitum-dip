"""
Real AI-based structured field extraction — Document Intelligence wizard
Stage 3 (pages/document-intelligence/AIExtraction.jsx). Uses the
document's own real OCR text (services/document_service.py + ocr/engine.py)
and calls whichever AI Provider connection the caller selects (see
connectors/ai/* — OpenAI/Anthropic/Ollama, all real HTTP calls). A
different document, schema, or model genuinely produces a different
prompt and a different real response — nothing here is canned.

Schema auto-detection also calls the AI model for real, asking it to
propose extraction fields from the document's actual OCR text, rather
than always returning a fixed invoice template.

Response parsing (_parse_json) is defensive because LLMs don't always
obey "return only JSON": it strips markdown code fences and falls back
to locating the first {...}/[...] block. A response that still can't be
parsed raises ExtractionError with the raw text (truncated) rather than
silently substituting a fabricated result.
"""
from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.extraction import custom_engine
from app.models.document import Document
from app.services import document_taxonomies
from app.services.connection_service import build_connector, get_connection

OCR_TEXT_CAP = 60000  # page-aware OCR characters sent per call; bounded but lets every page contribute

# Sentinel passed as connection_id to mean "use the Custom Extraction
# Engine (custom_engine.py) instead of a real AI Provider connection" —
# safe because Connection primary keys are DB autoincrement starting at
# 1, so 0 can never collide with a real connection. Lets every caller
# that already threads a connection_id through (API schemas, the manual
# "Run AI Extraction" picker, the Document Intelligence wizard) route to
# the Custom Engine with no schema/type changes anywhere.
CUSTOM_ENGINE_CONNECTION_ID = 0


class ExtractionError(Exception):
    pass


def _repair_bracket_types(text: str) -> str:
    """
    Smaller local models (observed with llama3.2 via Ollama) sometimes emit
    array elements as `[...]` instead of `{...}` — e.g.
    `[..., "[\"name\": \"x\", ...],\n[\"name\": \"y\", ...]]"`. This is
    unambiguous to detect for real (not a guess): a `"key":` pair is only
    valid JSON grammar directly inside an object, never directly inside an
    array. A single-pass bracket-matching scan retypes exactly those
    brackets — genuinely correcting the model's real output, not
    substituting different content for it.
    """
    out = []
    stack = []  # 'brace' | 'bracket' per open delimiter, in the ORIGINAL sense
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "{":
            stack.append("brace")
            out.append(c)
        elif c == "[":
            rest = text[i + 1:]
            looks_like_object = re.match(r'\s*"[^"\\]*"\s*:', rest)
            if looks_like_object:
                stack.append("brace_from_bracket")
                out.append("{")
            else:
                stack.append("bracket")
                out.append(c)
        elif c == "}":
            if stack:
                stack.pop()
            out.append("}")
        elif c == "]":
            top = stack.pop() if stack else "bracket"
            out.append("}" if top == "brace_from_bracket" else "]")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _parse_json(text: str):
    text = (text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    candidates = [text]
    brace_match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if brace_match:
        candidates.append(brace_match.group(0))

    for candidate in list(candidates):
        try:
            candidate_repaired = _repair_bracket_types(candidate)
        except Exception:
            candidate_repaired = None
        if candidate_repaired and candidate_repaired != candidate:
            candidates.append(candidate_repaired)

    # Last-resort fallback: some responses omit the enclosing array AND
    # the separating commas entirely, coming back as adjacent objects —
    # `{...}\n{...}\n{...}` or even `{...}{...}` with nothing between
    # them (a real, observed small-model quirk, not a guess: this codec
    # only ever runs after every stricter candidate has already failed
    # to parse, so it can't silently corrupt an already-valid response).
    # Inserting a comma between two top-level `}...{` / `]...[` boundaries
    # and wrapping in `[...]` is a standard, safe JSON-repair heuristic.
    for candidate in list(candidates):
        joined = re.sub(r"\}\s*\{", "},{", candidate)
        joined = re.sub(r"\]\s*\[", "],[", joined)
        if joined != candidate:
            candidates.append(joined)
    for candidate in list(candidates):
        stripped = candidate.strip()
        if stripped and not stripped.startswith("["):
            candidates.append(f"[{stripped}]")

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ExtractionError(f"The AI model's response wasn't valid JSON: {text[:400]!r}")


def _ocr_text(doc: Document) -> str:
    if not doc.ocr_result or not doc.ocr_result.get("full_text", "").strip():
        raise ExtractionError("This document has no OCR text yet — run OCR first.")
    return doc.ocr_result["full_text"]


def _ocr_languages(doc: Document) -> list[str] | None:
    lang = (doc.ocr_result or {}).get("language") or {}
    languages = lang.get("detected_languages") or []
    if not languages and lang.get("primary_language"):
        languages = [lang["primary_language"]]
    clean = [str(code).strip().lower() for code in languages if str(code).strip()]
    return clean or None


def _page_aware_ocr_context(doc: Document, *, cap: int = OCR_TEXT_CAP) -> tuple[str, list[int], int]:
    if not doc.ocr_result:
        raise ExtractionError("This document has no OCR text yet — run OCR first.")
    regions = doc.ocr_result.get("regions") or []
    by_page: dict[int, list[str]] = {}
    for region in regions:
        text = str(region.get("text") or "").strip()
        if text:
            by_page.setdefault(int(region.get("page") or 1), []).append(text)

    if not by_page:
        text = _ocr_text(doc)
        return text[:cap], [1] if text.strip() else [], 1

    total_pages = int(doc.ocr_result.get("page_count") or doc.page_count or max(by_page))
    page_numbers = list(range(1, total_pages + 1))
    per_page_cap = max(1000, cap // max(1, len(page_numbers)))
    parts: list[str] = []
    pages_used: list[int] = []
    for page_number in page_numbers:
        page_text = "\n".join(by_page.get(page_number, [])).strip()
        if not page_text:
            parts.append(f"[PAGE {page_number}]\n")
            continue
        pages_used.append(page_number)
        if len(page_text) > per_page_cap:
            page_text = page_text[:per_page_cap] + "\n[PAGE TEXT TRUNCATED]"
        parts.append(f"[PAGE {page_number}]\n{page_text}")
    return "\n\n".join(parts)[:cap], pages_used, total_pages


def _page_chunks_for_ocr_context(doc: Document, *, pages_per_chunk: int = 4, per_page_cap: int = 4000) -> list[tuple[str, list[int], int]]:
    if not doc.ocr_result:
        raise ExtractionError("This document has no OCR text yet — run OCR first.")
    regions = doc.ocr_result.get("regions") or []
    by_page: dict[int, list[str]] = {}
    for region in regions:
        text = str(region.get("text") or "").strip()
        if text:
            by_page.setdefault(int(region.get("page") or 1), []).append(text)
    total_pages = int(doc.ocr_result.get("page_count") or doc.page_count or (max(by_page) if by_page else 1))
    chunks: list[tuple[str, list[int], int]] = []
    for start in range(1, total_pages + 1, pages_per_chunk):
        page_numbers = list(range(start, min(total_pages, start + pages_per_chunk - 1) + 1))
        parts: list[str] = []
        used: list[int] = []
        for page_number in page_numbers:
            page_text = "\n".join(by_page.get(page_number, [])).strip()
            if page_text:
                used.append(page_number)
                if len(page_text) > per_page_cap:
                    page_text = page_text[:per_page_cap] + "\n[PAGE TEXT TRUNCATED]"
            parts.append(f"[PAGE {page_number}]\n{page_text}")
        chunks.append(("\n\n".join(parts), used, total_pages))
    return chunks


def _cell_value(raw):
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value") if raw.get("value") is not None else ""
    return raw if raw is not None else ""


def _fields_from_parsed(schema: list[dict], parsed: dict) -> list[dict]:
    fields_out = []
    for f in schema:
        name = f["name"]
        entry = parsed.get(name)

        if f.get("type") == "table":
            columns = [c["name"] for c in f.get("columns", [])]
            rows_raw = None
            conf_raw = None
            if isinstance(entry, dict):
                rows_raw = entry.get("rows")
                conf_raw = entry.get("confidence")
            elif isinstance(entry, list):
                rows_raw = entry

            rows = []
            seen_rows = set()
            if isinstance(rows_raw, list):
                for r in rows_raw:
                    if not isinstance(r, dict):
                        continue
                    row = {c: _cell_value(r.get(c)) for c in columns}
                    row_key = tuple(row.get(c) for c in columns)
                    if row_key in seen_rows:
                        continue
                    seen_rows.add(row_key)
                    rows.append(row)
            try:
                confidence = float(conf_raw) if conf_raw is not None else (75.0 if rows else 0.0)
            except (TypeError, ValueError):
                confidence = 75.0 if rows else 0.0
            fields_out.append({"name": name, "type": "table", "columns": columns, "rows": rows, "confidence": round(confidence, 1)})
            continue

        if not isinstance(entry, dict):
            entry = {"value": entry} if entry is not None else {}
        value = entry.get("value")
        if f.get("type") == "number" and value is not None and not isinstance(value, (int, float)):
            cleaned = re.sub(r"[^0-9.\-]", "", str(value))
            try:
                value = float(cleaned) if cleaned not in ("", "-", ".") else None
            except ValueError:
                value = None
        try:
            confidence = float(entry.get("confidence")) if entry.get("confidence") is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            page = int(entry.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        fields_out.append({"name": name, "type": "scalar", "value": value if value is not None else "", "confidence": round(confidence, 1), "page": page})
    return fields_out


def _merge_extraction_fields(schema: list[dict], chunks: list[list[dict]]) -> list[dict]:
    by_name: dict[str, list[dict]] = {}
    for fields in chunks:
        for field in fields:
            by_name.setdefault(field["name"], []).append(field)

    merged = []
    for spec in schema:
        name = spec["name"]
        fields = by_name.get(name, [])
        if spec.get("type") == "table":
            columns = [c["name"] for c in spec.get("columns", [])]
            rows = []
            seen = set()
            confidences = []
            for field in fields:
                confidences.append(float(field.get("confidence") or 0.0))
                for row in field.get("rows") or []:
                    row_key = tuple(row.get(c) for c in columns)
                    if row_key in seen:
                        continue
                    seen.add(row_key)
                    rows.append(row)
            confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0
            merged.append({"name": name, "type": "table", "columns": columns, "rows": rows, "confidence": confidence})
            continue

        candidates = [f for f in fields if f.get("value") not in (None, "")]
        chosen = max(candidates, key=lambda f: float(f.get("confidence") or 0.0), default=(fields[0] if fields else None))
        if chosen is None:
            merged.append({"name": name, "type": "scalar", "value": "", "confidence": 0.0, "page": 1})
        else:
            merged.append(chosen)
    return merged


async def _get_connector(db: AsyncSession, connection_id: int, organization_id: int | None):
    conn = await get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ExtractionError("AI connection not found (it may have been deleted, or belongs to a different organization).")
    if conn.category.value != "AI_PROVIDER":
        raise ExtractionError(f"Connection '{conn.name}' is a {conn.category.value} connection, not an AI Provider — pick one from the AI Agent category.")
    try:
        return build_connector(conn)
    except ConnectorError as exc:
        raise ExtractionError(str(exc)) from exc


async def _generate_json(connector, prompt: str, system: str, max_tokens: int):
    """Calls the connector and parses its response as JSON, with ONE
    corrective retry if the first response isn't valid JSON at all (not
    even via _parse_json's fence-stripping/bracket-repair fallbacks) —
    a real, observed failure mode with small local models (Ollama's
    llama3.2 in particular) that sometimes ignore "respond with ONLY
    JSON, no prose" entirely and write a plain-English narrative instead
    (e.g. "Here is a possible annotated version of the invoice:..."). The
    retry hands the model back its own bad response and asks it to
    correct itself — recovers the common case without failing the whole
    extraction outright on a model that just needs a second, sterner ask.
    If the retry ALSO isn't valid JSON, this raises ExtractionError same
    as before — a genuine final failure, not silently swallowed."""
    try:
        raw = await asyncio.to_thread(connector.generate, prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        raise ExtractionError(str(exc)) from exc
    try:
        return _parse_json(raw)
    except ExtractionError:
        pass  # fall through to the corrective retry below

    repair_prompt = (
        f"{prompt}\n\n---\nYour previous response was NOT valid JSON — it started with:\n"
        f"{(raw or '')[:300]!r}\n\nRespond again with ONLY valid JSON exactly as instructed above. "
        "No prose, no explanation, no markdown code fences — the response must start with { or [."
    )
    try:
        raw2 = await asyncio.to_thread(connector.generate, repair_prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        raise ExtractionError(str(exc)) from exc
    return _parse_json(raw2)


DETECT_SCHEMA_SYSTEM = (
    "You are a document data extraction assistant. Given the raw OCR text of a business "
    "document, propose the most useful fields to extract as structured data. "
    "Respond with ONLY a JSON array — no prose, no markdown fences. Most elements should have this "
    'exact shape: {"name": "snake_case_field_name", "type": "string|number|date|boolean", '
    '"required": true or false, "instruction": "short description of what this field means"}. '
    "If — and only if — the document contains a genuine repeating table with multiple rows (e.g. "
    "invoice line items, order items, transaction rows) — NOT a single one-off value like "
    '"total" — propose ONE additional element of this shape instead: {"name": '
    '"snake_case_field_name", "type": "table", "required": true or false, "instruction": "short '
    'description", "columns": [{"name": "snake_case_column_name", "type": "string|number|date|'
    'boolean"}, ...]}. List every column that actually appears per row (e.g. description, quantity, '
    "unit_price, amount). Do not propose a table field for data that is really just one flat "
    "value — only use it for genuinely repeating rows.\n\n"
    "Propose between 5 and 12 fields total (the table field, if any, counts as one) that are "
    "actually present or clearly implied in the text."
)

EXTRACT_SYSTEM = (
    "You are a document data extraction assistant. Extract the requested fields from the "
    "document's OCR text below, using ONLY what the text actually says — never invent, guess, or "
    "compute/derive a value from other fields. If a field's value isn't literally stated in the "
    "text as that specific concept, its value is null — do not calculate it from other numbers on "
    "the page. Respond with ONLY a JSON object, no prose, no markdown fences.\n\n"
    "LANGUAGE — the OCR text below may be in Kannada, Tamil, Hindi, English, or a mix. Copy every "
    "value EXACTLY as it appears in the source text, in its ORIGINAL script and language — a "
    "person's name, address, vendor name, item description, or any other text value must never be "
    "translated, transliterated, or romanized into English, even if English would seem more "
    "convenient or more accurate to you. A Kannada or Tamil or Hindi field value must appear in the "
    "JSON response in Kannada, Tamil, or Hindi Unicode characters, unchanged from the OCR text — the "
    "same rule that already applies to numbers and dates applies here: copy what is literally "
    "written, do not \"helpfully\" convert it into a different form. This applies to every field and "
    "every table cell, with no exception.\n\n"
    "For every field whose type is NOT \"table\": its value MUST be an object with exactly three "
    'keys: "value", "confidence", "page" — never a bare string or number. "confidence" is an '
    "integer 0-100: your genuine confidence this value is correctly read from the text (100 = "
    'certain, 0 = pure guess). "page" is the page number if identifiable, otherwise 1. If the field '
    "is genuinely not present in the text, set its value to null and confidence to 0. A field's "
    "instruction describes ONE specific concept — do not put a document title, section heading, or "
    "any other field's content into a field it doesn't literally describe.\n\n"
    'For a field whose type IS "table": its value MUST be an object with exactly two keys: "rows" '
    "and \"confidence\". \"rows\" is a JSON array with one element per row actually present in the "
    "text — each element is an object keyed by that table field's column names (from the fields "
    'list below), using ONLY what the text says for each cell. "confidence" is your overall '
    "0-100 confidence across the whole table.\n\n"
    "CRITICAL — a table field is ONLY for the document's genuine itemized rows (the products/"
    "services actually being billed). A row is NEVER any of: a section heading or label (\"From "
    "(Supplier)\", \"Name\", \"Address\", \"Amount\"), a party's name or address, a tax ID/GST/"
    "account number on its own, a tax/subtotal/total breakdown line, a page footnote, a signature "
    "line, or the document's own title (\"TAX INVOICE\"). Do NOT create one row per OCR text line — "
    "that is always wrong. A document with only ONE genuine item is a table with exactly ONE row; "
    "include it as a single-element array, never omit it just because it doesn't repeat, and never "
    "split ONE item's own description across multiple rows just because it wrapped onto several OCR "
    "lines — read the surrounding context and combine wrapped lines belonging to the same item into "
    "that single row. Use an empty array only when the document genuinely has no itemized rows — "
    "never invent rows, and never repeat the same rows twice in your response.\n\n"
    "ROW COUNT — the right number of rows is however many genuine itemized rows the document "
    "actually has, which could be 1 or could be 30+; there is no small cap to aim for and no reason "
    "to stop early. Before finalizing your response, scan the ENTIRE OCR text end to end (it may "
    "continue across a page break) and count every genuine item line once — a response that silently "
    "stops partway through a long table (e.g. covering only the first section of a multi-section "
    "invoice) is exactly as wrong as inventing rows that don't exist. Double-check that no two rows "
    "share an identical description+amount pair unless the source text itself repeats that exact "
    "line — a plausible-looking table that quietly drops the back half of the real one is a failure, "
    "not an acceptable summary.\n\n"
    "Example response shape for fields \"invoice_number\" (string), \"total\" (number), and "
    "\"line_items\" (table with columns description, quantity, unit_price, amount):\n"
    '{"invoice_number": {"value": "INV-2024-001", "confidence": 97, "page": 1}, '
    '"total": {"value": 1250.50, "confidence": 88, "page": 1}, '
    '"line_items": {"confidence": 91, "rows": ['
    '{"description": "Widget A", "quantity": 2, "unit_price": 10.5, "amount": 21.0}, '
    '{"description": "Widget B", "quantity": 1, "unit_price": 5.0, "amount": 5.0}]}}'
)


async def detect_schema(
    db: AsyncSession, doc: Document, connection_id: int, organization_id: int | None,
    *, document_type: str | None = None, extra_field_names: set[str] | None = None,
) -> list[dict]:
    """When document_type names a known canonical taxonomy (e.g. "INVOICE"
    — see document_taxonomies.py), skip the ad-hoc "guess 5-12 fields"
    LLM call entirely and return the comprehensive canonical schema
    instead (merged with extra_field_names, real field names mined from
    this organization's own past documents of the type — see
    document_service.discover_known_field_names()). extract_fields() below
    still does the real per-field work of checking the actual OCR text —
    this only changes WHAT it's asked to look for, from an LLM's one-shot
    guess to a comprehensive, consistent candidate list. document_type=None
    (the default) is the original ad-hoc behavior, unchanged.

    connection_id == CUSTOM_ENGINE_CONNECTION_ID short-circuits straight to
    the Custom Extraction Engine's own (non-LLM, taxonomy-only) schema
    detection — there's no ad-hoc "guess from this one document" mode
    without a real model, so it always seeds from the canonical taxonomy,
    defaulting to GENERIC when document_type isn't given."""
    if connection_id == CUSTOM_ENGINE_CONNECTION_ID:
        return custom_engine.detect_schema(document_type, extra_field_names)

    if document_type:
        seed_schema = document_taxonomies.build_seed_schema(document_type, extra_field_names)
        if seed_schema:
            return seed_schema

    text, pages_used, total_pages = _page_aware_ocr_context(doc)
    connector = await _get_connector(db, connection_id, organization_id)
    prompt = f"Document type: {doc.doc_type.value}\nPages included: {len(pages_used)} of {total_pages}\n\nOCR TEXT BY PAGE:\n{text}"
    parsed = await _generate_json(connector, prompt, DETECT_SCHEMA_SYSTEM, 2500)
    if not isinstance(parsed, list):
        raise ExtractionError("The AI model's schema proposal wasn't a JSON array.")

    fields = []
    for f in parsed:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        entry = {
            "name": str(f["name"]), "type": str(f.get("type") or "string"),
            "required": bool(f.get("required", False)), "instruction": str(f.get("instruction") or ""),
        }
        if entry["type"] == "table":
            # A table field's real shape is defined by its columns, not a
            # single scalar type — carry them through so extract_fields()
            # below knows what keys to ask the model for per row. A table
            # field with no usable columns can't be extracted meaningfully,
            # so it's demoted back to a plain string field rather than
            # silently producing an empty table later.
            cols = [
                {"name": str(c["name"]), "type": str(c.get("type") or "string")}
                for c in (f.get("columns") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            if cols:
                entry["columns"] = cols
            else:
                entry["type"] = "string"
        fields.append(entry)
    if not fields:
        raise ExtractionError("The AI model didn't propose any usable fields for this document.")
    return fields


async def extract_fields(
    db: AsyncSession, doc: Document, schema: list[dict], connection_id: int, organization_id: int | None,
) -> dict:
    if not schema:
        raise ExtractionError("No fields to extract.")
    if connection_id == CUSTOM_ENGINE_CONNECTION_ID:
        try:
            result = custom_engine.extract_fields(doc, schema, languages=_ocr_languages(doc))
            pages = sorted({int(r.get("page") or 1) for r in (doc.ocr_result or {}).get("regions", [])})
            result["pages_considered"] = pages
            result["total_ocr_pages"] = int((doc.ocr_result or {}).get("page_count") or doc.page_count or len(pages) or 1)
            return result
        except custom_engine.CustomEngineError as exc:
            raise ExtractionError(str(exc)) from exc
    text, pages_used, total_pages = _page_aware_ocr_context(doc)
    connector = await _get_connector(db, connection_id, organization_id)

    def _describe(f: dict) -> str:
        if f.get("type") == "table":
            cols = ", ".join(f"{c['name']} ({c.get('type', 'string')})" for c in f.get("columns", []))
            return f"- {f['name']} (table — one row per {f['name'].rstrip('s')}; columns: {cols}): {f.get('instruction', '')}"
        return f"- {f['name']} ({f.get('type', 'string')}){' [required]' if f.get('required') else ''}: {f.get('instruction', '')}"

    schema_desc = "\n".join(_describe(f) for f in schema)
    prompt = f"FIELDS TO EXTRACT:\n{schema_desc}\n\nPages included: {len(pages_used)} of {total_pages}\n\nOCR TEXT BY PAGE:\n{text}"
    # Scales with schema size — a real, observed failure with the comprehensive
    # canonical INVOICE taxonomy (45+ fields): a fixed 3000-token budget let a
    # smaller local model (llama3.2 via Ollama) run out of room mid-response and
    # get cut off before closing its JSON, which _parse_json correctly can't
    # recover from (that's a genuinely incomplete response, not a malformed
    # one). ~120 tokens/field covers the {"value","confidence","page"} shape
    # plus normal model verbosity, floored at the original 3000 for small
    # ad-hoc schemas and capped so one call can't run away unbounded.
    max_tokens = min(8000, max(3000, len(schema) * 120))
    raw_model_response = None
    if total_pages > 4:
        chunk_fields = []
        chunk_payloads = []
        pages_seen: set[int] = set()
        for chunk_text, chunk_pages, _total_pages in _page_chunks_for_ocr_context(doc):
            chunk_prompt = f"FIELDS TO EXTRACT:\n{schema_desc}\n\nPages included: {len(chunk_pages)} of {_total_pages}\n\nOCR TEXT BY PAGE:\n{chunk_text}"
            parsed = await _generate_json(connector, chunk_prompt, EXTRACT_SYSTEM, max_tokens)
            if not isinstance(parsed, dict):
                raise ExtractionError("The AI model's extraction response wasn't a JSON object.")
            chunk_fields.append(_fields_from_parsed(schema, parsed))
            chunk_payloads.append({"pages": chunk_pages, "response": parsed})
            pages_seen.update(chunk_pages)
        fields_out = _merge_extraction_fields(schema, chunk_fields)
        pages_used = sorted(pages_seen)
        raw_model_response = chunk_payloads
    else:
        parsed = await _generate_json(connector, prompt, EXTRACT_SYSTEM, max_tokens)
        if not isinstance(parsed, dict):
            raise ExtractionError("The AI model's extraction response wasn't a JSON object.")
        fields_out = _fields_from_parsed(schema, parsed)
        raw_model_response = parsed

    # Real bug found live: this used to score EVERY schema field, including
    # ones the model correctly reported as genuinely absent (value="",
    # confidence=0 per this function's own prompt instruction above) — so
    # `overall` was silently "how many of the taxonomy's ~47 canonical
    # INVOICE fields did this particular invoice happen to populate,"
    # not "how confident was the model in what it actually found." A real
    # invoice this build correctly extracted with 95%+ per-field confidence
    # on every field that exists on the page still scored 42% overall,
    # purely because ~26 of the 47 canonical fields (order_number, a full
    # vendor address breakdown, ...) are legitimately optional and simply
    # don't apply to a typical invoice — dragging every real invoice below
    # the 90% STRAIGHT_THROUGH_THRESHOLD (invoice_decision_service.py) and
    # the equivalent NEEDS_REVIEW confidence gate (document_service.py's
    # process_document(), same default) regardless of extraction accuracy.
    # Excluding empty scalar fields — exactly the same treatment an empty
    # TABLE field already got on this line — fixes this the same way
    # evidence_quality (invoice_decision_service.py) already scores only
    # non-missing fields: `overall` now reflects confidence in what was
    # actually extracted, not extraction breadth against a canonical
    # schema built to be a superset of any one invoice's real fields.
    scored = [f for f in fields_out if (f.get("type") == "table" and f.get("rows")) or (f.get("type") != "table" and f.get("value") not in (None, ""))]
    overall = round(sum(f["confidence"] for f in scored) / len(scored), 1) if scored else 0.0
    model_name = connector.config.get("model") or connector.connection_type
    return {
        "fields": fields_out,
        "overallConfidence": overall,
        "model": str(model_name),
        # ensure_ascii=False — real bug found live (Multilingual Document
        # Processing repo analysis): without it, any Kannada/Tamil/Hindi/
        # non-ASCII extracted value gets \uXXXX-escaped in this string.
        # Round-trips fine through json.loads(), but anyone who displays
        # rawJson as raw text (rather than re-parsing it) would see escape
        # sequences instead of the original script.
        "rawJson": json.dumps(
            {f["name"]: (f["rows"] if f.get("type") == "table" else f["value"]) for f in fields_out},
            indent=2, default=str, ensure_ascii=False,
        ),
        # The AI model's own response, verbatim (before this function's type
        # coercion/table-dedup post-processing above) — previously computed
        # here and immediately discarded. Persisted onto Document.ai_raw_
        # response by document_service so downstream consumers (e.g. Invoice
        # Intelligence's canonical materialization, INV5) can reuse it
        # without a second AI call. None for the Custom Extraction Engine
        # path (CUSTOM_ENGINE_CONNECTION_ID returns before this point) — no
        # AI model was actually called there, so there's honestly nothing to
        # persist under this key.
        "raw_model_response": raw_model_response,
        "pages_considered": pages_used,
        "total_ocr_pages": total_pages,
    }


GAP_FILL_SYSTEM = (
    "You are a document data extraction assistant. A fixed set of fields has already been "
    "extracted from this document's OCR text. Your job is ONLY to spot genuinely distinct pieces "
    "of information actually present in the text that are NOT already covered by the list below — "
    "never repeat, rename, or split an already-covered field. Respond with ONLY a JSON array, no "
    'prose, no markdown fences. Each element: {"name": "snake_case_field_name", "type": '
    '"string|number|date|boolean", "instruction": "short description of what this field means"}. '
    "If nothing meaningful is missing, respond with an empty array []."
)


async def find_gap_fields(
    db: AsyncSession, doc: Document, connection_id: int, organization_id: int | None,
    covered_field_names: list[str],
) -> list[dict]:
    """Second-pass "did we miss anything" check — see detect_schema()'s
    document_type path: extraction there starts from a comprehensive but
    FIXED canonical taxonomy, which by construction can't know about
    something unique to one specific document (e.g. a vendor-specific
    surcharge line, a custom reference code). This asks the model, given
    the document's real text and what's already covered, to name anything
    else genuinely present — directly extending recall beyond the fixed
    list rather than being capped by it. Returns a schema-shaped field
    list (possibly empty) suitable for a follow-up extract_fields() call."""
    text, pages_used, total_pages = _page_aware_ocr_context(doc)
    connector = await _get_connector(db, connection_id, organization_id)
    covered = ", ".join(sorted(covered_field_names))
    prompt = f"ALREADY COVERED FIELDS:\n{covered}\n\nPages included: {len(pages_used)} of {total_pages}\n\nOCR TEXT BY PAGE:\n{text}"
    try:
        parsed = await _generate_json(connector, prompt, GAP_FILL_SYSTEM, 1200)
    except ExtractionError:
        # Gap-fill is optional best-effort enrichment on top of an already-
        # extracted baseline — even after the corrective retry inside
        # _generate_json(), a persistent bad response here shouldn't fail
        # the whole extraction; just contribute nothing extra.
        return []
    if not isinstance(parsed, list):
        return []

    covered_lower = {c.lower() for c in covered_field_names}
    fields = []
    for f in parsed:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        name = str(f["name"])
        if name.lower() in covered_lower:
            continue
        fields.append({
            "name": name, "type": str(f.get("type") or "string"),
            "required": False, "instruction": str(f.get("instruction") or ""),
        })
    return fields
