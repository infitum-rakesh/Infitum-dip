"""
Automated tests — ZIP Batch Upload security (ZB2). Real zipfile.ZipFile
archives crafted on disk in each test (no mocking of zipfile or the code
under test), real filesystem extraction into pytest's tmp_path, following
this project's own "no fake implementation" discipline. No DB/async needed
— zip_security_service.py is pure filesystem + zipfile logic.

Covers the security-critical scenarios from the approved plan: ZIP-slip
(relative traversal, absolute path, Windows drive-letter), all five
zip-bomb limits (extracted-size, file-count, single-file-size,
compression-ratio, directory-depth), corrupted archive, encrypted archive,
nested archive, and configured skip-pattern filtering.
"""
import hashlib
import os
import zipfile

import pytest

from app.services.zip_security_service import (
    ExtractedEntry, ZipSecurityError, ZipValidationErrorCode, safe_extract, validate_zip,
)

_DEFAULT_LIMITS = dict(
    max_extracted_size_mb=100, max_files_per_zip=1000, max_single_file_size_mb=50,
    max_compression_ratio=100, max_directory_depth=20,
)


def _make_zip(path, entries: dict[str, bytes], *, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_valid_zip_extracts_with_correct_sha256(tmp_path):
    zip_path = tmp_path / "valid.zip"
    content = b"hello world, this is a real document body"
    _make_zip(zip_path, {"docs/report.pdf": content})

    validate_zip(zip_path, max_zip_size_mb=100)
    dest = tmp_path / "extracted"
    results = safe_extract(zip_path, dest, **_DEFAULT_LIMITS)

    assert len(results) == 1
    entry = results[0]
    assert entry.classification == "extracted"
    assert entry.zip_relative_path == "docs/report.pdf"
    assert entry.size == len(content)
    assert entry.sha256 == hashlib.sha256(content).hexdigest()
    assert entry.extracted_path.is_file()
    assert entry.extracted_path.read_bytes() == content
    assert str(entry.extracted_path).startswith(str(dest.resolve()))


def test_empty_zip_extracts_to_zero_entries(tmp_path):
    zip_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(zip_path, "w"):
        pass
    validate_zip(zip_path, max_zip_size_mb=100)
    results = safe_extract(zip_path, tmp_path / "extracted", **_DEFAULT_LIMITS)
    assert results == []


def test_zip_slip_relative_traversal_rejected_and_cleaned_up(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../../../etc/passwd", b"root:x:0:0::/root:/bin/bash")

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **_DEFAULT_LIMITS)
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SLIP_DETECTED
    # extraction root itself removed on abort — no partial state left behind
    assert not dest.exists()
    # and, decisively, nothing escaped onto the real filesystem outside tmp_path
    assert not (tmp_path.parent.parent.parent / "etc" / "passwd").exists()


def test_zip_slip_absolute_path_rejected(tmp_path):
    zip_path = tmp_path / "evil_abs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/etc/passwd", b"malicious absolute path entry")

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **_DEFAULT_LIMITS)
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SLIP_DETECTED
    assert not dest.exists()


def test_zip_slip_windows_drive_letter_rejected(tmp_path):
    zip_path = tmp_path / "evil_win.zip"
    # zipfile normalizes backslashes to "/" on some platforms when writing via
    # writestr with a plain string name, so inject the raw name via ZipInfo
    # to guarantee the literal "C:\..." bytes land in the archive.
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo(filename="C:\\Windows\\System32\\evil.dll")
        zf.writestr(info, b"MZ fake pe header")

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **_DEFAULT_LIMITS)
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SLIP_DETECTED
    assert not dest.exists()


def test_zip_bomb_compression_ratio_rejected(tmp_path):
    zip_path = tmp_path / "bomb_ratio.zip"
    # 5 MB of zeros compresses to a tiny fraction of that under DEFLATE —
    # a real, not simulated, high compression ratio.
    _make_zip(zip_path, {"bomb.bin": b"\x00" * (5 * 1024 * 1024)})

    with zipfile.ZipFile(zip_path) as zf:
        info = zf.infolist()[0]
        ratio = info.file_size / max(1, info.compress_size)
    assert ratio > 100, "test fixture didn't actually produce a high compression ratio"

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **{**_DEFAULT_LIMITS, "max_compression_ratio": 50})
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
    assert not dest.exists()


def test_max_files_per_zip_rejected(tmp_path):
    zip_path = tmp_path / "toomany.zip"
    _make_zip(zip_path, {f"file_{i}.txt": b"x" for i in range(5)})

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **{**_DEFAULT_LIMITS, "max_files_per_zip": 3})
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
    assert not dest.exists()


def test_max_directory_depth_rejected(tmp_path):
    zip_path = tmp_path / "toodeep.zip"
    _make_zip(zip_path, {"a/b/c/d/file.txt": b"deep content"})

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **{**_DEFAULT_LIMITS, "max_directory_depth": 2})
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
    assert not dest.exists()


def test_max_single_file_size_rejected(tmp_path):
    zip_path = tmp_path / "toobig.zip"
    # Incompressible random data so the ratio check doesn't also (correctly)
    # trigger first — isolates the single-file-size check specifically.
    _make_zip(zip_path, {"huge.bin": os.urandom(2 * 1024 * 1024)}, compression=zipfile.ZIP_STORED)

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **{**_DEFAULT_LIMITS, "max_single_file_size_mb": 1})
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
    assert not dest.exists()


def test_max_extracted_size_rejected(tmp_path):
    zip_path = tmp_path / "extracted_total_too_big.zip"
    _make_zip(
        zip_path,
        {"a.bin": os.urandom(2 * 1024 * 1024), "b.bin": os.urandom(2 * 1024 * 1024)},
        compression=zipfile.ZIP_STORED,
    )

    dest = tmp_path / "extracted"
    with pytest.raises(ZipSecurityError) as exc_info:
        safe_extract(zip_path, dest, **{**_DEFAULT_LIMITS, "max_extracted_size_mb": 3, "max_single_file_size_mb": 50})
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
    assert not dest.exists()


def test_corrupted_archive_detected(tmp_path):
    zip_path = tmp_path / "corrupt.zip"
    # Stored (uncompressed) so flipping one payload byte guarantees a CRC
    # mismatch without disturbing the ZIP's own structural headers/EOCD.
    _make_zip(zip_path, {"file.txt": b"A" * 1000}, compression=zipfile.ZIP_STORED)

    raw = bytearray(zip_path.read_bytes())
    marker = raw.find(b"A" * 1000)
    assert marker != -1, "test fixture setup couldn't locate the payload bytes to corrupt"
    raw[marker + 500] ^= 0xFF  # flip one payload byte deep inside the content, away from any header
    zip_path.write_bytes(bytes(raw))

    with pytest.raises(ZipSecurityError) as exc_info:
        validate_zip(zip_path, max_zip_size_mb=100)
    assert exc_info.value.code == ZipValidationErrorCode.CORRUPTED_ARCHIVE


def _set_encrypted_flag_raw(zip_path):
    """zipfile.ZipFile.writestr() recomputes/overwrites flag_bits on write,
    so a caller-set ZipInfo.flag_bits doesn't survive — stdlib zipfile also
    has no supported way to WRITE a genuinely password-encrypted entry. To
    exercise validate_zip()'s real flag_bits check against a real ZIP
    structure, patch the general-purpose bit-flag field's low byte
    directly in both the local file header (signature PK\\x03\\x04, flag
    field at signature_offset+6) and the central directory header
    (signature PK\\x01\\x02, flag field at signature_offset+8) — the exact
    fixed byte layout the ZIP format spec defines. Payload is short plain
    ASCII, so a coincidental signature-byte match inside file content is
    not a realistic risk here."""
    data = bytearray(zip_path.read_bytes())
    idx = data.find(b"PK\x01\x02")
    assert idx != -1, "test fixture has no central directory header to patch"
    data[idx + 8] |= 0x01
    idx = data.find(b"PK\x03\x04")
    assert idx != -1, "test fixture has no local file header to patch"
    data[idx + 6] |= 0x01
    zip_path.write_bytes(bytes(data))


def test_encrypted_zip_detected(tmp_path):
    zip_path = tmp_path / "encrypted.zip"
    _make_zip(zip_path, {"secret.txt": b"pretend-encrypted-payload"}, compression=zipfile.ZIP_STORED)
    _set_encrypted_flag_raw(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.infolist()[0].flag_bits & 0x1, "test fixture didn't actually persist the encryption flag bit"

    with pytest.raises(ZipSecurityError) as exc_info:
        validate_zip(zip_path, max_zip_size_mb=100)
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_ENCRYPTED_NOT_SUPPORTED


def test_nested_archive_catalogued_not_extracted(tmp_path):
    zip_path = tmp_path / "nested.zip"
    inner_zip_bytes = tmp_path / "_inner_tmp.zip"
    _make_zip(inner_zip_bytes, {"inner.txt": b"inner content"})
    _make_zip(zip_path, {
        "top_level.pdf": b"a real document",
        "archives/sub.zip": inner_zip_bytes.read_bytes(),
    })

    dest = tmp_path / "extracted"
    results = safe_extract(zip_path, dest, **_DEFAULT_LIMITS)

    by_path = {e.zip_relative_path: e for e in results}
    assert by_path["top_level.pdf"].classification == "extracted"
    assert by_path["archives/sub.zip"].classification == "nested_archive"
    assert by_path["archives/sub.zip"].extracted_path is None
    # The nested archive's own bytes were never written to disk anywhere.
    assert not any(p.name == "sub.zip" for p in dest.rglob("*"))


def test_skip_patterns_excluded_entirely(tmp_path):
    zip_path = tmp_path / "with_junk.zip"
    _make_zip(zip_path, {
        "real_doc.pdf": b"real content",
        "__MACOSX/._real_doc.pdf": b"apple double junk",
        ".DS_Store": b"finder junk",
        "Thumbs.db": b"windows thumbnail cache",
        "sub/._resource_fork": b"apple double junk 2",
    })

    dest = tmp_path / "extracted"
    results = safe_extract(zip_path, dest, **_DEFAULT_LIMITS)

    assert len(results) == 1
    assert results[0].zip_relative_path == "real_doc.pdf"
    assert results[0].classification == "extracted"


def test_unsupported_archive_type_rejected(tmp_path):
    not_a_zip = tmp_path / "fake.zip"
    not_a_zip.write_bytes(b"this is definitely not a zip file, just plain text")

    with pytest.raises(ZipSecurityError) as exc_info:
        validate_zip(not_a_zip, max_zip_size_mb=100)
    assert exc_info.value.code == ZipValidationErrorCode.UNSUPPORTED_ARCHIVE_TYPE


def test_zip_exceeding_max_size_rejected(tmp_path):
    zip_path = tmp_path / "toolarge.zip"
    _make_zip(zip_path, {"small.txt": b"tiny"})

    with pytest.raises(ZipSecurityError) as exc_info:
        validate_zip(zip_path, max_zip_size_mb=0)
    assert exc_info.value.code == ZipValidationErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED
