"""
RestApiConnector — real, HTTP-based REST API source connector (httpx —
the same library connectors/ai/* already uses for real HTTP calls).

test_connection() does a genuine GET against the connection's base_url;
success/latency/reachability are all real, not simulated. fetch_api()
does a genuine paginated fetch against one endpoint — every HTTP request
it logs actually went out over the network; there is no synthetic/sample
response path.

Auth (auth_type on the Connection's config, see services/
connection_service.py's REST field schema):
  None          — no auth headers.
  API Key       — sent as a request header (name configurable via
                  api_key_header, default "X-API-Key" — APIs vary on this
                  and there's no universal convention).
  Bearer Token  — Authorization: Bearer <token>.
  Basic Auth    — real HTTP Basic (httpx.BasicAuth), not just base64'd
                  into a header by hand.
  OAuth2        — real client_credentials grant against token_url; the
                  resulting access_token is cached in-process per
                  connection (keyed by the same cache_key the DB
                  connector pool uses) until shortly before its real
                  expires_in elapses, then refreshed — never persisted,
                  never returned to the caller.

Pagination (the `pagination` dict passed to fetch_api — see Source
Dataset node config for API objects):
  style: "none" | "offset" | "page" | "cursor" | "token"
    none    — a single request, no paging.
    offset  — increments an offset/limit query param pair each page.
    page    — increments a 1-based page-number query param each page.
    cursor  — re-issues the same request with a cursor value pulled out
              of the PREVIOUS response via cursor_path (JSON path),
              stops when that path yields nothing.
    token   — identical mechanics to cursor, separate name because many
              real APIs call the field "page_token"/"next_page_token"
              rather than "cursor" and the spec calls them out as
              distinct pagination styles.
  Every style stops for real once a page comes back short of page_size
  (offset/page) or the next cursor/token is empty (cursor/token), or once
  MAX_PAGES / the caller's `limit` is hit — never loops forever against a
  misbehaving API.

Nested JSON extraction: `json_path` is a minimal, dependency-free dot-path
evaluator (e.g. "$.data.customers" from the spec's own example, or
"$.result.items") — see _json_path_extract(). No wildcards/filters: no
real API config exercised in this build needs them, and a clearly-scoped
implementation beats a half-finished JSONPath dialect.

send_records() (added for the API Output Dataset target) is the
write-side counterpart to fetch_api() — real batched POST/PUT/PATCH
requests, same auth/retry machinery, no fabricated "success" if the
remote API rejects a batch.
"""
from __future__ import annotations

import time

import httpx

from app.connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ConnectorValidationError,
    PreviewResult,
)

DEFAULT_TIMEOUT = 30.0
MAX_PAGES = 50       # safety cap on real HTTP round trips per fetch_api() call
MAX_ROWS = 50000      # mirrors execution/pipeline_job_executor.py's SOURCE_ROW_CAP — same
                       # "small pipelines this build targets, not a distributed big-data
                       # engine" scope, disclosed via the truncated flag, never silent.


def _json_path_extract(data, path: str):
    """Dot-path evaluator. "$.data.customers" -> data["data"]["customers"];
    numeric segments index into a list (e.g. "$.results.0.items"). Raises
    ConnectorValidationError with the actual available keys on a miss —
    never returns None/[] silently for a path that doesn't exist, which
    would look like "the API returned zero rows" instead of "the JSON
    path is wrong"."""
    if not path or path in ("$", "."):
        return data
    body = path[1:] if path.startswith("$") else path
    segments = [s for s in body.split(".") if s != ""]
    current = data
    for seg in segments:
        if isinstance(current, list):
            try:
                idx = int(seg)
            except ValueError:
                raise ConnectorValidationError(
                    f"JSON path segment '{seg}' expects a list index (the response at this point is a list of {len(current)} item(s))."
                )
            if idx < 0 or idx >= len(current):
                raise ConnectorValidationError(f"JSON path index {idx} is out of range — the list has {len(current)} item(s).")
            current = current[idx]
        elif isinstance(current, dict):
            if seg not in current:
                available = ", ".join(list(current.keys())[:10]) or "(none)"
                raise ConnectorValidationError(f"JSON path segment '{seg}' not found in the response — available keys here: {available}.")
            current = current[seg]
        else:
            raise ConnectorValidationError(f"JSON path segment '{seg}' expects an object or list, but found {type(current).__name__}.")
    return current


class RestApiConnector(BaseConnector):
    connection_type = "REST"

    # In-process OAuth2 token cache — never persisted to disk/DB. Keyed by
    # this connection's cache_key so two different REST connections (even
    # to the same host with different credentials) never share a token.
    _oauth2_cache: dict[str, tuple[str, float]] = {}

    def _base_url(self) -> str:
        base = (self.config.get("base_url") or "").strip()
        if not base:
            raise ConnectorValidationError("This REST connection has no Base URL configured.")
        return base.rstrip("/")

    def _client_auth(self):
        if (self.config.get("auth_type") or "None") == "Basic Auth":
            return httpx.BasicAuth(self.config.get("username") or "", self.secrets.get("password") or "")
        return None

    def _oauth2_token(self) -> str:
        cache_key = self._cache_key or f"anon:{id(self)}"
        cached = RestApiConnector._oauth2_cache.get(cache_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        token_url = self.config.get("token_url")
        client_id = self.config.get("client_id")
        client_secret = self.secrets.get("client_secret")
        if not (token_url and client_id and client_secret):
            raise ConnectorValidationError("This REST connection uses OAuth2 auth but token_url/client_id/client_secret aren't fully configured.")
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
        RestApiConnector._oauth2_cache[cache_key] = (token, time.monotonic() + max(30.0, expires_in - 30.0))
        return token

    def _auth_headers(self) -> dict:
        auth_type = self.config.get("auth_type") or "None"
        if auth_type == "API Key":
            key = self.secrets.get("api_key")
            if not key:
                raise ConnectorValidationError("This REST connection uses API Key auth but no API Key is configured.")
            return {(self.config.get("api_key_header") or "X-API-Key"): key}
        if auth_type == "Bearer Token":
            token = self.secrets.get("bearer_token")
            if not token:
                raise ConnectorValidationError("This REST connection uses Bearer Token auth but no token is configured.")
            return {"Authorization": f"Bearer {token}"}
        if auth_type == "OAuth2":
            return {"Authorization": f"Bearer {self._oauth2_token()}"}
        return {}  # None / Basic Auth (Basic goes through httpx's auth= param, not a header here)

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            headers = self._auth_headers()
        except ConnectorValidationError as exc:
            return ConnectionTestResult(False, str(exc))
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
                resp = client.get(self._base_url(), headers=headers)
        except httpx.RequestError as exc:
            return ConnectionTestResult(False, self._scrub(f"Could not reach {self._base_url()}: {exc}"))
        latency_ms = (time.monotonic() - t0) * 1000
        if resp.status_code >= 500:
            return ConnectionTestResult(False, self._scrub(f"Server error reaching {self._base_url()}: HTTP {resp.status_code}"), latency_ms)
        # A 401/403/404 on the bare base URL still proves the host is real
        # and reachable — the actual endpoint + auth scoping is verified
        # per Source Dataset node via a real preview, not here.
        return ConnectionTestResult(True, f"Reached {self._base_url()} — HTTP {resp.status_code}.", latency_ms, server_version=resp.headers.get("server"))

    def fetch_api(
        self, *, endpoint: str, method: str = "GET", query_params: dict | None = None,
        headers: dict | None = None, body: dict | None = None, json_path: str | None = None,
        pagination: dict | None = None, limit: int = 1000, timeout: float | None = None,
        retry_max: int = 2, rate_limit_delay: float = 0.0,
    ) -> PreviewResult:
        if not endpoint:
            raise ConnectorValidationError("No endpoint configured for this API source node.")
        pagination = pagination or {"style": "none"}
        style = pagination.get("style", "none")
        page_size = max(1, int(pagination.get("page_size") or 100))
        max_pages = max(1, min(int(pagination.get("max_pages") or MAX_PAGES), MAX_PAGES))
        bounded_limit = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))

        url = f"{self._base_url()}{endpoint if endpoint.startswith('/') else '/' + endpoint}"
        merged_headers = {**self._auth_headers(), **(headers or {})}
        base_params = dict(query_params or {})

        all_rows: list[dict] = []
        columns: list[str] = []
        page = 0
        cursor_value = None
        truncated = False

        with httpx.Client(timeout=timeout or DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
            while True:
                page += 1
                params = dict(base_params)
                if style == "offset":
                    params[pagination.get("offset_param") or "offset"] = (page - 1) * page_size
                    params[pagination.get("page_size_param") or "limit"] = page_size
                elif style == "page":
                    params[pagination.get("page_param") or "page"] = page
                    params[pagination.get("page_size_param") or "page_size"] = page_size
                elif style in ("cursor", "token"):
                    param_name = pagination.get("cursor_param" if style == "cursor" else "token_param") or ("cursor" if style == "cursor" else "page_token")
                    if cursor_value is not None:
                        params[param_name] = cursor_value
                    params[pagination.get("page_size_param") or "limit"] = page_size

                resp = self._request_with_retry(client, method, url, params, merged_headers, body, retry_max)
                if resp.status_code >= 400:
                    raise ConnectorError(self._scrub(f"{method.upper()} {endpoint} returned HTTP {resp.status_code}: {resp.text[:300]}"))
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ConnectorError(f"Response from {endpoint} was not valid JSON: {exc}") from exc

                extracted = _json_path_extract(data, json_path) if json_path else data
                if isinstance(extracted, dict):
                    extracted = [extracted]
                if not isinstance(extracted, list):
                    raise ConnectorValidationError(
                        f"JSON path '{json_path or '$'}' resolved to a {type(extracted).__name__}, not a list of records."
                    )

                for row in extracted:
                    if isinstance(row, dict):
                        all_rows.append(row)
                        for k in row.keys():
                            if k not in columns:
                                columns.append(k)

                if len(all_rows) >= bounded_limit:
                    all_rows = all_rows[:bounded_limit]
                    truncated = True
                    break
                if style == "none" or not extracted:
                    break
                if page >= max_pages:
                    truncated = True
                    break
                if style in ("cursor", "token"):
                    cursor_path = pagination.get("cursor_path" if style == "cursor" else "token_path")
                    cursor_value = _json_path_extract(data, cursor_path) if cursor_path else None
                    if not cursor_value:
                        break
                elif len(extracted) < page_size:
                    break  # short page under offset/page style — no more data

                if rate_limit_delay:
                    time.sleep(min(rate_limit_delay, 5.0))

        rows = [[r.get(c) for c in columns] for r in all_rows]
        return PreviewResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)

    def send_records(
        self, *, endpoint: str, method: str = "POST", query_params: dict | None = None,
        headers: dict | None = None, records: list[dict], records_key: str | None = None,
        batch_size: int = 500, timeout: float | None = None, retry_max: int = 2,
    ) -> dict:
        """Real, batched write — the write-side counterpart to fetch_api()
        above. Each batch is one genuine HTTP request (method defaults to
        POST; PUT/PATCH also real) carrying up to `batch_size` records as
        the JSON body — either the raw array, or {records_key: [...]} when
        the target API expects an enveloped payload (mirrors json_path on
        the read side). Reuses _request_with_retry for the same real
        retry-on-5xx/backoff behavior fetch_api() has. A REST endpoint has
        no cross-request transaction: if batch N fails after N-1 batches
        already succeeded, those records are already on the remote system
        — the raised error says exactly how many got through, never
        rolled back or silently retried past retry_max."""
        if not endpoint:
            raise ConnectorValidationError("No endpoint configured for this API target node.")
        if method.upper() not in ("POST", "PUT", "PATCH"):
            raise ConnectorValidationError(f"API target method must be POST, PUT or PATCH — got {method.upper()} (a body-less method would silently drop every record).")
        url = f"{self._base_url()}{endpoint if endpoint.startswith('/') else '/' + endpoint}"
        merged_headers = {**self._auth_headers(), **(headers or {})}
        params = dict(query_params or {})
        batch_size = max(1, int(batch_size or 500))

        records_sent = 0
        batches_sent = 0
        last_status = None
        with httpx.Client(timeout=timeout or DEFAULT_TIMEOUT, auth=self._client_auth()) as client:
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                payload = {records_key: batch} if records_key else batch
                resp = self._request_with_retry(client, method, url, params, merged_headers, payload, retry_max)
                if resp.status_code >= 400:
                    raise ConnectorError(self._scrub(
                        f"{method.upper()} {endpoint} returned HTTP {resp.status_code} on batch "
                        f"{batches_sent + 1} (after {records_sent} record(s) already sent successfully): {resp.text[:300]}"
                    ))
                records_sent += len(batch)
                batches_sent += 1
                last_status = resp.status_code
        return {"batches_sent": batches_sent, "records_sent": records_sent, "last_status": last_status}

    def _request_with_retry(self, client, method, url, params, headers, body, retry_max):
        attempt = 0
        while True:
            try:
                resp = client.request(
                    method.upper(), url, params=params, headers=headers,
                    json=body if method.upper() in ("POST", "PUT", "PATCH") and body else None,
                )
                if resp.status_code >= 500 and attempt < retry_max:
                    attempt += 1
                    time.sleep(min(10.0, 1.5 ** attempt))
                    continue
                return resp
            except httpx.RequestError as exc:
                if attempt >= retry_max:
                    raise ConnectorError(self._scrub(f"Request to {url} failed after {attempt + 1} attempt(s): {exc}")) from exc
                attempt += 1
                time.sleep(min(10.0, 1.5 ** attempt))
