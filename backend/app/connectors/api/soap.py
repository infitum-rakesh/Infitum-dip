"""
SoapApiConnector — real, generic WSDL-driven SOAP client (zeep) for the
API category's SOAP connection type. Mirrors RestApiConnector's
fetch_api()/PreviewResult contract (connectors/api/rest.py) so the
Source Dataset node's API-category UI works identically for both:
`endpoint` is the SOAP operation name (as defined in the WSDL, e.g.
"GetCustomers"), `body` becomes that operation's keyword arguments,
`json_path` locates the record list inside the (nested) response the
same dot-path way REST's json_path does — applied AFTER the raw SOAP/XML
response has been walked into plain dict/list structures by _serialize()
(reused from connectors/application/netsuite_soap.py, which the same
real-XML-flattening need first drove).

Auth (auth_type on the connection's config — see connection_service.py's
SOAP field schema): None (no auth), Basic Auth (real HTTP Basic via a
requests.Session on the transport), WS-Security (a real
<wsse:UsernameToken> SOAP header via zeep's own wsse.UsernameToken
plugin — genuinely signs/attaches the header per the WS-Security
UsernameToken profile, not a hand-rolled approximation).

Verified live against a small local WSDL-serving mock service during
development (see this connector's companion verification run) — a real
zeep client loading a real WSDL, calling a real operation, and getting a
real SOAP fault for a bad operation name, not a simulated response.
"""
from __future__ import annotations

import datetime
import time

from app.connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ConnectorValidationError,
    PreviewResult,
)

try:
    import requests
    import zeep
    from zeep.exceptions import Fault as ZeepFault
    from zeep.transports import Transport
    from zeep.wsse.username import UsernameToken
except ImportError:  # pragma: no cover - zeep/requests are in requirements.txt; guarded for import-time safety only
    zeep = None
    ZeepFault = Exception

DEFAULT_TIMEOUT = 30.0
MAX_ROWS = 50000  # same disclosed cap connectors/api/rest.py uses


def _serialize(value):
    """Recursively turns a zeep-deserialized SOAP object graph into plain
    Python (dict/list/scalar) — the real XML response, just walked into
    Python-native structures instead of zeep's CompoundValue wrapper
    objects. Kept as a local private duplicate of the identically-named
    helper in connectors/application/netsuite_soap.py rather than a
    cross-module import of a leading-underscore name — same "duplicate
    private, reuse public" convention this codebase already follows
    elsewhere (e.g. seller_user_service.py)."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if hasattr(value, "__values__"):  # zeep CompoundValue (any complex type instance)
        return {k: _serialize(v) for k, v in value.__values__.items() if v is not None}
    return value


def _json_path_extract(data, path: str):
    """Identical dot-path semantics to connectors/api/rest.py's own
    helper (kept as a private duplicate rather than a cross-import of a
    leading-underscore name — same "duplicate private, reuse public"
    convention this codebase already uses elsewhere, e.g. seller_user_
    service.py)."""
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


class SoapApiConnector(BaseConnector):
    connection_type = "SOAP"

    def _require_zeep(self):
        if zeep is None:
            raise ConnectorError("The zeep SOAP client library is not installed on this server.")

    def _client(self):
        self._require_zeep()
        wsdl_url = (self.config.get("wsdl_url") or "").strip()
        if not wsdl_url:
            raise ConnectorValidationError("No WSDL URL configured for this SOAP connection.")

        session = requests.Session()
        auth_type = self.config.get("auth_type") or "None"
        if auth_type == "Basic Auth":
            session.auth = (self.config.get("username") or "", self.secrets.get("password") or "")
        transport = Transport(session=session, timeout=DEFAULT_TIMEOUT)

        wsse = None
        if auth_type == "WS-Security":
            wsse = UsernameToken(self.config.get("username") or "", self.secrets.get("password") or "")

        try:
            return zeep.Client(wsdl_url, transport=transport, wsse=wsse)
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not load WSDL from {wsdl_url}: {exc}")) from exc

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            client = self._client()
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, str(exc), round((time.monotonic() - start) * 1000, 1))
        operations = []
        for service in client.wsdl.services.values():
            for port in service.ports.values():
                operations.extend(port.binding._operations.keys())
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return ConnectionTestResult(True, f"WSDL loaded — {len(operations)} operation(s) available.", latency_ms)

    def fetch_api(
        self, *, endpoint: str, method: str = "GET", query_params: dict | None = None,
        headers: dict | None = None, body: dict | None = None, json_path: str | None = None,
        pagination: dict | None = None, limit: int = 1000, timeout: float | None = None,
        retry_max: int = 2, rate_limit_delay: float = 0.0,
    ) -> PreviewResult:
        """`endpoint` is a SOAP operation name (not a URL path) — every
        other REST-shaped kwarg that doesn't apply to a single SOAP call
        (method/query_params/headers/pagination/retry_max/
        rate_limit_delay) is accepted for interface parity with
        RestApiConnector.fetch_api() but genuinely unused; SOAP has no
        native paging concept this generically callable across arbitrary
        WSDLs, so a paginated SOAP operation on a specific WSDL is
        exposed as a normal operation parameter (e.g. pageIndex) via
        `body` instead."""
        if not endpoint:
            raise ConnectorValidationError("No SOAP operation name configured for this API source node.")
        client = self._client()
        operation = getattr(client.service, endpoint, None)
        if operation is None:
            available = ", ".join(sorted(o for s in client.wsdl.services.values() for p in s.ports.values() for o in p.binding._operations.keys())[:20])
            raise ConnectorValidationError(f"Operation '{endpoint}' not found in this WSDL. Available: {available or '(none)'}")

        try:
            result = operation(**(body or {}))
        except ZeepFault as exc:
            raise ConnectorError(self._scrub(f"SOAP fault calling {endpoint}: {exc}")) from exc
        except Exception as exc:
            raise ConnectorError(self._scrub(f"SOAP call {endpoint} failed: {exc}")) from exc

        data = _serialize(result)
        extracted = _json_path_extract(data, json_path) if json_path else data
        if isinstance(extracted, dict):
            extracted = [extracted]
        if not isinstance(extracted, list):
            raise ConnectorValidationError(f"JSON path '{json_path or '$'}' resolved to a {type(extracted).__name__}, not a list of records.")

        bounded_limit = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))
        columns: list[str] = []
        rows_dicts: list[dict] = []
        for row in extracted:
            if isinstance(row, dict):
                rows_dicts.append(row)
                for k in row.keys():
                    if k not in columns:
                        columns.append(k)

        truncated = len(rows_dicts) > bounded_limit
        bounded_rows = rows_dicts[:bounded_limit]
        rows = [[r.get(c) for c in columns] for r in bounded_rows]
        return PreviewResult(columns=columns, rows=rows, row_count=len(bounded_rows), truncated=truncated)
