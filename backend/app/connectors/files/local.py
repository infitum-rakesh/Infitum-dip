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

import fnmatch
import hashlib
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

    def list_files(self, pattern: str | None = None, recursive: bool = False) -> list[FileInfo]:
        base = self._base()
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
