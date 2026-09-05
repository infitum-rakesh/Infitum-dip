"""
FileConnectorBase — shared logic that adapts any file-oriented connector
(SftpConnector, LocalFileConnector) onto the SAME schema/object/column
browsing + preview interface Phase 2 already built for databases (see
connectors/database/base.py). No new API routes were needed for file
sources — GET /connections/{id}/objects, /objects/{name}/columns and
POST /preview just work once a connector here implements
list_files/open_read_stream and this class's format dispatch.

"Object" for a file connector = one file matching a pattern. "Columns" =
the header row from a delimited/XLSX file, or the key set of a JSONL
file's first record — genuinely read via the streaming format readers in
connectors/files/, never guessed. Concrete subclasses only implement the
pure I/O primitives (test_connection, list_files, open_read_stream,
move_file, file_checksum, _resolve_path) — this class does all the
-> ObjectInfo / -> ColumnInfo / -> PreviewResult adaptation once.
"""
from __future__ import annotations

import ftplib
import itertools

from app.connectors.base import BaseConnector, ColumnInfo, ConnectorValidationError, ObjectInfo, PreviewResult
from app.connectors.files import registry as format_registry


class FileRowStream:
    """Bounded-memory chunked iterator over one file's rows — the FILE-
    mode analogue of connectors/database/base.py's _SqlAlchemySqlStream.
    execution/worker.py's run_file_partition() calls fetch_chunk(size) in
    a loop exactly the way run_query_partition() calls SqlStream's, so a
    file of any size is read in run.chunk_size-row batches, never fully
    materialized. Backed by the real reader.iter_rows() generator over
    the real open stream — itertools.islice just batches its output,
    it does no buffering of its own beyond that one batch."""

    def __init__(self, stream, row_iter):
        self._stream = stream
        self._row_iter = row_iter

    def fetch_chunk(self, size: int) -> list[dict]:
        return list(itertools.islice(self._row_iter, size))

    def close(self) -> None:
        self._stream.close()


def _open_stream_or_validation_error(connector: "FileConnectorBase", path: str, object_name: str):
    # open_read_stream() is real I/O (local disk, a remote SFTP round
    # trip, or an FTP RETR) and raises plain OSError/FileNotFoundError/
    # PermissionError (local/SFTP) or ftplib.all_errors (FTP — e.g. a 550
    # from the server) for a file that doesn't exist or isn't readable —
    # none of those are ConnectorUnsupportedError/ConnectorValidationError,
    # so left uncaught they'd escape as an unhandled 500 instead of the
    # clean 400 every other "bad object_name" case in this module
    # produces. Translated here, once, for every concrete file connector.
    try:
        return connector.open_read_stream(path)
    except ConnectorValidationError:
        raise
    except OSError as exc:
        raise ConnectorValidationError(f"Could not read '{object_name}': {exc.strerror or exc}.") from exc
    except ftplib.all_errors as exc:
        raise ConnectorValidationError(f"Could not read '{object_name}': {exc}.") from exc


class FileConnectorBase(BaseConnector):
    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        files = self.list_files(pattern=search)
        return [ObjectInfo(name=f.name, schema=None, object_type="FILE") for f in files]

    def get_columns(self, object_name: str, schema: str | None = None) -> list[ColumnInfo]:
        # Resolve (and sandbox-check) the path BEFORE anything else — a
        # traversal attempt must always fail for the same reason,
        # regardless of what extension it happens to end in, rather than
        # potentially reaching format detection or file I/O first.
        path = self._resolve_path(object_name)
        reader = format_registry.get_reader(object_name)
        stream = _open_stream_or_validation_error(self, path, object_name)
        try:
            header = reader.peek_header(stream)
        finally:
            stream.close()
        return [ColumnInfo(name=h or f"column_{i + 1}", data_type="TEXT", nullable=True) for i, h in enumerate(header)]

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        path = self._resolve_path(object_name)
        reader = format_registry.get_reader(object_name)
        stream = _open_stream_or_validation_error(self, path, object_name)
        try:
            columns: list[str] | None = None
            rows: list[list] = []
            truncated = False
            for i, row in enumerate(reader.iter_rows(stream)):
                if columns is None:
                    columns = list(row.keys())
                if i >= limit:
                    truncated = True
                    break
                rows.append([row.get(c) for c in columns])
            return PreviewResult(columns=columns or [], rows=rows, row_count=None, truncated=truncated)
        finally:
            stream.close()

    def row_count(self, object_name: str, schema: str | None = None) -> int | None:
        # Would mean fully streaming the file just to count rows — real if
        # implemented, but expensive, and not needed for the design-time
        # browsing this serves. Left unsupported (returns None, degrading
        # the preview UI gracefully) rather than silently doing a full
        # file scan behind an innocuous-looking GET request.
        return None

    def resolve_path(self, object_name: str) -> str:
        """Public wrapper around the connector-specific, sandbox-checked
        path resolution — used by execution/worker.py's file-partition
        path (to open a file for streaming, then to move it to an
        archive/error dir afterward) without reaching into the
        underscore-prefixed subclass hook directly."""
        return self._resolve_path(object_name)

    def open_row_stream(self, object_name: str) -> FileRowStream:
        """Real analogue of open_sql_stream() for FILE-mode extraction —
        same path-resolve-before-format-detection ordering, same
        OSError-to-ConnectorValidationError translation, as
        get_columns/preview_object above."""
        path = self._resolve_path(object_name)
        reader = format_registry.get_reader(object_name)
        stream = _open_stream_or_validation_error(self, path, object_name)
        return FileRowStream(stream, reader.iter_rows(stream))

    def _resolve_path(self, object_name: str) -> str:
        raise NotImplementedError
