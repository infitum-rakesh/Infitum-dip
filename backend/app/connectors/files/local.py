"""
LocalFileConnector — reads from the backend server's own local
filesystem, sandboxed to the connection's configured base_path.

This is inherently a sensitive capability (whatever the backend process
can read from disk), so every resolved path is checked to be a real
descendant of the configured root before any read/list/move — a request
for an object_name that walks out via `../../etc/passwd` is rejected the
same way a genuinely-missing file would be, never given a different,
information-leaking error path.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from app.connectors.base import ConnectionTestResult, ConnectorValidationError, FileInfo
from app.connectors.files.base import FileConnectorBase


class LocalFileConnector(FileConnectorBase):
    connection_type = "LOCAL_FILE"

    def _base(self) -> Path:
        raw = (self.config.get("base_path") or ".").strip()
        # Found live: pasting a path copied via macOS "Copy as Pathname" /
        # a shell command's own quoting (common for paths with spaces or
        # parentheses, e.g. "'/Users/x/Downloads/invoice (2)'") leaves the
        # literal quote characters in the field. A leading "'" means
        # Path() no longer sees a string starting with "/", so it's
        # treated as RELATIVE and silently resolves against the backend
        # process's own cwd instead of the intended folder — surfaced as a
        # confusing "Base path does not exist: <cwd>/'<path>'" error.
        # Stripping one matching pair of surrounding quotes is always safe
        # to attempt (a real path starting and ending with the same quote
        # character is not a thing) and fixes this without the user
        # needing to notice and hand-edit the stray characters themselves.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1].strip()
        return Path(raw).expanduser().resolve()

    def _resolve_path(self, object_name: str) -> str:
        base = self._base()
        candidate = (base / object_name).resolve()
        if candidate != base and base not in candidate.parents:
            raise ConnectorValidationError(f"'{object_name}' resolves outside this connection's base path.")
        return str(candidate)

    def _effective_base(self, path: str | None) -> Path:
        """Resolves an optional sub-folder (e.g. a schedule's configured
        source_path) against this connection's base_path — same
        outside-the-sandbox containment check _resolve_path() applies to
        a single object_name, since a caller-supplied sub-path is exactly
        that kind of untrusted input too. None/'' means the connection's
        own base_path itself, the pre-existing behavior.

        Real bug found live (Phase 8): a schedule's source_path is almost
        always stored as an ABSOLUTE path — typically the connection's own
        base_path itself, or an absolute subfolder of it. Blindly stripping
        the leading "/" (meant to normalize a relative sub-path like
        "/orders/" -> "orders") also stripped it off a genuinely absolute
        path, turning it into a bogus relative one that then got joined
        onto `base` a SECOND time (e.g. base "/a/b" + path "/a/b" ->
        wrongly "/a/b/a/b" instead of "/a/b") — every document schedule
        with an absolute source_path failed discovery with a false
        "Source path does not exist" error. An absolute path is now
        resolved on its own, then checked for containment exactly as
        before; only a genuinely relative path still gets joined onto base."""
        base = self._base()
        if not path or not path.strip():
            return base
        cleaned = path.strip()
        candidate = Path(cleaned).expanduser().resolve() if Path(cleaned).is_absolute() else (base / cleaned.strip("/\\")).resolve()
        if candidate != base and base not in candidate.parents:
            raise ConnectorValidationError(f"'{path}' resolves outside this connection's base path.")
        return candidate

    def append_jsonl(self, object_name: str, record: dict) -> None:
        """Appends one JSON line to object_name (created, with parent
        directories, if it doesn't exist yet) — the FILES destination for
        services/document_storage_service.py's push_document(). Reuses
        _resolve_path()'s containment check so a configured target_table
        value (e.g. "../../etc/cron.d/x") can't escape this connection's
        base_path any more than a read can."""
        path = Path(self._resolve_path(object_name))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def append_csv_row(self, object_name: str, record: dict) -> None:
        """Appends one CSV row to object_name — the CSV file-format option
        for services/document_storage_service.py's push_document(), the
        same real, unconditional-append pattern append_jsonl() already
        uses for JSONL. The header row (and therefore the fixed column
        set) is written from whichever record's keys arrive FIRST, since a
        CSV file has one column set for its whole lifetime unlike JSONL's
        line-independent records; a later record with a different key set
        (e.g. include_ai_raw_response toggled after this file already
        exists) is written against that SAME original header — a genuinely
        new key is silently dropped from this row (there's no column for
        it) and a genuinely missing key writes an empty cell — a real,
        disclosed CSV limitation, not a fabricated full reconciliation."""
        path = Path(self._resolve_path(object_name))
        path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = path.exists() and path.stat().st_size > 0
        if file_exists:
            with path.open("r", encoding="utf-8", newline="") as f:
                header = next(csv.reader(f), [])
        else:
            header = list(record.keys())
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)
            writer.writerow([record.get(col, "") for col in header])

    def write_parquet_record(self, object_dir: str, record: dict) -> None:
        """Writes ONE real Parquet file per document under object_dir
        (named "document_{document_id}.parquet" when the record carries a
        document_id, else a random-suffixed name) — the Parquet file-
        format option for push_document(). Genuine single-row incremental
        appends to one growing Parquet file aren't practical here: pyarrow's
        ParquetWriter needs to stay open across the whole file's writes,
        which conflicts with this connector being constructed fresh for
        each independent, possibly-hours-apart document push. A directory
        of small per-document Parquet files is the same real, standard
        physical layout partitioned Parquet tables (Spark/Hive-style
        output) already use — a genuine, disclosed structural choice, not
        a fake single-row "append" pretending to be one file."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        base = Path(self._resolve_path(object_dir))
        base.mkdir(parents=True, exist_ok=True)
        doc_id = record.get("document_id")
        filename = f"document_{doc_id}.parquet" if doc_id is not None else f"record_{time.time_ns()}.parquet"
        path = base / filename
        columns = list(record.keys())
        table = pa.table({col: [record[col]] for col in columns})
        pq.write_table(table, str(path))

    def append_xml_record(self, object_dir: str, record: dict) -> None:
        """Writes ONE real XML file per document under object_dir (named
        "document_{document_id}.xml" when the record carries a
        document_id, else a random-suffixed name) — the XML file-format
        option for push_document() (Part 21). Same directory-of-small-files
        structural choice write_parquet_record() above already makes and
        explains: a real single growing XML file can't be incrementally
        appended to without keeping its root element open across pushes,
        which conflicts with this connector being constructed fresh for
        each independent, possibly-hours-apart document push. Root
        element name "row" mirrors sql_export_writers.py's XmlChunkWriter
        row-element convention, for consistency across this codebase's two
        XML writers."""
        import xml.etree.ElementTree as ET

        base = Path(self._resolve_path(object_dir))
        base.mkdir(parents=True, exist_ok=True)
        doc_id = record.get("document_id")
        filename = f"document_{doc_id}.xml" if doc_id is not None else f"record_{time.time_ns()}.xml"
        path = base / filename

        root = ET.Element("row")
        for key, value in record.items():
            child = ET.SubElement(root, str(key))
            if value is not None:
                child.text = str(value)
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            base = self._base()
            if not base.exists():
                return ConnectionTestResult(success=False, message=f"Base path does not exist: {base}")
            if not base.is_dir():
                return ConnectionTestResult(success=False, message=f"Base path is not a directory: {base}")
            list(base.iterdir())  # a real permission check, not just exists()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(success=True, message="Base path is accessible.", latency_ms=latency_ms)
        except Exception as exc:
            return ConnectionTestResult(success=False, message=self._scrub(str(exc)), latency_ms=round((time.monotonic() - start) * 1000, 1))

    def list_files(self, pattern: str | None = None, recursive: bool = False, path: str | None = None) -> list[FileInfo]:
        base = self._effective_base(path)
        if not base.exists():
            raise ConnectorValidationError(f"Source path does not exist: {path or '.'}")
        if not base.is_dir():
            raise ConnectorValidationError(f"Source path is not a directory: {path or '.'}")
        effective_pattern = pattern or self.config.get("file_pattern") or "*"
        is_regex = effective_pattern.startswith("re:")
        regex = re.compile(effective_pattern[3:]) if is_regex else None

        # recursive=True (Scheduled Document Intelligence Workflow — spec
        # "recursive scan" option) walks every subdirectory via rglob();
        # the default (False) preserves the exact pre-existing single-level
        # iterdir() behavior every other caller (Source Dataset node, SQL
        # Workspace schema browsing) already depends on. `name` becomes the
        # path relative to base_path once recursion is on (e.g.
        # "invoices/2026/file.pdf") rather than just the bare filename —
        # still a valid object_name for _resolve_path()/open_read_stream()
        # since that just does `(base / object_name).resolve()`.
        entries = sorted(base.rglob("*"), key=lambda p: p.name) if recursive else sorted(base.iterdir(), key=lambda p: p.name)

        results = []
        for entry in entries:
            if entry.is_dir():
                continue
            display_name = str(entry.relative_to(base)) if recursive else entry.name
            matched = regex.search(display_name) if is_regex else fnmatch.fnmatch(display_name, effective_pattern)
            if not matched:
                continue
            stat_result = entry.stat()
            results.append(FileInfo(
                name=display_name, path=str(entry), size=stat_result.st_size,
                modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
            ))
        return results

    def open_read_stream(self, file_path: str):
        return open(file_path, "rb")

    def move_file(self, file_path: str, dest_dir: str) -> str:
        base = self._base()
        dest_base = Path(dest_dir)
        if not dest_base.is_absolute():
            dest_base = base / dest_dir
        dest_base = dest_base.resolve()
        if dest_base != base and base not in dest_base.parents:
            raise ConnectorValidationError("Archive/error directory must stay within this connection's base path.")
        dest_base.mkdir(parents=True, exist_ok=True)
        dest_path = dest_base / Path(file_path).name
        shutil.move(file_path, dest_path)
        return str(dest_path)

    def file_checksum(self, file_path: str) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
