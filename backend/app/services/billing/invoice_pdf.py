"""
Invoice PDF generation (spec section 16). Real reportlab-generated PDFs —
company/seller branding (logo + digital signatures), customer billing
address, line items, subtotal/discount/tax/total, payment status/
instructions. No mocked layout — every field comes straight from the real
Invoice/InvoiceLineItem/Organization/Seller/Company rows passed in.

Company (models/company.py) is the entity whose logo/address/signature
actually brand the invoice — a Seller can register more than one Company,
and each customer Organization is assigned to exactly one
(Organization.company_id) that decides which one applies here. `company`
is optional (an org with no company assigned yet falls back to the
platform Seller's own name/branding) so this never breaks for
pre-existing data.
"""
import base64
import io

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER
except ImportError:  # pragma: no cover
    colors = letter = inch = ImageReader = SimpleDocTemplate = Table = TableStyle = Paragraph = Spacer = getSampleStyleSheet = ParagraphStyle = TA_RIGHT = TA_CENTER = None
    Image = object

from app.models.billing.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.company import Company
from app.models.organization import Organization
from app.models.seller import Seller

_APP_NAME = "Data Intelligence Platform"

_STATUS_LABEL = {
    InvoiceStatus.DRAFT: "Draft",
    InvoiceStatus.ISSUED: "Issued",
    InvoiceStatus.PAYMENT_PENDING: "Payment Pending",
    InvoiceStatus.PAID: "Paid",
    InvoiceStatus.PARTIALLY_PAID: "Partially Paid",
    InvoiceStatus.OVERDUE: "Overdue",
    InvoiceStatus.VOID: "Void",
    InvoiceStatus.CANCELLED: "Cancelled",
    InvoiceStatus.NO_PAYMENT_REQUIRED: "No Payment Required",
}


def _money(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.2f}"


def _decode_image(data_uri: str | None, *, max_width: float, max_height: float) -> Image | None:
    """Decodes a base64 (optionally data:-prefixed) image string into a
    reportlab Image flowable, scaled to fit within max_width/max_height
    while preserving aspect ratio. Returns None on anything malformed
    (missing/corrupt upload) rather than failing the whole PDF — a bad
    logo/signature should never block downloading a real invoice."""
    if not data_uri:
        return None
    try:
        raw = data_uri.split(",", 1)[1] if data_uri.startswith("data:") else data_uri
        image_bytes = base64.b64decode(raw)
        reader = ImageReader(io.BytesIO(image_bytes))
        natural_width, natural_height = reader.getSize()
        if not natural_width or not natural_height:
            return None
        scale = min(max_width / natural_width, max_height / natural_height, 1.0)
        return Image(io.BytesIO(image_bytes), width=natural_width * scale, height=natural_height * scale)
    except Exception:
        return None


def _address_lines(*, address_line, city, branch, postal_code, country) -> str:
    """Joins whichever billing-address parts are actually set into
    "123 Main St, Springfield, 62704" style lines — never renders an
    empty "Bill To" sub-line for a field nobody filled in."""
    line1 = address_line
    line2 = ", ".join(part for part in (city, postal_code) if part)
    parts = [p for p in (line1, line2, branch, country) if p]
    return "<br/>".join(parts)


def generate_invoice_pdf(
    invoice: Invoice, line_items: list[InvoiceLineItem], organization: Organization,
    seller: Seller | None, company: Company | None = None,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    right_style = ParagraphStyle("right", parent=styles["Normal"], alignment=TA_RIGHT)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    center_small = ParagraphStyle("center_small", parent=styles["Normal"], fontSize=8, alignment=TA_CENTER, textColor=colors.grey)
    sig_caption = ParagraphStyle("sig_caption", parent=styles["Normal"], fontSize=9, alignment=TA_CENTER, spaceBefore=4)

    elements = []

    # Header: whichever entity this invoice is actually issued under —
    # the org's assigned Company if it has one, else the platform Seller —
    # never a mix of the two's identity fields.
    brand_name = company.name if company else (seller.name if seller else "Seller")
    brand_legal = (company.legal_name if company and company.legal_name else None) \
        or (seller.legal_name if seller and seller.legal_name else None) or brand_name
    brand_email = (company.email if company else None) or (seller.billing_email if seller else None) or (seller.support_email if seller else None)
    brand_phone = company.phone if company else None
    seller_logo_url = (seller.branding or {}).get("logo_url") if seller else None
    logo_source = (company.logo if company else None) or seller_logo_url

    brand_text = f"<b>{brand_name}</b><br/>{_APP_NAME}<br/>{brand_legal}"
    if brand_email:
        brand_text += f"<br/>{brand_email}"
    if brand_phone:
        brand_text += f"<br/>{brand_phone}"

    logo_image = _decode_image(logo_source, max_width=1.3 * inch, max_height=0.7 * inch)
    brand_cell = [logo_image, Spacer(1, 4)] if logo_image else []
    brand_cell.append(Paragraph(brand_text, styles["Normal"]))

    header_data = [[brand_cell, Paragraph(f"<b>INVOICE</b><br/>{invoice.invoice_number}", right_style)]]
    header_table = Table(header_data, colWidths=[3.6 * inch, 3.4 * inch])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.3 * inch))

    address_block = _address_lines(
        address_line=organization.address_line, city=organization.city,
        branch=organization.branch, postal_code=organization.postal_code, country=organization.country,
    )
    bill_to = (
        f"<b>Bill To</b><br/>{organization.legal_name or organization.name}"
        + (f"<br/>{address_block}" if address_block else "")
        + (f"<br/>{organization.primary_contact_name}" if organization.primary_contact_name else "")
        + (f"<br/>{organization.primary_contact_email}" if organization.primary_contact_email else "")
        + (f"<br/>{organization.primary_contact_phone}" if organization.primary_contact_phone else "")
    )
    meta = (
        f"<b>Issue Date:</b> {invoice.issue_date.strftime('%d %b %Y')}<br/>"
        f"<b>Due Date:</b> {invoice.due_date.strftime('%d %b %Y') if invoice.due_date else '—'}<br/>"
        f"<b>Subscription:</b> #{invoice.subscription_id or '—'}<br/>"
        f"<b>Billing Period:</b> "
        + (f"{invoice.billing_period_start.strftime('%d %b %Y')} – {invoice.billing_period_end.strftime('%d %b %Y')}"
           if invoice.billing_period_start and invoice.billing_period_end else "—")
    )
    info_table = Table([[Paragraph(bill_to, styles["Normal"]), Paragraph(meta, right_style)]], colWidths=[3.6 * inch, 3.4 * inch])
    info_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.3 * inch))

    line_data = [["Module / Description", "Qty", "Unit Price", "Total"]]
    for li in line_items:
        line_data.append([li.description, str(li.quantity), _money(li.unit_price, invoice.currency), _money(li.total, invoice.currency)])
    line_table = Table(line_data, colWidths=[3.4 * inch, 0.6 * inch, 1.5 * inch, 1.5 * inch])
    line_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(line_table)
    elements.append(Spacer(1, 0.2 * inch))

    totals_data = [
        ["Subtotal", _money(invoice.subtotal, invoice.currency)],
        ["Discount", _money(-invoice.discount, invoice.currency) if invoice.discount else _money(0, invoice.currency)],
        ["Tax", _money(invoice.tax, invoice.currency)],
        ["Total", _money(invoice.total, invoice.currency)],
    ]
    totals_table = Table(totals_data, colWidths=[5.4 * inch, 1.6 * inch])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 0.4 * inch))

    status_label = _STATUS_LABEL.get(invoice.status, invoice.status.value)
    payment_line = f"<b>Payment Status:</b> {status_label}"
    if invoice.status == InvoiceStatus.NO_PAYMENT_REQUIRED:
        instructions = "This is a trial subscription. No payment is required."
    elif invoice.status == InvoiceStatus.PAID:
        instructions = f"Paid on {invoice.paid_at.strftime('%d %b %Y') if invoice.paid_at else '—'}. Thank you."
    else:
        instructions = "Please use the payment link provided to complete this payment, or contact your Sales team."
    elements.append(Paragraph(payment_line, styles["Normal"]))
    elements.append(Paragraph(instructions, small))

    # Signatures — the platform Seller's own (always shown) and the
    # issuing Company's own, if this invoice actually has one assigned
    # (skipped entirely, not duplicated, when there's no Company — an org
    # with no Company yet would otherwise get the Seller's name signed
    # twice under two different captions). A missing signature image
    # still reserves the caption/name line so the layout stays balanced.
    seller_sig = _decode_image(seller.digital_signature if seller else None, max_width=1.8 * inch, max_height=0.6 * inch)
    if seller or company:
        elements.append(Spacer(1, 0.5 * inch))
        seller_cell = [seller_sig] if seller_sig else [Spacer(1, 0.5 * inch)]
        seller_cell.append(Paragraph("_" * 30, center_small))
        seller_cell.append(Paragraph(f"Authorized Signatory<br/><b>{seller.name if seller else 'Seller'}</b>", sig_caption))

        if company:
            company_sig = _decode_image(company.digital_signature, max_width=1.8 * inch, max_height=0.6 * inch)
            company_cell = [company_sig] if company_sig else [Spacer(1, 0.5 * inch)]
            company_cell.append(Paragraph("_" * 30, center_small))
            company_cell.append(Paragraph(f"Authorized Signatory<br/><b>{company.name}</b>", sig_caption))
            sig_table = Table([[seller_cell, company_cell]], colWidths=[3.5 * inch, 3.5 * inch])
        else:
            sig_table = Table([[seller_cell]], colWidths=[7.0 * inch])
        sig_table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
        elements.append(sig_table)

    doc.build(elements)
    return buf.getvalue()
