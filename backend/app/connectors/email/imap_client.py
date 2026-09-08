"""
ImapConnector — real email-inbox intake (PAY19), the third Scheduled
Document Intelligence Workflow source kind alongside FILE (SFTP/FTP/
Network File/Local File/cloud) and DATABASE (PAY18). Built entirely on
Python's stdlib `imaplib`/`email` — no new dependency, no new schedule_
service.py code path either: this connector implements the exact same
FileConnectorBase contract SftpConnector/FtpConnector/NetworkFileConnector
do (list_files/open_read_stream/move_file/file_checksum/test_connection),
so a Document Source schedule pointed at an EMAIL_IMAP connection runs
through the *unmodified* FILE-kind dispatch in schedule_service.py — the
same OCR/AI-extraction/confidence/Review-Queue pipeline every other file
source already uses.

Mapping IMAP's model onto "files":
  - One "file" = one attachment on one message that matches the schedule's
    file_pattern (glob or `re:`-prefixed regex, same convention every
    other FileConnectorBase subclass uses).
  - `object_name`/`FileInfo.path` encodes identity as
    "{uid}_{part_index}_{filename}" — uid is the message's IMAP UID
    (stable across sessions, unlike sequence numbers), part_index is a
    1-based counter over attachment parts within that message (so two
    same-named attachments on one message still get distinct identities).
    Split with `.split("_", 2)` (max 2 splits) so filenames containing
    underscores are preserved intact.
  - list_files() does a real IMAP SEARCH (default "UNSEEN" — configurable
    via the connection's `search_criteria` field) then a real FETCH
    RFC822 per matching message to enumerate attachment parts/sizes. This
    is the same "list first" cost every file connector already pays
    (SFTP's listdir_attr, FTP's LIST) — for IMAP the honest primitive is
    the full message fetch (BODYSTRUCTURE parsing without a body fetch is
    possible but adds real complexity for marginal benefit at typical
    invoice-inbox volumes; disclosed here rather than silently pretending
    a cheaper metadata-only listing exists).
  - open_read_stream() re-fetches that one message by UID and returns the
    matching part's decoded payload as an in-memory BytesIO. Real IMAP
    section-only fetches (`BODY[n]`) would avoid the second full-message
    round trip; not implemented in this phase — disclosed, not silently
    optimized around.

Disclosed limitation (real, not silently papered over): IMAP's atomic
unit is the MESSAGE, not the attachment. move_file() — used for the
schedule's "archive after success" step — moves the WHOLE source email to
the destination folder. If a single message has more than one attachment
matching the pattern, the first attachment to finish processing archives
the message out from under any other attachment of the same message still
being processed concurrently (its later open_read_stream()/move_file()
call will fail with "message not found" in the original mailbox). Fine
for the common one-invoice-per-email case; a schedule against inboxes
with routinely multi-attachment emails should either not enable "archive
after success" or set max_concurrency=1.

Live-verified (PAY19) against a real Docker greenmail-standalone instance
(SMTP+IMAP test server) — a real message with a real PDF attachment sent
via smtplib, then discovered/downloaded/archived by this connector
through the actual Scheduled Document Intelligence Workflow.
"""
from __future__ import annotations

import email
import fnmatch
import hashlib
import imaplib
import io
import re
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime

from app.connectors.base import ConnectionTestResult, ConnectorError, FileInfo
from app.connectors.files.base import FileConnectorBase


def _decode_header_value(value: str | None) -> str:
    """MIME-decodes an attachment filename that may carry RFC 2047
    encoded-word encoding (e.g. non-ASCII filenames) — falls back to the
    raw value for the (common) plain-ASCII case."""
    if not value:
        return value or ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _parse_email_date(raw: str | None):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


class ImapConnector(FileConnectorBase):
    connection_type = "EMAIL_IMAP"

    def _mailbox(self) -> str:
        return self.config.get("mailbox") or "INBOX"

    def _search_criteria(self) -> str:
        return self.config.get("search_criteria") or "UNSEEN"

    def _connect(self):
        host = self.config.get("host")
        port = int(self.config.get("port") or 993)
        use_ssl = self.config.get("use_ssl", True)
        client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
        client.login(self.config.get("username") or "", self.secrets.get("password") or "")
        return client

    def _resolve_path(self, object_name: str) -> str:
        # object_name already fully identifies the message+part (see
        # module docstring) — nothing to resolve against a base directory
        # the way SFTP/Local File do.
        return object_name

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        client = None
        try:
            client = self._connect()
            typ, data = client.select(self._mailbox(), readonly=True)
            if typ != "OK":
                raise ConnectorError(f"Could not select mailbox '{self._mailbox()}': {data}")
            message_count = int(data[0]) if data and data[0] else 0
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            welcome = client.welcome
            banner = welcome.decode(errors="replace") if isinstance(welcome, bytes) else str(welcome)
            return ConnectionTestResult(
                success=True, message=f"Connection successful — {message_count} message(s) in '{self._mailbox()}'.",
                latency_ms=latency_ms, server_version=banner,
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=self._scrub(str(exc)), latency_ms=round((time.monotonic() - start) * 1000, 1))
        finally:
            if client:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001 — best-effort cleanup, never masks the real result above
                    pass

    def _iter_attachment_parts(self, msg):
        """Yields (1-based part_index, filename, part) for every non-
        multipart part that carries a filename — the same definition of
        'attachment' list_files()/open_read_stream() both rely on, kept
        in one place so they can never disagree on part numbering."""
        idx = 0
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if not filename:
                continue
            idx += 1
            yield idx, _decode_header_value(filename), part

    def list_files(self, pattern: str | None = None, recursive: bool = False) -> list[FileInfo]:
        # `recursive` has no IMAP analogue (one mailbox, no folder
        # hierarchy concept in this phase) — accepted and ignored, same
        # as the two flat cloud-object connectors do for the same reason.
        client = None
        try:
            client = self._connect()
            typ, _ = client.select(self._mailbox(), readonly=True)
            if typ != "OK":
                raise ConnectorError(f"Could not select mailbox '{self._mailbox()}'.")
            typ, data = client.uid("search", None, self._search_criteria())
            if typ != "OK":
                raise ConnectorError(f"IMAP SEARCH failed for '{self._search_criteria()}': {data}")
            uids = data[0].split() if data and data[0] else []

            effective_pattern = pattern or "*"
            is_regex = effective_pattern.startswith("re:")
            regex = re.compile(effective_pattern[3:]) if is_regex else None

            results: list[FileInfo] = []
            for uid in uids:
                typ, msg_data = client.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                msg_date = _parse_email_date(msg.get("Date"))
                uid_str = uid.decode()
                for part_index, filename, part in self._iter_attachment_parts(msg):
                    matched = regex.search(filename) if is_regex else fnmatch.fnmatch(filename, effective_pattern)
                    if not matched:
                        continue
                    payload = part.get_payload(decode=True) or b""
                    results.append(FileInfo(
                        name=f"{uid_str}_{part_index}_{filename}",
                        path=f"{uid_str}_{part_index}_{filename}",
                        size=len(payload), modified_at=msg_date,
                    ))
            results.sort(key=lambda f: f.name)
            return results
        except imaplib.IMAP4.error as exc:
            raise ConnectorError(self._scrub(str(exc))) from exc
        finally:
            if client:
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    pass

    def open_read_stream(self, file_path: str):
        uid_str, part_str, filename = file_path.split("_", 2)
        part_index = int(part_str)
        client = self._connect()
        try:
            typ, _ = client.select(self._mailbox())
            if typ != "OK":
                raise ConnectorError(f"Could not select mailbox '{self._mailbox()}'.")
            typ, msg_data = client.uid("fetch", uid_str.encode(), "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                raise ConnectorError(f"Could not fetch message uid {uid_str} (it may have been moved/deleted).")
            msg = email.message_from_bytes(msg_data[0][1])
            for idx, found_name, part in self._iter_attachment_parts(msg):
                if idx == part_index:
                    if found_name != filename:
                        # The message changed shape since list_files() ran
                        # (another process modified it) — fail loudly
                        # rather than silently return the wrong attachment.
                        raise ConnectorError(f"Attachment part {part_index} of message {uid_str} no longer matches '{filename}'.")
                    payload = part.get_payload(decode=True) or b""
                    return io.BytesIO(payload)
            raise ConnectorError(f"Attachment part {part_index} not found in message {uid_str}.")
        except imaplib.IMAP4.error as exc:
            raise ConnectorError(self._scrub(str(exc))) from exc
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

    def move_file(self, file_path: str, dest_dir: str) -> str:
        # See module docstring — this moves the WHOLE source email
        # (IMAP's atomic unit), not just the one attachment file_path
        # names. dest_dir is a plain IMAP mailbox/folder name here, not a
        # filesystem path — the schedule's generic "archive_path" text
        # field is reused verbatim as that folder name.
        uid_str, _part_str, _filename = file_path.split("_", 2)
        client = self._connect()
        try:
            typ, _ = client.select(self._mailbox())
            if typ != "OK":
                raise ConnectorError(f"Could not select mailbox '{self._mailbox()}'.")
            try:
                client.create(dest_dir)
            except imaplib.IMAP4.error:
                pass  # already exists — CREATE on an existing mailbox is the common "not an error" case
            typ, data = client.uid("copy", uid_str.encode(), dest_dir)
            if typ != "OK":
                raise ConnectorError(f"Could not copy message uid {uid_str} to '{dest_dir}': {data}")
            client.uid("store", uid_str.encode(), "+FLAGS", "(\\Deleted)")
            client.expunge()
            return f"{dest_dir}/{uid_str}"
        except imaplib.IMAP4.error as exc:
            raise ConnectorError(self._scrub(str(exc))) from exc
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass

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
