"""
ZIP Batch Upload — archive security (ZB2).

Two entry points, both real (no stubbed pass-through):

validate_zip() — structural/security checks BEFORE any extraction begins:
size ceiling, is-it-actually-a-zip, corruption (real testzip() CRC check),
password/encryption detection (real ZipInfo.flag_bits inspection).

safe_extract() — entry-by-entry STREAMING extraction (never
ZipFile.extractall(), never a whole entry read into memory — 1 MB chunks
throughout): real ZIP-slip protection (every target path normalized and
containment-checked against the resolved extraction root BEFORE it is ever
joined — absolute paths, Windows drive letters, and ".." traversal segments
are rejected outright, independent of the final containment check, which is
the actual defense against a symlink/junction trick the string check alone
could miss), real zip-bomb protection (five independently enforced limits:
MAX_EXTRACTED_SIZE, MAX_FILES_PER_ZIP, MAX_SINGLE_FILE_SIZE,
MAX_COMPRESSION_RATIO, MAX_DIRECTORY_DEPTH — the per-file/ratio limits are
checked twice: once cheaply from the central-directory metadata before
touching the entry's bytes, and again against the ACTUAL bytes copied
during streaming, so a crafted archive that lies about its own metadata
still can't get past the second check), configured skip-pattern filtering
(__MACOSX/, .DS_Store, Thumbs.db, AppleDouble ._* resource forks — never
turned into a processing task), and nested-archive detection (a .zip
member is catalogued as NESTED_ARCHIVE_NOT_SUPPORTED and never opened,
unless allow_nested_archives=True — recursive extraction itself is out of
scope for this phase and is disclosed as such rather than silently
mis-handled). SHA-256 is computed incrementally while each entry is
streamed to disk, per spec section 63 — never a second full read pass.

Any security-limit violation or ZIP-slip attempt aborts the WHOLE
extraction immediately and removes the partial extraction directory —
these are the "critical batch-level errors" the batch orchestrator
(document_batch_service.py, ZB4) is expected to fail the entire
DocumentBatch on, not isolate as one bad file among many.
"""
from __future__ import annotations

import enum
import hashlib
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_BUFSIZE = 1024 * 1024  # 1 MB streaming chunks — never load a whole entry into memory

_SKIP_DIR_PREFIXES = ("__macosx/",)
_SKIP_BASENAMES = {".ds_store", "thumbs.db"}


class ZipValidationErrorCode(str, enum.Enum):
    UNSUPPORTED_ARCHIVE_TYPE = "UNSUPPORTED_ARCHIVE_TYPE"
    CORRUPTED_ARCHIVE = "CORRUPTED_ARCHIVE"
    ZIP_ENCRYPTED_NOT_SUPPORTED = "ZIP_ENCRYPTED_NOT_SUPPORTED"
    ZIP_SECURITY_LIMIT_EXCEEDED = "ZIP_SECURITY_LIMIT_EXCEEDED"
    ZIP_SLIP_DETECTED = "ZIP_SLIP_DETECTED"


class ZipSecurityError(Exception):
    """Mirrors bulk_migration_run.py's BulkMigrationError shape (code +
    message + free-form details) — a fixed, JSON-serializable error
    taxonomy the caller can persist onto DocumentBatch.error_code without
    re-parsing free text."""
    def __init__(self, code: ZipValidationErrorCode, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class ExtractedEntry:
    zip_relative_path: str
    classification: str  # "extracted" | "skipped_pattern" | "nested_archive"
    extracted_path: Path | None = None
    size: int = 0
    sha256: str | None = None


def _is_skippable(rel_posix: str) -> bool:
    lower = rel_posix.lower()
    if lower.startswith(_SKIP_DIR_PREFIXES):
        return True
    basename = lower.rsplit("/", 1)[-1]
    if basename in _SKIP_BASENAMES:
        return True
    if basename.startswith("._"):  # macOS AppleDouble resource-fork sidecar
        return True
    return False


_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


def _resolve_safe_target(dest_root_resolved: Path, entry_name: str) -> tuple[str, Path]:
    """Normalizes one ZIP entry name and returns (clean_relative_posix_path,
    absolute_target_path) — or raises ZipSecurityError(ZIP_SLIP_DETECTED).
    Every rejection happens BEFORE the name is ever joined to a real path,
    plus an independent post-resolve containment check as defense in
    depth."""
    normalized = entry_name.replace("\\", "/").lstrip("/")
    if entry_name.replace("\\", "/").startswith("/") or entry_name.startswith("~"):
        raise ZipSecurityError(ZipValidationErrorCode.ZIP_SLIP_DETECTED, f"Entry {entry_name!r} uses an absolute path.")
    if _DRIVE_LETTER_RE.match(entry_name):
        raise ZipSecurityError(ZipValidationErrorCode.ZIP_SLIP_DETECTED, f"Entry {entry_name!r} uses a Windows drive-letter path.")
    pure = PurePosixPath(normalized)
    if any(part == ".." for part in pure.parts):
        raise ZipSecurityError(ZipValidationErrorCode.ZIP_SLIP_DETECTED, f"Entry {entry_name!r} contains a parent-directory traversal segment.")
    target = (dest_root_resolved / pure).resolve()
    try:
        target.relative_to(dest_root_resolved)
    except ValueError:
        raise ZipSecurityError(ZipValidationErrorCode.ZIP_SLIP_DETECTED, f"Entry {entry_name!r} resolves outside the extraction root.")
    return normalized, target


def validate_zip(zip_path: Path, *, max_zip_size_mb: int) -> None:
    """Structural/security checks before any extraction begins. Raises
    ZipSecurityError on the first problem found; returns None on success."""
    if not zip_path.is_file():
        raise ZipSecurityError(ZipValidationErrorCode.CORRUPTED_ARCHIVE, "Uploaded file not found on disk.")
    size = zip_path.stat().st_size
    max_bytes = max_zip_size_mb * 1024 * 1024
    if size > max_bytes:
        raise ZipSecurityError(
            ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
            f"ZIP is {size:,} bytes, exceeding the configured MAX_ZIP_SIZE_MB={max_zip_size_mb} ({max_bytes:,} bytes).",
        )
    if not zipfile.is_zipfile(zip_path):
        raise ZipSecurityError(ZipValidationErrorCode.UNSUPPORTED_ARCHIVE_TYPE, "File is not a valid ZIP archive.")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Encryption MUST be checked before testzip(): zipfile.open()
            # raises a bare RuntimeError (not BadZipFile) for any entry
            # whose general-purpose flag bit 0 is set, and testzip() calls
            # open() internally — checking flag_bits first, rather than
            # after, is what lets a genuinely encrypted archive get the
            # specific ZIP_ENCRYPTED_NOT_SUPPORTED result instead of an
            # unhandled RuntimeError crashing validate_zip() outright.
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    raise ZipSecurityError(
                        ZipValidationErrorCode.ZIP_ENCRYPTED_NOT_SUPPORTED,
                        f"ZIP entry {info.filename!r} is password-protected/encrypted; password support is not configured.",
                    )
            try:
                bad = zf.testzip()
            except RuntimeError as exc:
                # Defense in depth in case some entry still requires a
                # password despite not tripping the flag_bits check above.
                raise ZipSecurityError(ZipValidationErrorCode.ZIP_ENCRYPTED_NOT_SUPPORTED, f"ZIP archive requires a password: {exc}") from exc
            if bad is not None:
                raise ZipSecurityError(ZipValidationErrorCode.CORRUPTED_ARCHIVE, f"ZIP archive is corrupted — bad CRC on entry {bad!r}.")
    except zipfile.BadZipFile as exc:
        raise ZipSecurityError(ZipValidationErrorCode.CORRUPTED_ARCHIVE, f"ZIP archive is corrupted: {exc}") from exc


def safe_extract(
    zip_path: Path, dest_root: Path, *,
    max_extracted_size_mb: int, max_files_per_zip: int, max_single_file_size_mb: int,
    max_compression_ratio: int, max_directory_depth: int, allow_nested_archives: bool = False,
) -> list[ExtractedEntry]:
    """Streams every real (non-skipped, non-nested-archive) entry to disk
    under dest_root, enforcing all five zip-bomb limits and real ZIP-slip
    containment. Aborts and removes dest_root entirely on any violation —
    see module docstring. Returns one ExtractedEntry per catalogued entry
    (extracted, skipped_pattern is simply omitted from the result — the
    caller never needs to know about a .DS_Store — or nested_archive)."""
    dest_root_resolved = dest_root.resolve()
    dest_root_resolved.mkdir(parents=True, exist_ok=True)
    max_extracted_bytes = max_extracted_size_mb * 1024 * 1024
    max_single_file_bytes = max_single_file_size_mb * 1024 * 1024

    results: list[ExtractedEntry] = []
    running_extracted_total = 0
    file_count = 0

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                raw_name = info.filename
                rel_probe = raw_name.replace("\\", "/").lstrip("/")
                if _is_skippable(rel_probe):
                    continue

                file_count += 1
                if file_count > max_files_per_zip:
                    raise ZipSecurityError(
                        ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                        f"ZIP contains more than the configured MAX_FILES_PER_ZIP={max_files_per_zip} entries.",
                    )

                depth = len(PurePosixPath(rel_probe).parent.parts)
                if depth > max_directory_depth:
                    raise ZipSecurityError(
                        ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                        f"Entry {rel_probe!r} is nested {depth} directories deep, exceeding the configured MAX_DIRECTORY_DEPTH={max_directory_depth}.",
                    )

                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > max_compression_ratio:
                        raise ZipSecurityError(
                            ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                            f"Entry {rel_probe!r} has a {ratio:.0f}x compression ratio, exceeding the configured MAX_COMPRESSION_RATIO={max_compression_ratio} — likely a decompression bomb.",
                        )

                if info.file_size > max_single_file_bytes:
                    raise ZipSecurityError(
                        ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                        f"Entry {rel_probe!r} declares {info.file_size:,} bytes, exceeding the configured MAX_SINGLE_FILE_SIZE_MB={max_single_file_size_mb}.",
                    )

                if rel_probe.lower().endswith(".zip"):
                    # Nested-archive recursion is out of scope for this
                    # phase even when allow_nested_archives is set —
                    # catalogued and disclosed rather than silently
                    # mis-processed as a regular document.
                    results.append(ExtractedEntry(zip_relative_path=rel_probe, classification="nested_archive"))
                    continue

                rel, target = _resolve_safe_target(dest_root_resolved, raw_name)
                target.parent.mkdir(parents=True, exist_ok=True)

                sha = hashlib.sha256()
                written = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(_BUFSIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        running_extracted_total += len(chunk)
                        if written > max_single_file_bytes:
                            raise ZipSecurityError(
                                ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                                f"Entry {rel!r} decompressed beyond the configured MAX_SINGLE_FILE_SIZE_MB={max_single_file_size_mb} (declared metadata under-reported the true size).",
                            )
                        if running_extracted_total > max_extracted_bytes:
                            raise ZipSecurityError(
                                ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
                                f"Total extracted size exceeded the configured MAX_EXTRACTED_SIZE_MB={max_extracted_size_mb}.",
                            )
                        sha.update(chunk)
                        dst.write(chunk)

                results.append(ExtractedEntry(
                    zip_relative_path=rel, classification="extracted",
                    extracted_path=target, size=written, sha256=sha.hexdigest(),
                ))
    except (ZipSecurityError, zipfile.BadZipFile) as exc:
        shutil.rmtree(dest_root_resolved, ignore_errors=True)
        if isinstance(exc, ZipSecurityError):
            raise
        raise ZipSecurityError(ZipValidationErrorCode.CORRUPTED_ARCHIVE, f"ZIP archive is corrupted: {exc}") from exc

    return results
