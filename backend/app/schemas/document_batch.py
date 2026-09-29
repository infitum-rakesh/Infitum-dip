"""
ZIP Batch Upload — API schemas (ZB10). Response shapes bind straight off
the ORM rows via `model_validate()` (from_attributes) — same convention
as app/schemas/documents.py and app/schemas/invoices.py, since these
routes are thin pass-throughs with no derived/joined fields to patch on
afterward (unlike DocumentOut.uploaded_by_name etc.).
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class InitZipUploadRequest(BaseModel):
    filename: str
    size_bytes: int
    chunk_size: int
    total_chunks: int


class CompleteZipUploadRequest(BaseModel):
    expected_sha256: str | None = None
    auto_process: bool = True
    duplicate_policy: str = "SKIP_DUPLICATE"
    # Per-batch Processed Data Storage override (Part 21 — "DATA STORAGE
    # FOR ZIP PROCESSING"). storage_type=None (the default) means the
    # upload dialog's own "SQLite" choice — no override, this batch's
    # documents push through the org-wide config exactly as before this
    # feature existed. See document_batch_service.create_batch_from_upload().
    storage_type: str | None = None
    storage_target_connection_id: int | None = None
    storage_target_schema: str | None = None
    storage_target_table: str | None = None
    storage_load_mode: str | None = None
    storage_file_format: str | None = None


class BatchUploadOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int | None
    filename: str
    status: str
    size_bytes: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class CompleteZipUploadOut(BaseModel):
    upload: BatchUploadOut
    is_duplicate_upload: bool
    duplicate_of_upload_id: int | None = None
    batch: "DocumentBatchOut"


class DocumentBatchOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: int | None
    upload_id: int
    pipeline_type: str
    original_filename: str
    zip_sha256: str
    duplicate_policy: str
    total_entries: int
    valid_files: int
    duplicate_files: int
    unsupported_files: int
    failed_files: int
    completed_files: int
    review_files: int
    status: str
    effective_workers: int
    auto_process: bool
    cancel_requested: bool
    pause_requested: bool
    storage_type: str | None = None
    storage_target_connection_id: int | None = None
    storage_target_schema: str | None = None
    storage_target_table: str | None = None
    storage_load_mode: str | None = None
    storage_file_format: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class DocumentBatchListOut(BaseModel):
    items: list[DocumentBatchOut]
    total: int
    page: int
    page_size: int


class DocumentBatchFileOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    batch_id: int
    document_id: int | None = None
    zip_relative_path: str
    original_filename: str
    file_extension: str | None = None
    file_size: int
    sha256: str
    status: str
    attempt_number: int
    worker_id: str | None = None
    duplicate_of_document_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_heartbeat: datetime | None = None
    created_at: datetime


class DocumentBatchFileListOut(BaseModel):
    items: list[DocumentBatchFileOut]
    total: int
    page: int
    page_size: int


class DocumentBatchEventOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    batch_id: int
    file_id: int | None = None
    event_type: str
    message: str | None = None
    worker_id: str | None = None
    event_metadata: dict | None = None
    event_timestamp: datetime


class DocumentBatchEventListOut(BaseModel):
    items: list[DocumentBatchEventOut]
    since_id: int


class RetrySelectedRequest(BaseModel):
    file_ids: list[int]


class DocumentBatchMetricsOut(BaseModel):
    batch_id: int
    status_counts: dict[str, int]
    total_entries: int
    valid_files: int
    duplicate_files: int
    unsupported_files: int
    completed_files: int
    failed_files: int
    review_files: int
    elapsed_seconds: float | None = None


CompleteZipUploadOut.model_rebuild()
