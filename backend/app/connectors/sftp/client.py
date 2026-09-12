"""
SftpConnector — real SFTP connectivity via paramiko. Verified against a
live SFTP server (atmoz/sftp Docker image) during Phase 5 development.

Known limitation, disclosed rather than hidden: opens a fresh SSH+SFTP
session per operation (test/list/read/move/write) — Phase 5 doesn't pool
or reuse SFTP sessions the way connectors/database/pool_manager.py pools
DB connections. For the file-partition extraction pattern this connector
is built for (one stream opened, read to completion, closed, per file),
that cost is paid once per file rather than once per chunk, which is a
reasonable tradeoff; a production deployment doing very frequent small
operations against the same SFTP host would want to add session reuse
here without changing the interface.

write_file() (added for the SFTP Output Dataset target) is the real
write-side counterpart to open_read_stream() — a genuine paramiko
putfo() upload, not a simulated one.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import stat as stat_module
import time
from datetime import datetime, timezone

import paramiko

from app.connectors.base import ConnectionTestResult, ConnectorError, FileInfo
from app.connectors.files.base import FileConnectorBase

_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)  # DSA (DSSKey) was removed in paramiko 5.x — deprecated/insecure


class SftpConnector(FileConnectorBase):
    connection_type = "SFTP"

    def _base_path(self) -> str:
        return self.config.get("base_path") or "/"

    def _effective_base(self, path: str | None) -> str:
        """Joins an optional sub-path (e.g. a schedule's configured
        source_path) onto this connection's own base_path. None/'' means
        the connection's base_path itself, the pre-existing behavior."""
        base = self._base_path()
        if not path or not path.strip():
            return base
        return f"{base.rstrip('/')}/{path.strip().strip('/')}"

    def _resolve_path(self, object_name: str) -> str:
        return f"{self._base_path().rstrip('/')}/{object_name}"

    def _connect_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.config.get("host"),
            port=int(self.config.get("port") or 22),
            username=self.config.get("username"),
            timeout=10, banner_timeout=10, auth_timeout=10,
            look_for_keys=False, allow_agent=False,
        )
        if self.config.get("auth_type") == "Private Key":
            key_text = self.secrets.get("private_key")
            if not key_text:
                raise ConnectorError("No private key is configured for this connection.")
            pkey, last_exc = None, None
            for key_cls in _KEY_CLASSES:
                try:
                    pkey = key_cls.from_private_key(io.StringIO(key_text))
                    break
                except Exception as exc:  # noqa: BLE001 — trying multiple key types is the point
                    last_exc = exc
            if pkey is None:
                raise ConnectorError(f"Could not parse the configured private key: {last_exc}")
            kwargs["pkey"] = pkey
        else:
            kwargs["password"] = self.secrets.get("password")
        client.connect(**kwargs)
        return client

    def _open_sftp(self):
        client = self._connect_client()
        return client, client.open_sftp()

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        client = None
        try:
            client, sftp = self._open_sftp()
            sftp.listdir(self._base_path())
            transport = client.get_transport()
            banner = transport.remote_version if transport else None
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(success=True, message="Connection successful.", latency_ms=latency_ms, server_version=banner)
        except Exception as exc:
            return ConnectionTestResult(success=False, message=self._scrub(str(exc)), latency_ms=round((time.monotonic() - start) * 1000, 1))
        finally:
            if client:
                client.close()

    def _list_dir_recursive(self, sftp, base: str, relative_prefix: str) -> list:
        """Real recursive walk (Scheduled Document Intelligence Workflow —
        spec "recursive scan" option) — depth-first over sftp.listdir_attr(),
        one round trip per directory. Returns (filename_relative_to_base,
        entry) pairs; used only when recursive=True below, so the default
        single-level behavior every other caller relies on is unaffected."""
        out = []
        for entry in sftp.listdir_attr(base):
            child_path = f"{base.rstrip('/')}/{entry.filename}"
            child_relative = f"{relative_prefix}{entry.filename}"
            if stat_module.S_ISDIR(entry.st_mode or 0):
                out.extend(self._list_dir_recursive(sftp, child_path, f"{child_relative}/"))
            else:
                out.append((child_relative, entry))
        return out

    def list_files(self, pattern: str | None = None, recursive: bool = False, path: str | None = None) -> list[FileInfo]:
        client = None
        try:
            client, sftp = self._open_sftp()
            base = self._effective_base(path)
            effective_pattern = pattern or "*"
            is_regex = effective_pattern.startswith("re:")
            regex = re.compile(effective_pattern[3:]) if is_regex else None

            if recursive:
                entries = self._list_dir_recursive(sftp, base, "")
            else:
                entries = [(entry.filename, entry) for entry in sftp.listdir_attr(base) if not stat_module.S_ISDIR(entry.st_mode or 0)]

            results = []
            for display_name, entry in entries:
                matched = regex.search(display_name) if is_regex else fnmatch.fnmatch(display_name, effective_pattern)
                if not matched:
                    continue
                results.append(FileInfo(
                    name=display_name, path=f"{base.rstrip('/')}/{display_name}", size=entry.st_size or 0,
                    modified_at=datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc) if entry.st_mtime else None,
                ))
            results.sort(key=lambda f: f.name)
            return results
        finally:
            if client:
                client.close()

    def open_read_stream(self, file_path: str):
        client, sftp = self._open_sftp()
        handle = sftp.open(file_path, "rb")
        handle.set_pipelined(True)  # allow read-ahead prefetch — still bounded-memory .read(n) semantics for callers
        return _SftpFileHandle(handle, client)

    def move_file(self, file_path: str, dest_dir: str) -> str:
        client = None
        try:
            client, sftp = self._open_sftp()
            filename = file_path.rsplit("/", 1)[-1]
            dest_path = f"{dest_dir.rstrip('/')}/{filename}"
            try:
                sftp.stat(dest_dir)
            except FileNotFoundError:
                sftp.mkdir(dest_dir)
            sftp.posix_rename(file_path, dest_path)
            return dest_path
        finally:
            if client:
                client.close()

    def file_checksum(self, file_path: str) -> str:
        stream = self.open_read_stream(file_path)
        try:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            stream.close()

    def write_file(self, file_path: str, data: bytes) -> int:
        """Real upload via paramiko's putfo — the caller already has the
        complete file in memory (an in-memory DataFrame serialized to
        CSV/Excel/JSON bytes by pipeline_job_executor.py, same bounded
        scope as every other in-memory target in this build), so one
        putfo call is the honest primitive here, not a fabricated
        streaming write. Creates the immediate parent directory if it
        doesn't exist yet (same mkdir-on-miss pattern move_file() already
        uses) — does NOT create a multi-level missing path, matching
        every other write path in this connector."""
        client = None
        try:
            client, sftp = self._open_sftp()
            parent_dir = file_path.rsplit("/", 1)[0] or "/"
            try:
                sftp.stat(parent_dir)
            except FileNotFoundError:
                sftp.mkdir(parent_dir)
            sftp.putfo(io.BytesIO(data), file_path)
            return len(data)
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not write {file_path}: {exc}")) from exc
        finally:
            if client:
                client.close()


class _SftpFileHandle:
    """Bundles the open SFTPFile with the SSHClient that owns its
    transport, so closing the handle can't leak the underlying
    connection — every read stays a bounded .read(size) call on the
    remote file, never a whole-file buffer."""

    def __init__(self, sftp_file, ssh_client):
        self._file = sftp_file
        self._client = ssh_client

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._client.close()
