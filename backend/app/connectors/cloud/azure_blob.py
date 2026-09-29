"""
AzureBlobConnector — real Azure Blob Storage connectivity via the
official azure-storage-blob SDK. Verified against a live Azurite
(Microsoft's own local Azure Storage emulator,
mcr.microsoft.com/azure-storage/azurite) instance during development —
the same real SDK calls (list_blobs/download_blob/upload_blob/delete_
blob/etc.) a production Azure Storage account would receive, just
pointed at a local emulator via a Connection String.

Config shape (see connection_service.py's AZURE field schema):
storage_account, container, auth_type (Connection String / Access Key /
Service Principal). Connection String is the only auth path genuinely
testable against a local emulator (Azurite has no concept of "an access
key against a real storage account" or a real Azure AD tenant) — Access
Key and Service Principal are real, correct code following the SDK's own
documented contract (BlobServiceClient(account_url=..., credential=...)
/ azure-identity's ClientSecretCredential) but weren't exercised against
a real Azure subscription in this environment — same disclosed-but-real
precedent as connectors/database/snowflake.py.

Object name = blob name (relative to the configured container — no
separate base_path/prefix field; a caller wanting a subset lists with a
"prefix*"-style glob pattern, the same fnmatch convention connectors/
ftp/client.py and connectors/sftp/client.py already use, since Azure's
own listing API only supports a plain string prefix, not glob/regex).
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import time

try:
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    from azure.storage.blob import BlobServiceClient
except ImportError:  # pragma: no cover
    AzureError = Exception
    ResourceNotFoundError = Exception
    BlobServiceClient = None

from app.connectors.base import ConnectionTestResult, ConnectorError, ConnectorValidationError, FileInfo
from app.connectors.files.base import FileConnectorBase


class AzureBlobConnector(FileConnectorBase):
    connection_type = "AZURE"

    def _container(self) -> str:
        container = (self.config.get("container") or "").strip()
        if not container:
            raise ConnectorValidationError("No Container configured for this Azure connection.")
        return container

    def _service_client(self) -> BlobServiceClient:
        auth_type = self.config.get("auth_type") or "Access Key"
        if auth_type == "Connection String":
            conn_str = self.secrets.get("connection_string")
            if not conn_str:
                raise ConnectorValidationError("No Connection String configured for this Azure connection.")
            return BlobServiceClient.from_connection_string(conn_str)

        account = (self.config.get("storage_account") or "").strip()
        if not account:
            raise ConnectorValidationError("No Storage Account configured for this Azure connection.")
        account_url = f"https://{account}.blob.core.windows.net"

        if auth_type == "Access Key":
            key = self.secrets.get("access_key")
            if not key:
                raise ConnectorValidationError("No Access Key configured for this Azure connection.")
            return BlobServiceClient(account_url=account_url, credential=key)

        if auth_type == "Service Principal":
            from azure.identity import ClientSecretCredential
            tenant_id = self.config.get("tenant_id")
            client_id = self.config.get("client_id")
            client_secret = self.secrets.get("client_secret")
            if not all([tenant_id, client_id, client_secret]):
                raise ConnectorValidationError("Service Principal auth needs Tenant ID, Client ID and Client Secret.")
            credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
            return BlobServiceClient(account_url=account_url, credential=credential)

        raise ConnectorValidationError(f"Unknown Azure auth_type '{auth_type}'.")

    def _container_client(self):
        return self._service_client().get_container_client(self._container())

    def _resolve_path(self, object_name: str) -> str:
        return object_name  # blob name IS the path — no separate base dir concept

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            container = self._container_client()
            props = container.get_container_properties()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms, server_version=str(props.get("etag") or "").strip('"') or None)
        except (AzureError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def list_files(self, pattern: str | None = None, recursive: bool = False, path: str | None = None) -> list[FileInfo]:
        # recursive is accepted-and-ignored — Blob Storage has no real
        # directories (see AwsS3Connector's identical note); list_blobs()
        # over the whole container already returns every blob at every
        # nesting level. `path` (e.g. a schedule's configured
        # source_path) scopes that listing to blobs whose name starts
        # with that prefix — real server-side filtering via the SDK's own
        # name_starts_with, not a client-side re-filter of everything.
        effective_pattern = pattern or "*"
        is_regex = effective_pattern.startswith("re:")
        regex = re.compile(effective_pattern[3:]) if is_regex else None
        prefix = f"{path.strip().strip('/')}/" if path and path.strip() else None
        try:
            container = self._container_client()
            results = []
            for blob in container.list_blobs(name_starts_with=prefix):
                matched = regex.search(blob.name) if is_regex else fnmatch.fnmatch(blob.name, effective_pattern)
                if not matched:
                    continue
                results.append(FileInfo(
                    name=blob.name, path=blob.name, size=blob.size or 0,
                    modified_at=blob.last_modified,
                ))
            results.sort(key=lambda f: f.name)
            return results
        except AzureError as exc:
            raise ConnectorError(self._scrub(f"Could not list container '{self._container()}': {exc}")) from exc

    def open_read_stream(self, file_path: str):
        try:
            blob_client = self._container_client().get_blob_client(file_path)
            downloader = blob_client.download_blob()
        except ResourceNotFoundError as exc:
            raise ConnectorError(self._scrub(f"Blob '{file_path}' not found: {exc}")) from exc
        except AzureError as exc:
            raise ConnectorError(self._scrub(f"Could not open blob '{file_path}': {exc}")) from exc
        return _BlobReadStream(downloader)

    def move_file(self, file_path: str, dest_dir: str) -> str:
        """Azure has no native rename — real copy-then-delete, same as
        any Azure Blob client would have to do (there is no server-side
        "move" primitive for blobs, only copy + separate delete)."""
        try:
            filename = file_path.rsplit("/", 1)[-1]
            dest_path = f"{dest_dir.rstrip('/')}/{filename}"
            source_client = self._container_client().get_blob_client(file_path)
            dest_client = self._container_client().get_blob_client(dest_path)
            dest_client.start_copy_from_url(source_client.url)
            source_client.delete_blob()
            return dest_path
        except AzureError as exc:
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
            blob_client = self._container_client().get_blob_client(file_path)
            blob_client.upload_blob(data, overwrite=True)
            return len(data)
        except AzureError as exc:
            raise ConnectorError(self._scrub(f"Could not write blob '{file_path}': {exc}")) from exc


class _BlobReadStream:
    """Wraps azure-storage-blob's StorageStreamDownloader into the same
    bounded .read(size)/.close() duck-type every other connector's read
    stream implements — the downloader streams real chunks over the
    network internally; this just exposes them via a plain buffer so
    partial reads never force the whole blob into memory at once."""

    def __init__(self, downloader):
        self._downloader = downloader
        self._chunks = downloader.chunks()
        self._buf = b""
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buf]
            self._buf = b""
            for chunk in self._chunks:
                parts.append(chunk)
            self._eof = True
            return b"".join(parts)

        while len(self._buf) < size and not self._eof:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._eof = True
                break
        result, self._buf = self._buf[:size], self._buf[size:]
        return result

    def close(self) -> None:
        pass  # StorageStreamDownloader has no explicit close — the underlying HTTP response is consumed via chunks()
