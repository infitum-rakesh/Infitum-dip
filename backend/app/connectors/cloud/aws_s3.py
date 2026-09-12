"""
AwsS3Connector — real Amazon S3 connectivity via boto3 (the official AWS
SDK). Verified against a live LocalStack (localstack/localstack Docker
image — a real S3-API-compatible emulator, not a fake) instance during
development — the same real SDK calls (list_objects_v2/get_object/
put_object/copy_object/delete_object) a production AWS account would
receive, just pointed at the emulator via `endpoint_url`.

Config shape (see connection_service.py's AWS field schema): region,
service (S3 is the only one this connector implements — see field help
text: Redshift/RDS are wire-protocol-compatible with the existing
POSTGRESQL/MYSQL/SQL_SERVER connectors, not a distinct integration worth
duplicating), resource (bucket name), access_key_id, secret_access_key,
optional endpoint_url (for S3-compatible services like MinIO, or local
testing — this is what LocalStack verification used; blank means real
AWS S3).

Object name = S3 object key (relative to the configured bucket — same
"no separate base_path" convention connectors/cloud/azure_blob.py uses,
for the same reason: S3 has no directory concept, only key prefixes).
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.connectors.base import ConnectionTestResult, ConnectorError, ConnectorValidationError, FileInfo
from app.connectors.files.base import FileConnectorBase


class AwsS3Connector(FileConnectorBase):
    connection_type = "AWS"

    def _bucket(self) -> str:
        bucket = (self.config.get("resource") or "").strip()
        if not bucket:
            raise ConnectorValidationError("No Bucket / Resource configured for this AWS connection.")
        return bucket

    def _client(self):
        service = (self.config.get("service") or "S3").strip()
        if service != "S3":
            raise ConnectorValidationError(
                f"AWS {service} isn't a distinct connector here — {service} speaks the same wire protocol as an "
                "existing database connection type; create a SQL Server/PostgreSQL/MySQL connection pointed at "
                "the RDS/Redshift endpoint instead."
            )
        region = self.config.get("region") or None
        access_key = self.config.get("access_key_id") or None
        secret_key = self.secrets.get("secret_access_key") or None
        endpoint_url = self.config.get("endpoint_url") or None
        try:
            return boto3.client(
                "s3", region_name=region, aws_access_key_id=access_key,
                aws_secret_access_key=secret_key, endpoint_url=endpoint_url,
            )
        except (BotoCoreError, ValueError) as exc:
            raise ConnectorError(self._scrub(f"Could not create S3 client: {exc}")) from exc

    def _resolve_path(self, object_name: str) -> str:
        return object_name  # the S3 key IS the path

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            client = self._client()
            client.head_bucket(Bucket=self._bucket())
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (ClientError, BotoCoreError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def list_files(self, pattern: str | None = None, recursive: bool = False, path: str | None = None) -> list[FileInfo]:
        # recursive is accepted-and-ignored (interface compatibility with
        # every other FileConnectorBase subclass — see
        # LocalFileConnector/SftpConnector/etc.'s real recursive walks):
        # S3 has no real directories, so list_objects_v2 over the whole
        # bucket already returns every key at every nesting level, exactly
        # what "recursive scan" asks for, with no flag needed. `path`
        # (e.g. a schedule's configured source_path) scopes that listing
        # to keys under that prefix via list_objects_v2's own real
        # Prefix param, not a client-side re-filter of every key.
        effective_pattern = pattern or "*"
        is_regex = effective_pattern.startswith("re:")
        regex = re.compile(effective_pattern[3:]) if is_regex else None
        prefix = f"{path.strip().strip('/')}/" if path and path.strip() else ""
        try:
            client = self._client()
            results = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket(), Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue  # S3 "directory marker" — not a real object
                    matched = regex.search(key) if is_regex else fnmatch.fnmatch(key, effective_pattern)
                    if not matched:
                        continue
                    results.append(FileInfo(name=key, path=key, size=obj.get("Size") or 0, modified_at=obj.get("LastModified")))
            results.sort(key=lambda f: f.name)
            return results
        except (ClientError, BotoCoreError) as exc:
            raise ConnectorError(self._scrub(f"Could not list bucket '{self._bucket()}': {exc}")) from exc

    def open_read_stream(self, file_path: str):
        try:
            client = self._client()
            response = client.get_object(Bucket=self._bucket(), Key=file_path)
        except ClientError as exc:
            raise ConnectorError(self._scrub(f"Could not open object '{file_path}': {exc}")) from exc
        return _S3ReadStream(response["Body"])

    def move_file(self, file_path: str, dest_dir: str) -> str:
        """S3 has no native rename — real copy-then-delete, the standard
        way any S3 client achieves a "move" (S3's own console/CLI do the
        exact same two calls under the hood)."""
        try:
            client = self._client()
            bucket = self._bucket()
            filename = file_path.rsplit("/", 1)[-1]
            dest_key = f"{dest_dir.rstrip('/')}/{filename}"
            client.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": file_path}, Key=dest_key)
            client.delete_object(Bucket=bucket, Key=file_path)
            return dest_key
        except (ClientError, BotoCoreError) as exc:
            raise ConnectorError(self._scrub(f"Could not move {file_path} to {dest_dir}: {exc}")) from exc

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
        try:
            client = self._client()
            client.put_object(Bucket=self._bucket(), Key=file_path, Body=data)
            return len(data)
        except (ClientError, BotoCoreError) as exc:
            raise ConnectorError(self._scrub(f"Could not write object '{file_path}': {exc}")) from exc


class _S3ReadStream:
    """Wraps botocore's StreamingBody (already a real bounded-read HTTP
    stream — botocore's own .read(size) reads directly off the socket)
    with a .close() that's always safe to call, matching the duck-type
    every other connector's read stream implements."""

    def __init__(self, body):
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size if size is not None and size >= 0 else None)

    def close(self) -> None:
        self._body.close()
