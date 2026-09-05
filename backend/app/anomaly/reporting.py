"""
reporting — real content builders for AnomalyReport (CSV/XLSX/JSON/PDF),
one incremental writer per format, driven by execution/anomaly_export_
executor.py in DB-side chunks — never the whole record set held in Python
memory at once, same bounded-memory discipline sql_export_writers.py
already established for SQL Workspace's background exports.

Content shape is deliberately different from a raw SQL export (which just
dumps query rows): a report is the ANOMALY DETECTION RESULT — job/
execution summary, every selected column's result (including PASS — see
models/anomaly.py's module docstring), and the individual anomalous
records. CSV/JSON/XLSX export ALL of it, chunk by chunk, no cap. PDF is
the one deliberate exception: a multi-hundred-page PDF listing millions
of raw rows isn't a usable document, so its record table is capped at
PDF_RECORD_CAP and — when the true count exceeds that — the PDF says so
explicitly ("showing first N of TOTAL") rather than silently truncating.
"""
from __future__ import annotations

import abc
import csv
import json
from pathlib import Path

from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PDF_RECORD_CAP = 500

RECORD_COLUMNS = [
    "column_name", "anomaly_type", "record_identifier", "value", "expected_range",
    "score", "raw_score", "severity", "detected_at",
]
COLUMN_RESULT_COLUMNS = ["column_name", "data_type", "records", "anomalies", "anomaly_rate", "severity", "status"]


def _summary_dict(execution, job) -> dict:
    return {
        "job_name": job.name,
        "execution_id": execution.id,
        "status": execution.status.value if hasattr(execution.status, "value") else execution.status,
        "overall_status": execution.overall_status.value if execution.overall_status and hasattr(execution.overall_status, "value") else execution.overall_status,
        "trigger": execution.trigger,
        "records_scanned": execution.records_scanned,
        "columns_scanned": execution.columns_scanned,
        "anomalies_found": execution.anomalies_found,
        "critical_count": execution.critical_count,
        "warning_count": execution.warning_count,
        "profiling_ms": execution.profiling_ms,
        "detection_ms": execution.detection_ms,
        "total_ms": execution.total_ms,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
    }


def _record_row(record, column_name_by_result_id: dict) -> list:
    return [
        column_name_by_result_id.get(record.column_result_id, "?"),
        record.anomaly_type,
        record.record_identifier,
        record.value,
        record.expected_range,
        record.score,
        record.raw_score,
        record.severity.value if hasattr(record.severity, "value") else record.severity,
        record.detected_at.isoformat() if record.detected_at else None,
    ]


class ReportWriter(abc.ABC):
    """One open file, written incrementally — write_summary()/write_column_
    results() are each called once; write_records() is called once per
    chunk as the executor streams them from the DB."""

    def __init__(self, path: Path):
        self.path = path

    @abc.abstractmethod
    def write_summary(self, execution, job) -> None: ...

    @abc.abstractmethod
    def write_column_results(self, column_results: list) -> None: ...

    @abc.abstractmethod
    def write_records(self, records: list, column_name_by_result_id: dict) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class CsvReportWriter(ReportWriter):
    """Flat table of every anomalous record (the actionable part of a
    report) — job/execution identity is embedded as leading columns on
    every row rather than a separate section, since CSV has no notion of
    "multiple sections" the way XLSX sheets or a JSON object do."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._header_written = False
        self._job_name = self._execution_id = None

    def write_summary(self, execution, job) -> None:
        self._job_name, self._execution_id = job.name, execution.id

    def write_column_results(self, column_results: list) -> None:
        pass  # CSV's single flat table is records-only — see class docstring

    def write_records(self, records: list, column_name_by_result_id: dict) -> None:
        if not self._header_written:
            self._writer.writerow(["job_name", "execution_id", *RECORD_COLUMNS])
            self._header_written = True
        for r in records:
            self._writer.writerow([self._job_name, self._execution_id, *_record_row(r, column_name_by_result_id)])

    def close(self) -> None:
        self._fh.close()


class JsonReportWriter(ReportWriter):
    """A real, complete structured document — {summary, column_results,
    records} — streamed the same way sql_export_writers.JsonChunkWriter
    streams a JSON array: '{' at open, each section appended as it's
    known, ']'/'}'  closed only once every chunk has actually arrived."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._fh = open(path, "w", encoding="utf-8")
        self._fh.write("{")
        self._first_record = True
        self._records_array_open = False

    def write_summary(self, execution, job) -> None:
        self._fh.write(f'"summary": {json.dumps(_summary_dict(execution, job), default=str)}, ')

    def write_column_results(self, column_results: list) -> None:
        rows = [
            {c: (getattr(cr, c).value if hasattr(getattr(cr, c), "value") else getattr(cr, c)) for c in COLUMN_RESULT_COLUMNS}
            for cr in column_results
        ]
        self._fh.write(f'"column_results": {json.dumps(rows, default=str)}, ')

    def write_records(self, records: list, column_name_by_result_id: dict) -> None:
        if not self._records_array_open:
            self._fh.write('"records": [')
            self._records_array_open = True
        for r in records:
            if not self._first_record:
                self._fh.write(",")
            self._first_record = False
            row = dict(zip(RECORD_COLUMNS, _record_row(r, column_name_by_result_id)))
            self._fh.write(json.dumps(row, default=str))

    def close(self) -> None:
        if not self._records_array_open:
            self._fh.write('"records": [')
        self._fh.write("]}")
        self._fh.close()


class XlsxReportWriter(ReportWriter):
    """Three sheets — Summary (key/value), Column Results (one row per
    selected column, incl. PASS), Records (every anomalous record) —
    genuinely the most human-browsable of the four formats, which is why
    it's the one this codebase's other spreadsheet exports (Rejected
    Records, Result Grid) also default to."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._wb = Workbook(write_only=True)
        self._records_ws = None

    def write_summary(self, execution, job) -> None:
        ws = self._wb.create_sheet("Summary")
        ws.append(["Field", "Value"])
        for k, v in _summary_dict(execution, job).items():
            ws.append([k, v])

    def write_column_results(self, column_results: list) -> None:
        ws = self._wb.create_sheet("Column Results")
        ws.append(COLUMN_RESULT_COLUMNS)
        for cr in column_results:
            ws.append([
                cr.column_name, cr.data_type, cr.records, cr.anomalies, cr.anomaly_rate,
                cr.severity.value if cr.severity else None,
                cr.status.value if hasattr(cr.status, "value") else cr.status,
            ])

    def write_records(self, records: list, column_name_by_result_id: dict) -> None:
        if self._records_ws is None:
            self._records_ws = self._wb.create_sheet("Records")
            self._records_ws.append(RECORD_COLUMNS)
        for r in records:
            self._records_ws.append(_record_row(r, column_name_by_result_id))

    def close(self) -> None:
        if self._records_ws is None:  # an execution with zero anomalies still gets a (header-only) Records sheet
            self._records_ws = self._wb.create_sheet("Records")
            self._records_ws.append(RECORD_COLUMNS)
        self._wb.save(self.path)


class PdfReportWriter(ReportWriter):
    """The one capped format — see module docstring. Buffers records up
    to PDF_RECORD_CAP (a bounded, small amount of memory — 500 rows of
    short strings, nothing like the unbounded CSV/JSON/XLSX streams) and
    renders everything in one shot at close(), since reportlab's
    SimpleDocTemplate builds the whole flowable list before laying out
    pages anyway — there's no meaningful "incremental" PDF write the way
    there is for the other three formats."""

    def __init__(self, path: Path):
        super().__init__(path)
        self._execution = self._job = None
        self._column_results: list = []
        self._records: list = []
        self._total_records = 0

    def write_summary(self, execution, job) -> None:
        self._execution, self._job = execution, job

    def write_column_results(self, column_results: list) -> None:
        self._column_results = column_results

    def write_records(self, records: list, column_name_by_result_id: dict) -> None:
        self._total_records += len(records)
        remaining = PDF_RECORD_CAP - len(self._records)
        if remaining > 0:
            self._records.extend(_record_row(r, column_name_by_result_id) for r in records[:remaining])

    def close(self) -> None:
        doc = SimpleDocTemplate(str(self.path), pagesize=letter)
        styles = getSampleStyleSheet()
        story = [Paragraph(f"Anomaly Detection Report — {self._job.name}", styles["Title"]), Spacer(1, 0.2 * inch)]

        summary = _summary_dict(self._execution, self._job)
        summary_rows = [[k, str(v)] for k, v in summary.items()]
        story.append(Paragraph("Execution Summary", styles["Heading2"]))
        story.append(_styled_table([["Field", "Value"], *summary_rows]))
        story.append(Spacer(1, 0.3 * inch))

        story.append(Paragraph("Column Results", styles["Heading2"]))
        col_rows = [COLUMN_RESULT_COLUMNS] + [
            [cr.column_name, cr.data_type, cr.records, cr.anomalies, f"{cr.anomaly_rate:.4f}",
             cr.severity.value if cr.severity else "-", cr.status.value if hasattr(cr.status, "value") else cr.status]
            for cr in self._column_results
        ]
        story.append(_styled_table(col_rows))
        story.append(Spacer(1, 0.3 * inch))

        title = "Anomalous Records"
        if self._total_records > len(self._records):
            title += f" (showing first {len(self._records)} of {self._total_records})"
        story.append(Paragraph(title, styles["Heading2"]))
        if self._records:
            rec_rows = [RECORD_COLUMNS] + [[str(v) if v is not None else "" for v in r] for r in self._records]
            story.append(_styled_table(rec_rows))
        else:
            story.append(Paragraph("No anomalies were found.", styles["Normal"]))

        doc.build(story)


def _styled_table(rows: list[list]) -> Table:
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


_WRITERS = {"CSV": CsvReportWriter, "JSON": JsonReportWriter, "XLSX": XlsxReportWriter, "PDF": PdfReportWriter}
_EXTENSIONS = {"CSV": "csv", "JSON": "json", "XLSX": "xlsx", "PDF": "pdf"}


def make_writer(fmt: str, path: Path) -> ReportWriter:
    cls = _WRITERS.get(fmt)
    if cls is None:
        raise ValueError(f"Unknown report format: {fmt}")
    return cls(path)


def extension_for(fmt: str) -> str:
    return _EXTENSIONS.get(fmt, "dat")
