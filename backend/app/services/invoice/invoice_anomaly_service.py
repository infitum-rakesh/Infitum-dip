"""
invoice_anomaly_service.py — INV23: wires the existing Anomaly Detection
engine into automated invoice processing. Real reuse, not a bespoke
parallel implementation: builds two small pandas DataFrames (this
organization's own real invoice history, and the one invoice being
evaluated), wraps each in the SAME DataFrameRowSource (anomaly/
dataframe_source.py) the Pipeline Builder's anomaly_detection_step node
already uses (see execution/pipeline_job_executor.py::_run_anomaly_
detection), and runs the SAME statistical detector (anomaly/detectors/
statistical.py's Z-SCORE, via anomaly/engine.py's own profile_columns()/
scoring machinery) — reference stats profiled from history alone, the
invoice's own value scanned separately against them, exactly like the
standalone module's own baseline-vs-current-run split. No separate
"invoice anomaly math."

Two checks reuse the existing Z-SCORE detector against real data:
  - AMOUNT — is this invoice's `total` a statistical outlier against the
    organization's own recent invoice history?
  - TAX_RATIO — same Z-SCORE detector, pointed at a derived `tax/subtotal`
    column, so "tax-ratio anomaly" is genuinely the existing statistical
    machinery applied to real computed data, not a new detector class.

One genuinely new, bespoke check the generic column-level detectors
can't express (it's a population-membership question, not a per-column
outlier question):
  - FIRST_TIME_VENDOR — this exact vendor has never previously invoiced
    this organization. A common real AP fraud/error control (e.g. an
    impersonated-vendor invoice) with no existing detector to reuse.

Cold start: fewer than MIN_HISTORY_ROWS prior invoices for this org ->
the statistical checks are skipped entirely (never fabricate a "normal
range" from too little data) — returns whatever real findings exist
(FIRST_TIME_VENDOR can still fire), never a false CRITICAL from noise.

Findings feed invoice_decision_service.decide()'s existing narrow post-
decision hook — see that module's own call site for exactly how a
CRITICAL finding downgrades an otherwise-STRAIGHT_THROUGH invoice to
REVIEW_REQUIRED and creates a real Review Queue item.
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.anomaly import scoring as anomaly_scoring
from app.anomaly.dataframe_source import DataFrameRowSource
from app.anomaly.detectors import statistical
from app.anomaly.profiler import profile_columns
from app.models.invoice_intelligence.core import ApInvoice

MIN_HISTORY_ROWS = 5  # need a real population before "outlier" means anything
Z_SCORE_THRESHOLD = 3.0  # same default the standalone Anomaly Detection module ships (DEFAULT_PARAMS["Z_SCORE"])
HISTORY_WINDOW = 200  # bounded read, same spirit as every RowSource scan cap in this codebase


def _tax_ratio(tax: float | None, subtotal: float | None) -> float | None:
    if tax is None or not subtotal:
        return None
    return round(tax / subtotal, 4)


async def evaluate_invoice_anomalies(db: AsyncSession, invoice: ApInvoice, organization_id: int | None) -> list[dict]:
    """Returns [{check, severity, message}] — empty when nothing anomalous
    was found (including cold start). severity is one of anomaly_scoring's
    own band labels (LOW/MEDIUM/HIGH/CRITICAL), the same vocabulary the
    standalone Anomaly Detection module uses everywhere else."""
    stmt = (
        select(ApInvoice)
        .where(ApInvoice.organization_id == organization_id, ApInvoice.id != invoice.id, ApInvoice.total.is_not(None))
        .order_by(ApInvoice.created_at.desc())
        .limit(HISTORY_WINDOW)
    )
    history = list((await db.execute(stmt)).scalars().all())
    findings: list[dict] = []

    # -- FIRST_TIME_VENDOR — bespoke; no existing detector expresses "have we ever seen this value before" --
    if invoice.vendor_name and len(history) >= MIN_HISTORY_ROWS:
        seen_vendor = any((h.vendor_name or "").strip().lower() == invoice.vendor_name.strip().lower() for h in history)
        if not seen_vendor:
            findings.append({
                "check": "FIRST_TIME_VENDOR", "severity": "MEDIUM",
                "message": f"'{invoice.vendor_name}' has never previously invoiced this organization.",
            })

    if len(history) < MIN_HISTORY_ROWS:
        return findings  # too little history for the statistical checks below to mean anything yet

    # Reference stats (mean/std_dev) come from HISTORY ONLY, scanned
    # separately from the invoice being tested — including the invoice's
    # own (possibly extreme) value in the population it's compared against
    # would let one outlier drag the mean/std enough to mask itself,
    # exactly the failure mode a real Z-score check must avoid.
    history_df = pd.DataFrame([{"id": h.id, "total": h.total, "tax_ratio": _tax_ratio(h.tax, h.subtotal)} for h in history])
    history_source = DataFrameRowSource(history_df, label=f"invoice history — org {organization_id}")
    current_df = pd.DataFrame([{"id": invoice.id, "total": invoice.total, "tax_ratio": _tax_ratio(invoice.tax, invoice.subtotal)}])
    current_source = DataFrameRowSource(current_df, label="invoice being evaluated")

    for column, label in (("total", "amount"), ("tax_ratio", "tax ratio")):
        if history_df[column].dropna().shape[0] < MIN_HISTORY_ROWS:
            continue  # too many missing values (e.g. no subtotal on most invoices) to trust this column
        stats = profile_columns(history_source, None, "invoice_anomaly_history", [{"name": column, "data_type": "float"}])
        col_stats = stats.get(column) or {}
        hits = statistical.detect_zscore(current_source, column, col_stats, id_column="id", threshold=Z_SCORE_THRESHOLD)
        hit = next((h for h in hits if h.record_identifier == str(invoice.id)), None)
        if hit is None:
            continue
        severity = anomaly_scoring.severity_for_score(anomaly_scoring.normalize_score(hit.raw_score))
        findings.append({
            "check": f"{column.upper()}_OUTLIER", "severity": severity.value,
            "message": f"This invoice's {label} ({hit.value}) is a statistical outlier vs. this organization's "
                       f"own recent invoice history (expected range {hit.expected_range}).",
        })

    return findings
