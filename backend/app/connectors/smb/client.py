"""
NetworkFileConnector — real SMB/CIFS connectivity to a Windows-style
network share via smbprotocol's high-level `smbclient` module (a pure-
Python SMB2/3 implementation — no system-level Samba client tooling
needed). Verified against a live Samba server (dperson/samba Docker
image) during development.

Config shape (see connection_service.py's NETWORK_FILE field schema):
`unc_path` (e.g. "\\\\server\\share\\optional\\subdir"), `username`,
`password`, optional `port` (default 445 — the real SMB port; only
non-default for things like local Docker testing, where the container's
445 is remapped). _split_unc() below parses unc_path into (server,
share, base_path) once; every operation addresses files as
"\\\\server\\share\\base_path\\<name>", matching smbclient's own path
convention exactly (it accepts forward or back slashes, but this uses
backslashes throughout to match how a real UNC path is normally written
and how Windows/Samba server logs report them).

Real, live-verified gotcha this module works around: smbclient's
register_session() alone is NOT enough when using a non-default port —
every high-level path function (scandir/open_file/stat/rename/makedirs)
independently resolves its own session via the server name parsed out of
the path, silently defaulting to port 445 unless `port=` is passed to
THAT SAME CALL too. _kwargs() below is threaded through every one of
those calls for exactly this reason — confirmed live: omitting it made
scandir() try (and fail) to reach port 445 even though register_session()
had already been called with the real (non-default) port.

open_read_stream()/write_file() use smbclient.open_file() in binary
mode — a real streaming SMB file handle (.read(size)/.write(data)),
never the whole file buffered beyond what the caller itself chooses to
read at once, same bounded-memory principle every other connector here
follows.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import time
from datetime import datetime, timezone

import smbclient
from smbprotocol.exceptions import SMBException

from app.connectors.base import ConnectionTestResult, ConnectorError, ConnectorValidationError, FileInfo
from app.connectors.files.base import FileConnectorBase


def _split_unc(unc_path: str) -> tuple[str, str, str]:
    """"\\\\server\\share\\a\\b" -> ("server", "share", "a\\b")."""
    normalized = unc_path.replace("/", "\\").strip("\\")
    parts = normalized.split("\\")
    if len(parts) < 2:
        raise ConnectorValidationError(f"'{unc_path}' isn't a valid UNC path — expected \\\\server\\share[\\subdir].")
    server, share = parts[0], parts[1]
    base = "\\".join(parts[2:])
    return server, share, base


class NetworkFileConnector(FileConnectorBase):
    connection_type = "NETWORK_FILE"

    def _server_share_base(self) -> tuple[str, str, str]:
        unc = (self.config.get("unc_path") or "").strip()
        if not unc:
            raise ConnectorValidationError("No UNC path configured for this Network File connection.")
        return _split_unc(unc)

    def _kwargs(self) -> dict:
        """Credentials + port, passed to EVERY smbclient call — see
        module docstring for why port specifically can't just be set
        once via register_session()."""
        return {
            "username": self.config.get("username") or None,
            "password": self.secrets.get("password") or None,
            "port": int(self.config.get("port") or 445),
            "connection_timeout": 10,
        }

    def _base_path(self) -> str:
        server, share, base = self._server_share_base()
        return f"\\\\{server}\\{share}" + (f"\\{base}" if base else "")

    def _resolve_path(self, object_name: str) -> str:
        return f"{self._base_path()}\\{object_name}"

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            list(smbclient.scandir(self._base_path(), **self._kwargs()))
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (SMBException, OSError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def list_files(self, pattern: str | None = None) -> list[FileInfo]:
        base = self._base_path()
        effective_pattern = pattern or "*"
        is_regex = effective_pattern.startswith("re:")
        regex = re.compile(effective_pattern[3:]) if is_regex else None
        kwargs = self._kwargs()

        results = []
        try:
            for entry in smbclient.scandir(base, **kwargs):
                if entry.is_dir():
                    continue
                name = entry.name
                matched = regex.search(name) if is_regex else fnmatch.fnmatch(name, effective_pattern)
                if not matched:
                    continue
                # entry.stat() (SMBDirEntry's own bound method) doesn't
                # carry forward this connector's kwargs (notably `port`)
                # — confirmed live: it defaults to port 445 regardless of
                # what scandir() above was actually called with. Calling
                # smbclient.stat() directly with our own kwargs avoids
                # that and reuses the same already-open connection.
                stat = smbclient.stat(f"{base}\\{name}", **kwargs)
                results.append(FileInfo(
                    name=name, path=f"{base}\\{name}", size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) if stat.st_mtime else None,
                ))
        except (SMBException, OSError) as exc:
            raise ConnectorError(self._scrub(f"Could not list {base}: {exc}")) from exc
        results.sort(key=lambda f: f.name)
        return results

    def open_read_stream(self, file_path: str):
        return smbclient.open_file(file_path, mode="rb", **self._kwargs())

    def move_file(self, file_path: str, dest_dir: str) -> str:
        kwargs = self._kwargs()
        try:
            filename = file_path.rsplit("\\", 1)[-1]
            dest_path = f"{dest_dir.rstrip(chr(92))}\\{filename}"
            try:
                smbclient.stat(dest_dir, **kwargs)
            except (SMBException, OSError):
                smbclient.makedirs(dest_dir, exist_ok=True, **kwargs)
            smbclient.rename(file_path, dest_path, **kwargs)
            return dest_path
        except (SMBException, OSError) as exc:
            raise ConnectorError(self._scrub(f"Could not move {file_path} to {dest_dir}: {exc}")) from exc

    def file_checksum(self, file_path: str) -> str:
        digest = hashlib.sha256()
        with smbclient.open_file(file_path, mode="rb", **self._kwargs()) as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def write_file(self, file_path: str, data: bytes) -> int:
        kwargs = self._kwargs()
        try:
            parent_dir = file_path.rsplit("\\", 1)[0]
            try:
                smbclient.stat(parent_dir, **kwargs)
            except (SMBException, OSError):
                smbclient.makedirs(parent_dir, exist_ok=True, **kwargs)
            with smbclient.open_file(file_path, mode="wb", **kwargs) as f:
                f.write(data)
            return len(data)
        except (SMBException, OSError) as exc:
            raise ConnectorError(self._scrub(f"Could not write {file_path}: {exc}")) from exc
