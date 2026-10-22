"""
Canonical field taxonomies for AI Extraction, keyed by a user-chosen
document type (see api/documents.py's /run-extraction, called from the
Document Details and Review Queue pages).

Why this exists: extraction_service.detect_schema()'s default mode asks
the LLM to invent a schema from scratch for each document — genuinely
data-driven, but that also means it only ever proposes what one document
happens to make obvious, and different documents of the same real-world
type (e.g. two invoices from different vendors) can come back with
inconsistent field sets/names. When the user tells us the document IS an
invoice, we can do much better: seed the model with a comprehensive,
real-world invoice field taxonomy — including granular address
sub-fields (street/city/state/postal/country, not one flattened "address"
string) — and ask it to (a) confirm which of these are actually present
in THIS document, using only what the text says, and (b) surface any
extra fields this document has that aren't in the canonical list. This is
still 100% grounded in the document's real OCR text — the taxonomy only
shapes WHAT to look for, never fabricates a value the model doesn't find.

TAXONOMIES values are also merged with field names mined from the
organization's own already-extracted documents (see
discover_known_field_names() in extraction_service.py) — so the schema a
new invoice gets seeded with genuinely reflects what this organization's
real invoices have contained before, not just a generic template.
"""
from __future__ import annotations

# Each entry: (name, type, instruction). type is one of
# string|number|date|boolean — matches extraction_service's scalar types.
INVOICE_FIELDS: list[tuple[str, str, str]] = [
    # Document identifiers
    ("invoice_number", "string", "The invoice's own identifying number"),
    ("invoice_date", "date", "The date the invoice was issued"),
    ("due_date", "date", "The payment due date"),
    ("po_number", "string", "Purchase order number this invoice is billed against, if any"),
    ("order_number", "string", "Sales/order number, if different from the PO number"),
    ("reference_number", "string", "Any other reference/tracking number printed on the invoice"),

    # Vendor / seller — who issued the invoice
    ("vendor_name", "string", "The company or person issuing/selling — who is being paid"),
    ("vendor_address_line1", "string", "Vendor street address, first line"),
    ("vendor_address_line2", "string", "Vendor street address, second line (suite/unit/floor) if present"),
    ("vendor_city", "string", "Vendor city"),
    ("vendor_state", "string", "Vendor state/province/region"),
    ("vendor_postal_code", "string", "Vendor postal/ZIP code"),
    ("vendor_country", "string", "Vendor country"),
    ("vendor_tax_id", "string", "Vendor's tax ID / VAT number / GSTIN / EIN"),
    ("vendor_gstin", "string", "Vendor's GSTIN (Goods and Services Tax Identification Number), if this is an Indian GST invoice — leave blank if not shown separately from vendor_tax_id"),
    ("vendor_pan", "string", "Vendor's PAN (Permanent Account Number), if shown separately from GSTIN"),
    ("vendor_email", "string", "Vendor contact email"),
    ("vendor_phone", "string", "Vendor contact phone number"),
    ("vendor_website", "string", "Vendor website"),

    # Bill-to — who is being invoiced
    ("bill_to_name", "string", "The person or company being billed"),
    ("bill_to_address_line1", "string", "Bill-to street address, first line"),
    ("bill_to_address_line2", "string", "Bill-to street address, second line if present"),
    ("bill_to_city", "string", "Bill-to city"),
    ("bill_to_state", "string", "Bill-to state/province/region"),
    ("bill_to_postal_code", "string", "Bill-to postal/ZIP code"),
    ("bill_to_country", "string", "Bill-to country"),
    ("buyer_gstin", "string", "Buyer/recipient's GSTIN, if an Indian GST invoice"),
    ("place_of_supply", "string", "Place of supply state name or code, for Indian GST invoices"),

    # Ship-to — where goods were/are shipped, if different from bill-to
    ("ship_to_name", "string", "The person or company goods were shipped to, if stated separately from bill-to"),
    ("ship_to_address_line1", "string", "Ship-to street address, first line"),
    ("ship_to_address_line2", "string", "Ship-to street address, second line if present"),
    ("ship_to_city", "string", "Ship-to city"),
    ("ship_to_state", "string", "Ship-to state/province/region"),
    ("ship_to_postal_code", "string", "Ship-to postal/ZIP code"),
    ("ship_to_country", "string", "Ship-to country"),

    # Money
    ("currency", "string", "Currency code or symbol used on the invoice"),
    ("subtotal", "number", "Sum of line items before tax/discount/shipping"),
    ("discount_amount", "number", "Total discount applied, if any"),
    ("tax_rate", "string", "Tax rate applied (e.g. a percentage)"),
    ("tax_amount", "number", "Total tax amount"),
    ("cgst_amount", "number", "Total Central GST (CGST) amount, for intra-state Indian GST invoices"),
    ("sgst_amount", "number", "Total State GST (SGST/UTGST) amount, for intra-state Indian GST invoices"),
    ("igst_amount", "number", "Total Integrated GST (IGST) amount, for inter-state Indian GST invoices"),
    ("cess_amount", "number", "Total cess amount, if any, for Indian GST invoices"),
    ("shipping_amount", "number", "Shipping/freight charge, if any"),
    ("total_amount", "number", "The final total amount due on the invoice"),
    ("amount_paid", "number", "Amount already paid, if any is shown"),
    ("balance_due", "number", "Remaining balance due, if shown separately from the total"),

    # Payment
    ("payment_terms", "string", "Payment terms text (e.g. Net 30, Due on Receipt) — NOT the document's own title/heading like 'TAX INVOICE', and NOT present at all on most receipts"),
    ("payment_method", "string", "How payment should be/was made"),
    ("payment_reference", "string", "Bank UTR / cheque / transaction reference number for a payment, if this document reflects a payment"),
    ("bank_name", "string", "Bank name for wire/transfer payment, if shown"),
    ("bank_account_number", "string", "Bank account number for payment, if shown"),
    ("bank_routing_number", "string", "Bank routing/SWIFT/IFSC code for payment, if shown"),

    # Indian tax-deducted-at-source
    ("tds_section", "string", "TDS section under which tax was deducted (e.g. 194C, 194J), if applicable"),
    ("tds_rate", "string", "TDS rate applied, if shown"),
    ("tds_amount", "number", "TDS amount deducted, if applicable"),

    ("notes", "string", "Any free-text notes, remarks, or special instructions on the invoice"),
]

# The one genuinely repeating structure on almost every invoice.
INVOICE_LINE_ITEMS_TABLE = {
    "name": "line_items",
    "instruction": "Each individual line item/row in the invoice's itemized table",
    "columns": [
        {"name": "description", "type": "string"},
        {"name": "sku", "type": "string"},
        {"name": "hsn_sac_code", "type": "string"},
        {"name": "uom", "type": "string"},
        {"name": "quantity", "type": "number"},
        {"name": "unit_price", "type": "number"},
        {"name": "tax", "type": "string"},
        {"name": "cgst_rate", "type": "string"},
        {"name": "cgst_amount", "type": "number"},
        {"name": "sgst_rate", "type": "string"},
        {"name": "sgst_amount", "type": "number"},
        {"name": "igst_rate", "type": "string"},
        {"name": "igst_amount", "type": "number"},
        {"name": "amount", "type": "number"},
    ],
}

# Wide-net fallback for when the caller hasn't told us (or the LLM's own
# ad-hoc schema-detection call came back with nothing usable) what specific
# type a document is. Deliberately not invoice-specific — covers the common
# ground across receipts, contracts, forms, statements, letters, IDs: who's
# involved (with the same granular address breakdown as INVOICE, not one
# flattened string), what dates/numbers/amounts appear, contact details,
# and a generic repeating-row table for whatever tabular data is present
# (receipts' line items, statements' transaction rows, forms' repeated
# entries — column names are intentionally generic so the model maps
# whatever columns the real table actually has onto them).
GENERIC_FIELDS: list[tuple[str, str, str]] = [
    ("document_title", "string", "The document's own title or heading, if any"),
    ("document_number", "string", "Any identifying number printed on the document (invoice/receipt/reference/ID/policy/case number)"),
    ("document_date", "date", "The primary date on the document (issued, signed, or dated)"),
    ("due_date", "date", "A due/expiry/deadline date, if present"),

    ("party_1_name", "string", "The first named person or organization on the document (sender, issuer, seller, employer, landlord, first party)"),
    ("party_1_address_line1", "string", "Party 1 street address, first line"),
    ("party_1_address_line2", "string", "Party 1 street address, second line if present"),
    ("party_1_city", "string", "Party 1 city"),
    ("party_1_state", "string", "Party 1 state/province/region"),
    ("party_1_postal_code", "string", "Party 1 postal/ZIP code"),
    ("party_1_country", "string", "Party 1 country"),
    ("party_1_id_number", "string", "Party 1's own ID/registration/tax number, if shown"),
    ("party_1_email", "string", "Party 1 email"),
    ("party_1_phone", "string", "Party 1 phone number"),

    ("party_2_name", "string", "The second named person or organization on the document (recipient, buyer, employee, tenant, second party)"),
    ("party_2_address_line1", "string", "Party 2 street address, first line"),
    ("party_2_address_line2", "string", "Party 2 street address, second line if present"),
    ("party_2_city", "string", "Party 2 city"),
    ("party_2_state", "string", "Party 2 state/province/region"),
    ("party_2_postal_code", "string", "Party 2 postal/ZIP code"),
    ("party_2_country", "string", "Party 2 country"),
    ("party_2_id_number", "string", "Party 2's own ID/registration/tax number, if shown"),
    ("party_2_email", "string", "Party 2 email"),
    ("party_2_phone", "string", "Party 2 phone number"),

    ("currency", "string", "Currency code or symbol used, if any monetary amounts appear"),
    ("subtotal_amount", "number", "A subtotal amount, if present"),
    ("tax_amount", "number", "A tax amount, if present"),
    ("total_amount", "number", "The main/final/grand total amount on the document, if present"),
    ("balance_amount", "number", "A balance due/remaining/outstanding amount, if present"),

    ("reference_number", "string", "Any other reference, order, tracking, or confirmation number"),
    ("status", "string", "A status/state word printed on the document (e.g. Paid, Pending, Approved, Active)"),
    ("terms", "string", "Any terms, conditions, or validity period text"),
    ("signature_name", "string", "A signatory's printed name, if the document is signed"),
    ("notes", "string", "Any free-text notes, remarks, or special instructions"),
]

GENERIC_TABLE = {
    "name": "line_items",
    "instruction": "Each row of any repeating table on the document (line items, transactions, entries) — map the real columns onto description/quantity/unit_price/amount as closely as they actually correspond; leave a column blank for a row where the document has no equivalent value",
    "columns": [
        {"name": "description", "type": "string"},
        {"name": "quantity", "type": "number"},
        {"name": "unit_price", "type": "number"},
        {"name": "amount", "type": "number"},
    ],
}

# Memorandum of Association (Compliance & Verification module, Phase
# Group 3 / IV14) — fields follow the real clause structure Indian
# company law gives every MOA (Companies Act, 2013, Form INC-33/
# Table A-J schedules): Name Clause, Registered Office Clause, Objects
# Clause, Liability Clause, Capital Clause, and the subscription/
# association clause listing the first subscribers. Extracted exactly
# like every other taxonomy — grounded in this document's real OCR text,
# never fabricated — and then handed to moa_verification_service.py for
# the completeness/authenticity/cross-record checks spec section 11
# actually asks for (OCR+AI extraction alone can confirm a clause's
# TEXT is present; it can never independently prove the document is
# genuine, currently effective, or filed with MCA — see that module's
# own docstring for how it keeps those two kinds of claim separate).
MOA_FIELDS: list[tuple[str, str, str]] = [
    ("document_title", "string", "The document's own title/heading, e.g. 'Memorandum of Association'"),
    ("company_name", "string", "The company's full legal name as stated in the Name Clause"),
    ("cin", "string", "Corporate Identification Number (CIN), if printed on the document"),
    ("llpin", "string", "LLP Identification Number, if this is an LLP agreement rather than a company MOA"),
    ("company_type", "string", "Entity type/classification stated (e.g. Private Limited, Public Limited, One Person Company, Section 8)"),
    ("registered_office_address", "string", "The full registered office address as stated in the Registered Office Clause"),
    ("state_of_registration", "string", "The State in which the registered office is situated, per the Registered Office Clause"),
    ("incorporation_date", "date", "Date of incorporation, if stated"),
    ("roc_jurisdiction", "string", "Registrar of Companies (RoC) jurisdiction named, if stated"),

    ("objects_clause_text", "string", "The full text of the Objects Clause — the company's main objects and matters necessary for their attainment"),

    ("liability_clause_text", "string", "The full text of the Liability Clause"),
    ("liability_type", "string", "The liability type stated — Limited by Shares / Limited by Guarantee / Unlimited"),

    ("capital_clause_text", "string", "The full text of the Capital Clause"),
    ("authorized_capital_amount", "number", "Authorized share capital amount stated in the Capital Clause"),
    ("authorized_capital_currency", "string", "Currency of the authorized capital amount"),
    ("number_of_shares", "number", "Number of shares the authorized capital is divided into"),
    ("share_face_value", "number", "Face value per share"),

    ("execution_date", "date", "The date the memorandum was executed/adopted/signed"),
    ("amendment_indicator", "string", "Any text indicating this is an amended/altered memorandum, and the amendment date if stated — blank for an original, unamended memorandum"),
    ("has_signature", "boolean", "Whether the document shows a signature block for subscribers/witnesses"),
    ("has_company_seal_or_stamp", "boolean", "Whether a company seal, rubber stamp, or certification mark is visible"),
    ("certifying_authority", "string", "Any certifying professional/authority named (e.g. a practising Company Secretary) who certified the document, if stated"),
    ("total_pages_stated", "number", "Total page count, if stated on the document itself (e.g. a page footer or certificate)"),
    ("referenced_schedules", "string", "Any schedules, annexures, or attachments referenced in the body text, if any"),
]

MOA_SUBSCRIBERS_TABLE = {
    "name": "subscribers",
    "instruction": "Each subscriber/signatory listed in the Memorandum's subscription/association clause — the people who subscribed for shares and signed as the company's first members",
    "columns": [
        {"name": "subscriber_name", "type": "string"},
        {"name": "father_or_husband_name", "type": "string"},
        {"name": "address", "type": "string"},
        {"name": "occupation", "type": "string"},
        {"name": "shares_subscribed", "type": "number"},
        {"name": "witness_name", "type": "string"},
    ],
}

TAXONOMIES: dict[str, dict] = {
    "INVOICE": {"fields": INVOICE_FIELDS, "table": INVOICE_LINE_ITEMS_TABLE},
    "GENERIC": {"fields": GENERIC_FIELDS, "table": GENERIC_TABLE},
    "MOA": {"fields": MOA_FIELDS, "table": MOA_SUBSCRIBERS_TABLE},
}


def build_seed_schema(document_type: str, extra_field_names: set[str] | None = None) -> list[dict]:
    """Builds the seed schema list (same shape detect_schema()/extract_fields()
    already use) for a canonical document type, plus any extra field names
    discovered from the organization's own document history — those are
    added as generic string fields since we don't know their real type
    ahead of time; the model still only fills in a value if it's genuinely
    present in THIS document's text."""
    taxonomy = TAXONOMIES.get(document_type.upper())
    if not taxonomy:
        return []

    seen = set()
    fields = []
    for name, ftype, instruction in taxonomy["fields"]:
        seen.add(name)
        fields.append({"name": name, "type": ftype, "required": False, "instruction": instruction})

    for extra in sorted(extra_field_names or ()):
        if extra in seen:
            continue
        seen.add(extra)
        fields.append({"name": extra, "type": "string", "required": False, "instruction": "Seen on past documents of this type in this organization"})

    table = taxonomy.get("table")
    if table:
        fields.append({
            "name": table["name"], "type": "table", "required": False, "instruction": table["instruction"],
            "columns": list(table["columns"]),
        })

    return fields


def available_document_types() -> list[str]:
    return sorted(TAXONOMIES.keys())
