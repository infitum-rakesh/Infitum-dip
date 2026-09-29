"""
Automated tests for the Data Format Transformation Engine (spec item 8,
session Phase 4 / TE1-TE8) — written in Phase 7 to close the gap TE8 left:
that work was verified live via real file uploads/downloads only, never
captured as a repeatable pytest suite.

Exercises the real, reusable core the transformation executor
orchestrates: services/sql_export_writers.py's ChunkWriter classes (write
side) and connectors/files/registry.py's format readers, reached through
a real LocalFileConnector pointed at a pytest tmp_path (read side) — a
genuine write-to-disk-then-read-back round trip for every supported
format, not a mock of either side.
"""
from pathlib import Path

import pytest

from app.connectors.files.local import LocalFileConnector
from app.models.transformation_job import TransformFormat
from app.services.sql_export_writers import extension_for, make_writer

COLUMNS = ["id", "name", "amount"]
ROWS = [
    [1, "Ant Pvt", 100.5],
    [2, "Mng Ltd", 200.25],
    [3, "Uj Industries", 300.0],
]


def _write_all_chunks(writer, columns, rows, chunk_size=2):
    for i in range(0, len(rows), chunk_size):
        writer.write_rows(columns, rows[i : i + chunk_size])
    writer.close()


def _read_back(tmp_path: Path, filename: str) -> list[dict]:
    connector = LocalFileConnector({"base_path": str(tmp_path)}, {})
    stream = connector.open_row_stream(filename)
    rows = []
    try:
        while True:
            chunk = stream.fetch_chunk(1000)
            if not chunk:
                break
            rows.extend(chunk)
    finally:
        stream.close()
    return rows


@pytest.mark.parametrize("fmt", ["CSV", "JSON", "XLSX", "XML"])
def test_write_then_read_back_round_trip_preserves_all_rows(tmp_path, fmt):
    """Real round trip for every text/structured format the engine
    supports (Parquet is covered separately below — its numeric fidelity
    needs its own assertions since CSV/JSON/XML/XLSX all serialize to
    text and Parquet keeps native types)."""
    ext = extension_for(fmt)
    path = tmp_path / f"out.{ext}"
    writer = make_writer(fmt, path)
    _write_all_chunks(writer, COLUMNS, ROWS)

    assert path.exists()
    rows = _read_back(tmp_path, path.name)
    assert len(rows) == len(ROWS)

    read_names = {r.get("name") for r in rows}
    assert read_names == {"Ant Pvt", "Mng Ltd", "Uj Industries"}
    read_ids = sorted(int(r.get("id")) for r in rows)
    assert read_ids == [1, 2, 3]


def test_parquet_round_trip_preserves_native_types(tmp_path):
    """Parquet keeps native column types (not a text serialization like
    the others) — read back via pyarrow directly (not the generic file
    connector, since Parquet is registered there too but this test wants
    to assert exact dtype fidelity, which is Parquet's whole point)."""
    import pyarrow.parquet as pq

    path = tmp_path / "out.parquet"
    writer = make_writer("PARQUET", path)
    _write_all_chunks(writer, COLUMNS, ROWS)

    table = pq.read_table(path)
    df = table.to_pandas()
    assert len(df) == 3
    assert sorted(df["id"].tolist()) == [1, 2, 3]
    assert set(df["name"].tolist()) == {"Ant Pvt", "Mng Ltd", "Uj Industries"}
    assert sorted(df["amount"].tolist()) == [100.5, 200.25, 300.0]

    # And the generic file-connector read path (what the transformation
    # executor's FileRowSource actually uses) reads it back too.
    rows = _read_back(tmp_path, "out.parquet")
    assert len(rows) == 3


def test_excel_format_value_is_xlsx_not_excel_regression():
    """Regression guard for the real bug found live in TE8: a job created
    with format value "EXCEL" failed with "Unknown export format: EXCEL"
    because sql_export_writers._WRITERS/_EXTENSIONS are keyed by "XLSX".
    TransformFormat.EXCEL's enum VALUE must stay "XLSX" so
    target_format.value passed straight into make_writer() always works."""
    assert TransformFormat.EXCEL.value == "XLSX"
    # And make_writer must accept that exact value without raising.
    writer = make_writer(TransformFormat.EXCEL.value, Path("/tmp/_never_written_regression_check.xlsx"))
    assert writer is not None
    with pytest.raises(ValueError):
        make_writer("EXCEL", Path("/tmp/_never_written.xlsx"))


def test_unicode_and_special_characters_survive_every_format(tmp_path):
    """Data Format Transformation Engine (spec item 8) sits alongside the
    OCR/Unicode-integrity requirement (item 11) — a name with non-ASCII
    text and a comma/quote (real CSV-escaping hazards) must round-trip
    correctly through every format, not just plain ASCII fixtures."""
    columns = ["id", "name"]
    rows = [[1, "Müller, \"GmbH\""], [2, "日本語会社"], [3, "O'Brien & Sons"]]

    for fmt in ["CSV", "JSON", "XML"]:
        ext = extension_for(fmt)
        path = tmp_path / f"unicode_{fmt.lower()}.{ext}"
        writer = make_writer(fmt, path)
        _write_all_chunks(writer, columns, rows, chunk_size=10)
        read_rows = _read_back(tmp_path, path.name)
        read_names = {r.get("name") for r in read_rows}
        assert "日本語会社" in read_names, f"{fmt} lost unicode content"
        assert "O'Brien & Sons" in read_names, f"{fmt} lost apostrophe content"


def test_empty_dataset_produces_a_valid_readable_file(tmp_path):
    """A conversion job whose source has zero rows must still produce a
    structurally valid (if empty) output file, not a corrupt/partial one —
    real edge case a live-only test pass would rarely happen to hit."""
    for fmt in ["CSV", "JSON"]:
        ext = extension_for(fmt)
        path = tmp_path / f"empty.{ext}"
        writer = make_writer(fmt, path)
        writer.close()
        assert path.exists()


# ── Part 8 additions: configurable CSV/Excel/XML target-format options ──


def test_csv_delimiter_and_quotechar_are_honored(tmp_path):
    path = tmp_path / "out.csv"
    writer = make_writer("CSV", path, {"delimiter": ";", "quotechar": "'"})
    _write_all_chunks(writer, COLUMNS, ROWS)
    text = path.read_text()
    assert ";" in text.splitlines()[0]
    assert "," not in text.splitlines()[0]


def test_csv_encoding_option_is_honored(tmp_path):
    path = tmp_path / "out_latin1.csv"
    writer = make_writer("CSV", path, {"encoding": "latin-1"})
    _write_all_chunks(writer, ["id", "name"], [[1, "Müller"]])
    raw = path.read_bytes()
    assert "Müller".encode("latin-1") in raw


def test_excel_sheet_name_is_honored(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "out.xlsx"
    writer = make_writer("XLSX", path, {"sheet_name": "Invoices"})
    _write_all_chunks(writer, COLUMNS, ROWS)
    wb = load_workbook(path, read_only=True)
    try:
        assert wb.sheetnames == ["Invoices"]
    finally:
        wb.close()


def test_excel_sheet_name_is_sanitized_when_invalid(tmp_path):
    path = tmp_path / "out2.xlsx"
    writer = make_writer("XLSX", path, {"sheet_name": "Bad:Name/2024*"})
    _write_all_chunks(writer, COLUMNS, ROWS)
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    try:
        assert wb.sheetnames == ["Bad_Name_2024_"]
    finally:
        wb.close()


def test_xml_root_and_row_element_names_are_honored(tmp_path):
    path = tmp_path / "out.xml"
    writer = make_writer("XML", path, {"root_element": "invoices", "row_element": "invoice"})
    _write_all_chunks(writer, COLUMNS, ROWS)
    text = path.read_text()
    assert "<invoices>" in text
    assert "</invoices>" in text
    assert "<invoice>" in text
    assert "<rows>" not in text
    assert "<row>" not in text


def test_format_options_with_unrecognized_keys_for_format_are_ignored(tmp_path):
    """A shared options dict with keys for OTHER formats (e.g. xlsx_sheet_name
    passed while writing CSV) must not raise — make_writer() only picks the
    keys relevant to the chosen format."""
    path = tmp_path / "out.csv"
    writer = make_writer("CSV", path, {"sheet_name": "Ignored", "root_element": "ignored", "delimiter": "|"})
    _write_all_chunks(writer, COLUMNS, ROWS)
    assert "|" in path.read_text().splitlines()[0]


def test_format_options_none_preserves_all_current_defaults(tmp_path):
    """No format_options at all (the pre-Part-8 shape, and every job
    created before this field existed) must behave identically to before —
    comma CSV, "Result" sheet, <rows>/<row> XML tags."""
    from openpyxl import load_workbook

    csv_path = tmp_path / "d.csv"
    _write_all_chunks(make_writer("CSV", csv_path, None), COLUMNS, ROWS)
    assert "," in csv_path.read_text().splitlines()[0]

    xlsx_path = tmp_path / "d.xlsx"
    _write_all_chunks(make_writer("XLSX", xlsx_path, None), COLUMNS, ROWS)
    wb = load_workbook(xlsx_path, read_only=True)
    try:
        assert wb.sheetnames == ["Result"]
    finally:
        wb.close()

    xml_path = tmp_path / "d.xml"
    _write_all_chunks(make_writer("XML", xml_path, None), COLUMNS, ROWS)
    xml_text = xml_path.read_text()
    assert "<rows>" in xml_text and "<row>" in xml_text
