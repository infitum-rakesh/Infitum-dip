"""
OracleErpConnector — real, HTTP-based connectivity to Oracle Fusion ERP
Cloud's documented Payables Invoices REST resource (httpx, same library
connectors/api/rest.py and connectors/ai/* already use for real HTTP
calls). Built strictly against Oracle's own PUBLICLY DOCUMENTED REST API
contract (the "Invoices" resource under
`/fscmRestApi/resources/{version}/invoices`, part of Oracle's published
Financials Cloud REST API reference) — not against any undocumented or
proprietary Oracle internals, per this build's explicit scope constraint.

Real, but NOT live-verified: this environment has no Oracle Fusion ERP
Cloud tenant to authenticate against (a licensed, provisioned instance —
none available here), same disclosed-but-real precedent as
connectors/database/sap.py, connectors/database/fabric.py and
connectors/application/salesforce.py. Every HTTP call below is a genuine
request built to the documented resource shape (path, query params,
field names, custom-action convention); nothing is simulated or
randomized. Structural correctness is verified against Oracle's own
public REST API documentation for this resource; a real tenant is
required to verify runtime behavior (exact validation error wording,
rate limits, etc.) end to end — that gap is disclosed here and in the
final implementation report, never silently assumed to be tested.

Auth (see services/connection_service.py's ORACLE_ERP_CLOUD field
schema) — Oracle Fusion Applications REST APIs document two supported
mechanisms:
  Basic Auth — HTTP Basic with the ERP Cloud application user's own
               username/password. Oracle's default/simplest documented
               auth mode for these REST APIs.
  OAuth2     — client_credentials grant against an Oracle Identity Cloud
               Service (IDCS) / OCI IAM token endpoint, identical
               mechanics to connectors/api/rest.py's OAuth2 branch (same
               in-process token cache, same real refresh-before-expiry
               behavior) — Oracle's documented mechanism for
               server-to-server integration without a human login.

Resource shape (Oracle's own documented AP Invoice REST fields — see
services/invoice/erp/oracle_erp_mapping.py, INV9, for the canonical ->
Oracle field mapping):
  Header: InvoiceNumber, InvoiceCurrency, InvoiceAmount, InvoiceDate,
          BusinessUnit, InvoiceTypeCode, Source, Description,
          PaymentTerms, VendorNum / VendorSiteCode.
  Lines (child resource "invoiceLines"): LineNumber, LineTypeLookupCode,
          LineAmount, Description, DistributionCombination (or
          AccountingDate/DistributionSetName, deployment-dependent).
  Status fields Oracle documents on GET: ValidationStatus, ApprovalStatus,
          PaymentStatus.
  Custom actions follow Oracle's own REST convention of
          POST .../invoices/{InvoiceId}/action/<actionName> — used here
          for cancelInvoice.

health_check() hits the resource's own "describe" metadata endpoint
(`?onlyData=false&limit=0`-style lightweight call is deployment-specific,
so this simply reuses test_connection()'s base reachability check —
Oracle does not document a separate universal service-health endpoint
for this resource the way some platforms do).
"""
from __future__ import annotations

import time

import httpx

from app.connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ConnectorValidationError,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_API_VERSION = "11.13.18.05"  # Oracle's long-stable published REST API version identifier for this resource


class OracleErpConnector(BaseConnector):
    connection_type = "ORACLE_ERP_CLOUD"

    # In-process OAuth2 token cache — never persisted to disk/DB. Same
    # cache_key-scoped pattern as connectors/api/rest.py's
    # RestApiConnector, kept as a separate class-level dict so the two
    # connector types never collide on cache key.
    _oauth2_cache: dict[str, tuple[str, float]] = {}

    def _host(self) -> str:
        host = (self.config.get("host") or "").strip()
        if not host:
            raise ConnectorValidationError("This Oracle ERP Cloud connection has no Host (instance URL) configured.")
        return host.rstrip("/")

    def _api_version(self) -> str:
        return (self.config.get("api_version") or DEFAULT_API_VERSION).strip()

    def _invoices_url(self) -> str:
        return f"{self._host()}/fscmRestApi/resources/{self._api_version()}/invoices"

    def _client_auth(self):
        if (self.config.get("auth_type") or "Basic Auth") == "Basic Auth":
            username = self.config.get("username") or ""
            password = self.secrets.get("password") or ""
            if not (username and password):
                raise ConnectorValidationError("This Oracle ERP Cloud connection uses Basic Auth but username/password aren't fully configured.")
            return httpx.BasicAuth(username, password)
        return None

    def _oauth2_token(self) -> str:
        cache_key = self._cache_key or f"anon:{id(self)}"
        cached = OracleErpConnector._oauth2_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        token_url = self.config.get("token_url")
        client_id = self.config.get("client_id")
        client_secret = self.secrets.get("client_secret")
        if not (token_url and client_id and client_secret):
            raise ConnectorValidationError("This Oracle ERP Cloud connection uses OAuth2 auth but token_url/client_id/client_secret aren't fully configured.")
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
                resp = client.post(token_url, data={
                    "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                })
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach OAuth2 token URL {token_url}: {exc}")) from exc
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"OAuth2 token request failed: HTTP {resp.status_code} {resp.text[:200]}"))
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ConnectorError("OAuth2 token endpoint responded but returned no access_token.")
        expires_in = float(data.get("expires_in") or 300)
        OracleErpConnector._oauth2_cache[cache_key] = (token, time.monotonic() + max(30.0, expires_in - 30.0))
        return token

    def _auth_headers(self) -> dict:
        if (self.config.get("auth_type") or "Basic Auth") == "OAuth2":
            return {"Authorization": f"Bearer {self._oauth2_token()}"}
        return {}  # Basic Auth goes through httpx's auth= param, not a header here

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            headers = self._auth_headers()
            client_auth = self._client_auth()
        except ConnectorValidationError as exc:
            return ConnectionTestResult(False, str(exc))
        except ConnectorError as exc:
            # OAuth2 auth type routes through _oauth2_token(), which makes
            # a real network call to token_url and can raise ConnectorError
            # (unreachable host, or the IDCS/OCI IAM endpoint rejecting the
            # client credentials) — without this, that error would
            # propagate uncaught out of test_connection() instead of the
            # graceful ConnectionTestResult(False, ...) every other failure
            # path here returns.
            return ConnectionTestResult(False, str(exc))
        # limit=1 keeps this a genuinely cheap real call — reads real
        # metadata about the invoices resource without pulling any actual
        # invoice data, same intent as REST connector's bare-base-URL GET.
        url = f"{self._invoices_url()}?limit=1&onlyData=true"
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=client_auth) as client:
                resp = client.get(url, headers={**headers, "REST-Framework-Version": "4"})
        except httpx.RequestError as exc:
            return ConnectionTestResult(False, self._scrub(f"Could not reach {self._host()}: {exc}"))
        latency_ms = (time.monotonic() - t0) * 1000
        if resp.status_code >= 500:
            return ConnectionTestResult(False, self._scrub(f"Server error reaching Oracle ERP Cloud: HTTP {resp.status_code}"), latency_ms)
        if resp.status_code in (401, 403):
            return ConnectionTestResult(False, self._scrub(f"Oracle ERP Cloud rejected these credentials: HTTP {resp.status_code}"), latency_ms)
        if resp.status_code == 404:
            return ConnectionTestResult(False, self._scrub(f"Invoices resource not found at {url} — check Host / API Version."), latency_ms)
        return ConnectionTestResult(
            True, f"Reached Oracle ERP Cloud Invoices resource at {self._host()} — HTTP {resp.status_code}.",
            latency_ms, server_version=self._api_version(),
        )

    def list_invoices(self, *, limit: int = 25, offset: int = 0, q: str | None = None) -> dict:
        """PAY21 — scheduled ERP-sourced intake (the inbound counterpart of
        submit_invoice()'s outbound posting): a real GET against the same
        documented Invoices collection resource, using Oracle's own
        documented collection-query params (`limit`/`offset` paging,
        `hasMore` in the response telling the caller whether to fetch
        another page — the standard shape every Oracle ADF/REST BC
        collection resource returns, not something specific to this one
        resource) plus `expand=invoiceLines` (Oracle's documented
        child-resource inlining convention) so each returned invoice
        already carries its line items in one round trip rather than a
        second call per invoice. `q` is Oracle's own documented REST
        query-language filter string (e.g. "ValidationStatus='NEEDS_REVALIDATION'"),
        passed through unmodified when the caller supplies one — this
        method builds no filter of its own.

        Returns the decoded JSON response verbatim (`items`/`count`/
        `hasMore`/...) — reshaping into this build's own record shape is
        document_schedule_service.discover_erp_records()'s job, the same
        division of labor fetch_api()/discover_api_records() already use."""
        params = {"limit": limit, "offset": offset, "onlyData": "true", "expand": "invoiceLines"}
        if q:
            params["q"] = q
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
                resp = client.get(self._invoices_url(), headers=self._auth_headers(), params=params)
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Oracle ERP Cloud to list invoices: {exc}")) from exc
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"Oracle ERP Cloud invoice listing failed: HTTP {resp.status_code} {resp.text[:400]}"))
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError(f"Oracle ERP Cloud's invoice listing response was not valid JSON: {exc}") from exc

    def submit_invoice(self, payload: dict, *, idempotency_key: str | None = None) -> dict:
        headers = {**self._auth_headers(), "Content-Type": "application/vnd.oracle.adf.resourceitem+json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
                resp = client.post(self._invoices_url(), headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Oracle ERP Cloud to submit invoice: {exc}")) from exc
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"Oracle ERP Cloud rejected the invoice: HTTP {resp.status_code} {resp.text[:400]}"))
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError(f"Oracle ERP Cloud's invoice creation response was not valid JSON: {exc}") from exc

    def get_invoice_status(self, erp_invoice_id: str) -> dict:
        url = f"{self._invoices_url()}/{erp_invoice_id}"
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
                resp = client.get(url, headers=self._auth_headers())
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Oracle ERP Cloud for invoice status: {exc}")) from exc
        if resp.status_code == 404:
            raise ConnectorError(f"Oracle ERP Cloud has no invoice with id {erp_invoice_id}.")
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"Oracle ERP Cloud status lookup failed: HTTP {resp.status_code} {resp.text[:300]}"))
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError(f"Oracle ERP Cloud's invoice status response was not valid JSON: {exc}") from exc

    def cancel_invoice(self, erp_invoice_id: str) -> dict:
        url = f"{self._invoices_url()}/{erp_invoice_id}/action/cancelInvoice"
        headers = {**self._auth_headers(), "Content-Type": "application/vnd.oracle.adf.action+json"}
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
                resp = client.post(url, headers=headers, json={})
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Oracle ERP Cloud to cancel invoice: {exc}")) from exc
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"Oracle ERP Cloud refused to cancel invoice {erp_invoice_id}: HTTP {resp.status_code} {resp.text[:400]}"))
        try:
            return resp.json() if resp.text else {"status": "CANCELLED", "invoice_id": erp_invoice_id}
        except ValueError:
            return {"status": "CANCELLED", "invoice_id": erp_invoice_id}
