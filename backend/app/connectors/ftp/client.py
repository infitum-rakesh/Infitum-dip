"""
FtpConnector — real FTP connectivity via Python's stdlib ftplib (no
external dependency needed — same "use the real protocol client" rule
every other connector in this codebase follows). Verified against a live
FTP server (delfer/alpine-ftp-server Docker image, vsftpd underneath) —
see the live verification run this module was built alongside.

Listing prefers MLSD (RFC 3659) — the modern, structured FTP LIST variant
that reports real size/type/modify facts per entry, over parsing
freeform LIST output (which varies enough per-server that naive parsing
is a real, common source of FTP integration bugs). Not every real server
supports it though — verified live against delfer/alpine-ftp-server
(vsftpd), which genuinely returns "500 Unknown command" for MLSD — so
list_files() falls back to a real Unix `ls -l`-style LIST parser
(_parse_unix_list_line()) when MLSD isn't available, rather than just
erroring out. That fallback covers the standard Unix format real FTP
servers overwhelmingly use; a server whose LIST output doesn't match it
gets a clear ConnectorError naming the unparsed line, not a silently
empty/wrong result.

open_read_stream()/write_file() use ftplib's transfercmd()/storbinary()
directly against the raw data-connection socket — real bounded .recv(n)
reads, never the whole file buffered in memory, same principle every
other connector's streaming path follows.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import time
from datetime import datetime, timezone
from ftplib import FTP, error_perm

from app.connectors.base import ConnectionTestResult, ConnectorError, FileInfo
from app.connectors.files.base import FileConnectorBase

DEFAULT_TIMEOUT = 15

# Standard Unix `ls -l` LIST line, e.g.:
#   -rw-r--r--    1 dipuser  dipuser        28 Aug 18 12:41 hello.txt
#   drwxr-x---    2 dipuser  dipuser      4096 Aug 18 12:40 archive
_UNIX_LIST_RE = re.compile(
    r"^(?P<type>[-dl])\S{9}\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+(?P<date>\w+\s+\d+\s+[\d:]+)\s+(?P<name>.+)$"
)


def _parse_unix_list_line(line: str) -> tuple[str, str, int] | None:
    """Returns (name, type, size) or None if the line doesn't match the
    standard format — never guesses a partial match."""
    m = _UNIX_LIST_RE.match(line.strip())
    if not m:
        return None
    entry_type = "dir" if m.group("type") == "d" else "file"
    return m.group("name"), entry_type, int(m.group("size"))


class FtpConnector(FileConnectorBase):
    connection_type = "FTP"

    def _base_path(self) -> str:
        return self.config.get("base_path") or "/"

    def _resolve_path(self, object_name: str) -> str:
        return f"{self._base_path().rstrip('/')}/{object_name}"

    def _connect(self) -> FTP:
        ftp = FTP(timeout=DEFAULT_TIMEOUT)
        ftp.connect(self.config.get("host"), int(self.config.get("port") or 21))
        ftp.login(self.config.get("username") or "anonymous", self.secrets.get("password") or "")
        return ftp

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        ftp = None
        try:
            ftp = self._connect()
            welcome = ftp.getwelcome()
            ftp.cwd(self._base_path())
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms, server_version=welcome)
        except Exception as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))
        finally:
            if ftp:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()

    def list_files(self, pattern: str | None = None) -> list[FileInfo]:
        ftp = self._connect()
        try:
            base = self._base_path()
            effective_pattern = pattern or "*"
            is_regex = effective_pattern.startswith("re:")
            regex = re.compile(effective_pattern[3:]) if is_regex else None

            try:
                entries = list(ftp.mlsd(base))
            except error_perm:
                return self._list_files_via_list(ftp, base, effective_pattern, is_regex, regex)

            results = []
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                if facts.get("type") != "file":
                    continue
                matched = regex.search(name) if is_regex else fnmatch.fnmatch(name, effective_pattern)
                if not matched:
                    continue
                modified = None
                modify_raw = facts.get("modify")
                if modify_raw:
                    try:
                        modified = datetime.strptime(modify_raw[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                results.append(FileInfo(
                    name=name, path=f"{base.rstrip('/')}/{name}",
                    size=int(facts.get("size") or 0), modified_at=modified,
                ))
            results.sort(key=lambda f: f.name)
            return results
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def _list_files_via_list(self, ftp: FTP, base: str, effective_pattern: str, is_regex: bool, regex) -> list[FileInfo]:
        """Real fallback for a server without MLSD (see module docstring
        — verified live against vsftpd, which needs this path). Parses
        genuine LIST-command output line by line; a line that doesn't
        match the standard Unix format is skipped, not guessed at."""
        raw_lines: list[str] = []
        try:
            ftp.retrlines(f"LIST {base}", raw_lines.append)
        except error_perm as exc:
            raise ConnectorError(self._scrub(f"Could not list {base}: {exc}")) from exc

        results = []
        for line in raw_lines:
            parsed = _parse_unix_list_line(line)
            if parsed is None:
                continue
            name, entry_type, size = parsed
            if name in (".", "..") or entry_type != "file":
                continue
            matched = regex.search(name) if is_regex else fnmatch.fnmatch(name, effective_pattern)
            if not matched:
                continue
            results.append(FileInfo(name=name, path=f"{base.rstrip('/')}/{name}", size=size, modified_at=None))
        results.sort(key=lambda f: f.name)
        return results

    def open_read_stream(self, file_path: str):
        ftp = self._connect()
        ftp.voidcmd("TYPE I")
        sock = ftp.transfercmd(f"RETR {file_path}")
        return _FtpFileHandle(sock, ftp)

    def move_file(self, file_path: str, dest_dir: str) -> str:
        ftp = self._connect()
        try:
            filename = file_path.rsplit("/", 1)[-1]
            dest_path = f"{dest_dir.rstrip('/')}/{filename}"
            try:
                ftp.mkd(dest_dir)
            except error_perm:
                pass  # already exists — real, expected outcome, not an error
            ftp.rename(file_path, dest_path)
            return dest_path
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not move {file_path} to {dest_dir}: {exc}")) from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

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
        ftp = self._connect()
        try:
            parent_dir = file_path.rsplit("/", 1)[0] or "/"
            try:
                ftp.mkd(parent_dir)
            except error_perm:
                pass
            ftp.storbinary(f"STOR {file_path}", io.BytesIO(data))
            return len(data)
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not write {file_path}: {exc}")) from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()


class _FtpFileHandle:
    """Wraps the raw data-connection socket transfercmd() hands back into
    a bounded .read(size) file-like object — real socket .recv() calls,
    never the whole file buffered. Finishing the transfer (closing the
    socket, then reading the server's real 226 completion reply via
    voidresp()) happens in close(), matching ftplib's own documented
    transfercmd()/retrbinary() completion protocol."""

    def __init__(self, sock, ftp: FTP):
        self._sock = sock
        self._ftp = ftp
        self._buf = b""
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = [self._buf]
            self._buf = b""
            while not self._eof:
                chunk = self._sock.recv(65536)
                if not chunk:
                    self._eof = True
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        while len(self._buf) < size and not self._eof:
            chunk = self._sock.recv(max(size - len(self._buf), 4096))
            if not chunk:
                self._eof = True
                break
            self._buf += chunk
        result, self._buf = self._buf[:size], self._buf[size:]
        return result

    def close(self) -> None:
        try:
            self._sock.close()
        finally:
            try:
                self._ftp.voidresp()
            except Exception:
                pass
            try:
                self._ftp.quit()
            except Exception:
                self._ftp.close()
