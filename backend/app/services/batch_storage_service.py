"""
ZIP Batch Upload — storage abstraction (ZB3).

A thin interface (BatchStorageProvider) with only a real Local
implementation (LocalBatchStorageProvider) — satisfies the spec's "pluggable
for future S3/Azure providers" ask without building unused cloud adapters
that can't be tested in this environment. Every method call is a real
filesystem operation; nothing here is a stub.

Layout (tenant-scoped, never inside the application source directory):

    {upload_dir}/tenants/{organization_id}/uploads/{upload_id}/.chunks/part_000001
    {upload_dir}/tenants/{organization_id}/uploads/{upload_id}/source.zip
    {upload_dir}/tenants/{organization_id}/batches/{batch_id}/extracted/...

Uploads are scoped by upload_id, not batch_id, because a DocumentBatch row
doesn't exist yet during the chunked-upload phase — it's only created once
the assembled ZIP has been validated and extraction begins
(document_batch_service.py, ZB4). DocumentBatch.upload_id is the FK that
ties the two together afterward, so nothing is lost by not nesting the
upload under a batch directory.

Chunk assembly streams each part file into the target in 1 MB reads
(shutil.copyfileobj) — a multi-GB ZIP is never held in memory, matching the
same streaming discipline zip_security_service.py uses for extraction.
"""
from __future__ import annotations

import abc
import shutil
from pathlib import Path


class BatchStorageProvider(abc.ABC):
    @abc.abstractmethod
    def save_chunk(self, organization_id: int | None, upload_id: int, chunk_number: int, data: bytes) -> int:
        """Writes one chunk to durable storage; returns bytes written."""
        raise NotImplementedError

    @abc.abstractmethod
    def chunk_numbers_present(self, organization_id: int | None, upload_id: int) -> set[int]:
        """Real, on-disk source of truth for which chunks have actually
        landed — used for resume, independent of BatchUpload.received_chunks
        (which is just a fast-read cache of this)."""
        raise NotImplementedError

    @abc.abstractmethod
    def assemble(self, organization_id: int | None, upload_id: int, total_chunks: int) -> Path:
        """Concatenates all chunk parts, in order, into the assembled file.
        Returns the assembled file's path. Raises FileNotFoundError if any
        part is missing — callers must confirm completeness first."""
        raise NotImplementedError

    @abc.abstractmethod
    def assembled_path(self, organization_id: int | None, upload_id: int) -> Path:
        raise NotImplementedError

    @abc.abstractmethod
    def delete_chunks(self, organization_id: int | None, upload_id: int) -> None:
        """Removes the now-redundant chunk parts after a successful
        assembly — not the retention/cleanup job (ZB9), just tidying up an
        upload's own working directory once its job is done."""
        raise NotImplementedError

    @abc.abstractmethod
    def delete_upload(self, organization_id: int | None, upload_id: int) -> None:
        """Removes everything for one upload session, including the
        assembled file — used on cancel."""
        raise NotImplementedError

    @abc.abstractmethod
    def batch_extracted_root(self, organization_id: int | None, batch_id: int) -> Path:
        raise NotImplementedError

    @abc.abstractmethod
    def delete_batch_extracted(self, organization_id: int | None, batch_id: int) -> None:
        """Removes a batch's extracted/ directory tree — the retention/
        cleanup job (ZB9), once a batch is old enough and has nothing left
        that still needs the original bytes (see document_batch_service.
        cleanup_expired_document_batches() for the actual eligibility
        check). DocumentBatch/DocumentBatchFile DB rows are never touched
        here — they're the audit trail, kept regardless."""
        raise NotImplementedError


class LocalBatchStorageProvider(BatchStorageProvider):
    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir)

    @staticmethod
    def _org_segment(organization_id: int | None) -> str:
        return str(organization_id) if organization_id is not None else "unassigned"

    def _upload_root(self, organization_id: int | None, upload_id: int) -> Path:
        return self._base / "tenants" / self._org_segment(organization_id) / "uploads" / str(upload_id)

    def _chunks_dir(self, organization_id: int | None, upload_id: int) -> Path:
        return self._upload_root(organization_id, upload_id) / ".chunks"

    def assembled_path(self, organization_id: int | None, upload_id: int) -> Path:
        return self._upload_root(organization_id, upload_id) / "source.zip"

    def save_chunk(self, organization_id: int | None, upload_id: int, chunk_number: int, data: bytes) -> int:
        chunks_dir = self._chunks_dir(organization_id, upload_id)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        part_path = chunks_dir / f"part_{chunk_number:06d}"
        part_path.write_bytes(data)
        return len(data)

    def chunk_numbers_present(self, organization_id: int | None, upload_id: int) -> set[int]:
        chunks_dir = self._chunks_dir(organization_id, upload_id)
        if not chunks_dir.is_dir():
            return set()
        present = set()
        for p in chunks_dir.iterdir():
            if p.name.startswith("part_"):
                try:
                    present.add(int(p.name[len("part_"):]))
                except ValueError:
                    continue
        return present

    def assemble(self, organization_id: int | None, upload_id: int, total_chunks: int) -> Path:
        chunks_dir = self._chunks_dir(organization_id, upload_id)
        target = self.assembled_path(organization_id, upload_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            for n in range(total_chunks):
                part_path = chunks_dir / f"part_{n:06d}"
                if not part_path.is_file():
                    raise FileNotFoundError(f"Chunk {n} is missing — cannot assemble upload {upload_id}.")
                with open(part_path, "rb") as pf:
                    shutil.copyfileobj(pf, out, length=1024 * 1024)
        return target

    def delete_chunks(self, organization_id: int | None, upload_id: int) -> None:
        shutil.rmtree(self._chunks_dir(organization_id, upload_id), ignore_errors=True)

    def delete_upload(self, organization_id: int | None, upload_id: int) -> None:
        shutil.rmtree(self._upload_root(organization_id, upload_id), ignore_errors=True)

    def batch_extracted_root(self, organization_id: int | None, batch_id: int) -> Path:
        return self._base / "tenants" / self._org_segment(organization_id) / "batches" / str(batch_id) / "extracted"

    def delete_batch_extracted(self, organization_id: int | None, batch_id: int) -> None:
        shutil.rmtree(self.batch_extracted_root(organization_id, batch_id), ignore_errors=True)


def get_batch_storage() -> BatchStorageProvider:
    """Single choke point every caller uses instead of instantiating
    LocalBatchStorageProvider directly — the seam a future
    S3StorageProvider/AzureBlobStorageProvider would be swapped in behind."""
    from app.core.config import get_settings
    return LocalBatchStorageProvider(get_settings().upload_dir)
