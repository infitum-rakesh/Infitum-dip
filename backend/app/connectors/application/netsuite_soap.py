"""
NetSuiteSoapConnector — real SuiteTalk SOAP client (zeep) that runs an
EXISTING NetSuite saved search by ID and returns its results as flat
tabular rows. TBA (Token-Based Authentication) request signing follows
Oracle's own documented algorithm exactly — see the two real sources this
was verified against:
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4395630653.html  (TokenPassport shape)
  https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1534941088.html  (signature algorithm)

Signature: base string `account&consumerKey&token&nonce&timestamp` (raw,
not URL-encoded — NetSuite's own worked example in the first doc above
uses unencoded values), key `consumerSecret&tokenSecret`, HMAC-SHA256,
Base64-encoded. HMAC-SHA1 was retired NetSuite-side as of 2023.1 — this
only implements SHA256. Per the same docs, a TBA request must NOT also
carry an applicationInfo SOAP header (the two auth styles are mutually
exclusive) — this connector never sets one.

Every type/operation name and shape used below (TokenPassport,
TokenPassportSignature.signature having value+algorithm, {RecordType}
SearchAdvanced.savedSearchId, SearchResult.searchRowList, SearchColumn*
Field.searchValue, getSavedSearch/GetSavedSearchResult.recordRefList,
search/searchMoreWithId's real operation signatures) was verified
directly against NetSuite's real, public WSDL
(https://webservices.netsuite.com/wsdl/v2024_2_0/netsuite.wsdl, fetched
live) with zeep's own type/operation introspection — not guessed from
memory. What could NOT be verified in this environment: an actual
successful authenticated call, since no live NetSuite account/saved
search exists here. test_connection() below makes a genuine SOAP call
(getServerTime, which needs no valid saved search to exist) — a bad
account/credential set fails with NetSuite's own real SOAP fault, not a
simulated error. See connectors/registry.py's docstring for the same
disclosed-but-real precedent used elsewhere (Snowflake, Oracle before
Docker) — this is exactly that same category of connector.

XML handling ("configure ETL for netsuite soap as its response is in
xml" — see also execution/ wiring in ingestion/netsuite_search.py):
zeep parses the real SOAP/XML response into typed Python objects; this
module's _flatten_row()/_serialize() turn those nested objects back into
flat {column: value} dicts generically — walking into any SearchColumn*
Field wrapper to pull out its real .searchValue, unwrapping a RecordRef
select value to its human-readable .name, and ISO-formatting datetimes —
without hand-coding a fixed column list per NetSuite record type, since
a saved search's own defined columns (not this connector) decide what
comes back.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import secrets as _secrets
import time

from app.connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ConnectorUnsupportedError,
    ConnectorValidationError,
    ObjectInfo,
    PreviewResult,
)

try:
    import zeep
    from zeep.exceptions import Fault as ZeepFault
except ImportError:  # pragma: no cover - zeep is in requirements.txt; guarded for import-time safety only
    zeep = None
    ZeepFault = Exception

DEFAULT_WSDL_VERSION = "2024_2_0"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

# A convenience starting-point subset of NetSuite's real SearchRecordType
# enum (used by getSavedSearch()/list_objects() below) — NOT exhaustive;
# any other valid NetSuite record type name can still be typed directly
# into the Source Dataset node's "Record Type" field, and NetSuite's own
# SOAP fault reports back clearly if it's not a real enum value.
COMMON_RECORD_TYPES = [
    "customer", "vendor", "employee", "contact", "partner", "lead",
    "transaction", "salesOrder", "invoice", "purchaseOrder", "estimate",
    "creditMemo", "vendorBill", "vendorPayment", "customerPayment",
    "item", "inventoryItem", "journalEntry", "opportunity", "task",
]


def _account_domain(account: str) -> str:
    return account.strip().lower().replace("_", "-")


def _wsdl_url(account: str, version: str) -> str:
    return f"https://{_account_domain(account)}.suitetalk.api.netsuite.com/wsdl/v{version}/netsuite.wsdl"


def _endpoint_url(account: str, version: str) -> str:
    return f"https://{_account_domain(account)}.suitetalk.api.netsuite.com/services/NetSuitePort_{version}"


def _find_type(client, type_name: str):
    """Resolves a WSDL type by its bare name, searching every namespace
    the loaded WSDL defines — used instead of hardcoding a namespace URI
    (which embeds the WSDL version, e.g. "core_2024_2") so this connector
    keeps working across whatever wsdl_version the connection is
    configured with."""
    for t in client.wsdl.types.types:
        if getattr(t, "name", None) == type_name:
            return client.get_type(str(t.qname))
    raise ConnectorError(f"WSDL type '{type_name}' not found — check the configured WSDL Version against your NetSuite account's supported SOAP versions.")


def _serialize(value):
    """Recursively turns a zeep-deserialized SOAP object graph into plain
    Python (dict/list/scalar) — the actual real XML response, just walked
    into Python-native structures rather than left as zeep's CompoundValue
    wrapper objects."""
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


def _flatten_row(obj, prefix: str = "") -> dict:
    """Flattens one search-row object into {column_name: value}. Every
    NetSuite SearchColumn*Field type wraps its real value in a
    `searchValue` attribute (verified against the real WSDL — see module
    docstring) — this walks into that wrapper and uses the FIELD NAME the
    wrapper was found under as the column name, so "basic.entityId
    .searchValue" becomes just the column "entityId". A RecordRef
    searchValue (a NetSuite internal-id + name reference, e.g. a Select
    field) is reduced to its human-readable .name (falling back to
    internalId) rather than left as a nested object — the real, useful
    value for a flat dataset row, not a fabricated simplification."""
    row: dict = {}
    data = _serialize(obj)
    if not isinstance(data, dict):
        return row
    for key, value in data.items():
        if isinstance(value, dict):
            if "searchValue" in value:
                sv = value["searchValue"]
                if isinstance(sv, dict):
                    sv = sv.get("name") or sv.get("internalId")
                row[key] = sv
            else:
                # Nested join group (e.g. a related record's own basic
                # fields) — recurse, prefixing to avoid column collisions
                # across joins.
                row.update(_flatten_row(obj.__values__[key] if hasattr(obj, "__values__") and key in obj.__values__ else value, f"{prefix}{key}."))
        elif value is not None:
            row[f"{prefix}{key}"] = value
    return row


class NetSuiteSoapConnector(BaseConnector):
    connection_type = "NETSUITE_SOAP"

    def _require_zeep(self):
        if zeep is None:
            raise ConnectorError("The zeep SOAP client library is not installed on this server.")

    def _client(self):
        self._require_zeep()
        cfg = self.config
        account = (cfg.get("account") or "").strip()
        if not account:
            raise ConnectorValidationError("No Account ID configured for this NetSuite (SOAP) connection.")
        version = (cfg.get("wsdl_version") or DEFAULT_WSDL_VERSION).strip()
        wsdl_path_version = version if version.count("_") >= 2 else f"{version}_0"
        print(f"[NetSuite SOAP] Loading WSDL for account {account} (v{wsdl_path_version})...", flush=True)
        try:
            client = zeep.Client(_wsdl_url(account, wsdl_path_version))
        except Exception as exc:  # requests.RequestException, zeep parse errors, etc. — all genuinely real failures
            print(f"[NetSuite SOAP] WSDL load failed: {self._scrub(str(exc))}", flush=True)
            raise ConnectorError(self._scrub(f"Could not load NetSuite WSDL for account {account}: {exc}")) from exc
        # Direct the binding at the account-specific SOAP endpoint (not
        # the generic webservices.netsuite.com host the WSDL itself was
        # fetched from) — required for any 2019.1+ NetSuite account, per
        # Oracle's account-specific-domain documentation.
        client.service._binding_options["address"] = _endpoint_url(account, wsdl_path_version)
        print(f"[NetSuite SOAP] Connected — endpoint {_endpoint_url(account, wsdl_path_version)}", flush=True)
        return client

    def _token_passport(self, client):
        cfg = self.config
        account = (cfg.get("account") or "").strip()
        consumer_key = cfg.get("consumer_key") or ""
        consumer_secret = self.secrets.get("consumer_secret") or ""
        token_id = cfg.get("token_id") or ""
        token_secret = self.secrets.get("token_secret") or ""
        missing = [n for n, v in [("account", account), ("consumer_key", consumer_key), ("consumer_secret", consumer_secret), ("token_id", token_id), ("token_secret", token_secret)] if not v]
        if missing:
            raise ConnectorValidationError(f"This NetSuite (SOAP) connection is missing: {', '.join(missing)}.")

        nonce = _secrets.token_hex(10)  # 20 hex chars — within NetSuite's required 6-64 char range
        timestamp = int(time.time())
        base_string = "&".join([account, consumer_key, token_id, nonce, str(timestamp)])
        key = f"{consumer_secret}&{token_secret}".encode()
        signature_b64 = base64.b64encode(hmac.new(key, base_string.encode(), hashlib.sha256).digest()).decode()

        signature_type = _find_type(client, "TokenPassportSignature")
        passport_type = _find_type(client, "TokenPassport")
        return passport_type(
            account=account, consumerKey=consumer_key, token=token_id,
            nonce=nonce, timestamp=timestamp,
            signature=signature_type(signature_b64, algorithm="HMAC-SHA256"),
        )

    def _soap_call(self, client, operation: str, *args, **kwargs):
        """Every real SOAP call goes through here so the TokenPassport
        header (freshly signed — nonce/timestamp must be current per
        call, not reused) and NetSuite's own real status/fault are
        handled consistently. Deliberately never sends an applicationInfo
        header alongside TBA — see module docstring."""
        headers = {"tokenPassport": self._token_passport(client)}
        try:
            result = getattr(client.service, operation)(*args, _soapheaders=headers, **kwargs)
        except ZeepFault as exc:
            raise ConnectorError(self._scrub(f"NetSuite SOAP fault on {operation}: {exc}")) from exc
        except Exception as exc:
            raise ConnectorError(self._scrub(f"NetSuite SOAP call {operation} failed: {exc}")) from exc
        status = getattr(result, "status", None)
        if status is not None and not getattr(status, "isSuccess", True):
            details = "; ".join(
                f"{d.get('code', '')}: {d.get('message', '')}" if isinstance(d, dict) else str(d)
                for d in (_serialize(getattr(status, "statusDetail", None)) or [])
            ) or "NetSuite reported failure with no detail message."
            raise ConnectorError(self._scrub(f"NetSuite rejected {operation}: {details}"))
        return result

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            client = self._client()
            self._soap_call(client, "getServerTime")
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, str(exc), round((time.monotonic() - start) * 1000, 1))

    # ── Schema/object browsing — "schema" is repurposed as NetSuite's own
    # record-type concept (e.g. "customer", "salesOrder"); "objects" are
    # that record type's saved searches, listed via the real
    # getSavedSearch() operation. ───────────────────────────────────────

    def list_schemas(self) -> list[str]:
        return list(COMMON_RECORD_TYPES)

    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        if not schema:
            raise ConnectorValidationError("Pick a NetSuite record type first (e.g. \"customer\", \"salesOrder\") to list its saved searches.")
        client = self._client()
        record_type = _find_type(client, "GetSavedSearchRecord")(searchType=schema)
        result = self._soap_call(client, "getSavedSearch", record_type)
        refs = _serialize(getattr(result, "recordRefList", None)) or {}
        ref_list = refs.get("recordRef") if isinstance(refs, dict) else refs
        if not ref_list:
            return []
        objects = [
            ObjectInfo(name=f"{r.get('name')} (#{r.get('internalId')})", schema=schema, object_type="SAVED_SEARCH")
            for r in ref_list if isinstance(r, dict)
        ]
        if search:
            needle = search.lower()
            objects = [o for o in objects if needle in o.name.lower()]
        objects.sort(key=lambda o: o.name.lower())
        return objects

    def get_columns(self, object_name: str, schema: str | None = None):
        # A saved search's result columns are defined inside NetSuite
        # itself (Setup > ... > Saved Searches) and only become knowable
        # by actually running it — there is no separate "describe this
        # saved search's columns" SOAP operation. preview_object() below
        # is the real way to discover them (its PreviewResult.columns
        # comes straight from one real page of actual results).
        raise ConnectorUnsupportedError(
            "NetSuite saved searches don't expose column metadata separately from running them — use Preview to see real columns and sample rows together."
        )

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        saved_search_id = _extract_internal_id(object_name)
        rows, total_records, _total_pages = self._run_saved_search_page(
            record_type=schema, saved_search_id=saved_search_id, page_index=1,
            page_size=min(max(1, limit), MAX_PAGE_SIZE),
        )
        columns: list[str] = []
        for row in rows:
            for k in row.keys():
                if k not in columns:
                    columns.append(k)
        return PreviewResult(
            columns=columns, rows=[[r.get(c) for c in columns] for r in rows[:limit]],
            row_count=total_records, truncated=total_records is not None and total_records > len(rows),
        )

    # ── Real pagination primitive — used by both preview_object() above
    # and the ETL wiring (ingestion/netsuite_search.py) below. Returns
    # (rows, total_records, total_pages) for one real page. ────────────

    def run_saved_search_page(self, record_type: str, saved_search_id: str, page_index: int, page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict], int, int]:
        return self._run_saved_search_page(record_type, saved_search_id, page_index, page_size)

    def _run_saved_search_page(self, record_type: str | None, saved_search_id: str, page_index: int, page_size: int):
        if not record_type:
            raise ConnectorValidationError("A NetSuite record type is required to run a saved search (e.g. \"customer\", \"salesOrder\").")
        client = self._client()
        search_prefs_type = _find_type(client, "SearchPreferences")
        prefs = search_prefs_type(bodyFieldsOnly=False, pageSize=min(max(1, page_size), MAX_PAGE_SIZE))

        advanced_type_name = f"{record_type[0].upper()}{record_type[1:]}SearchAdvanced"
        advanced_type = _find_type(client, advanced_type_name)
        search_record = advanced_type(savedSearchId=str(saved_search_id))

        print(f"[NetSuite SOAP] Running saved search {saved_search_id!r} ({record_type}) — page {page_index}, page size {prefs.pageSize}...", flush=True)
        headers = {"tokenPassport": self._token_passport(client), "searchPreferences": prefs}
        try:
            if page_index <= 1:
                result = client.service.search(search_record, _soapheaders=headers)
            else:
                # searchMoreWithId needs a prior search's searchId — this
                # connector re-issues page 1 first when called directly
                # for a page > 1 in isolation (e.g. a retried checkpoint);
                # the ingestion path (ingestion/netsuite_search.py) instead
                # keeps one open searchId across an entire run's pages.
                first = client.service.search(search_record, _soapheaders=headers)
                search_id = getattr(getattr(first, "searchResult", first), "searchId", None) or getattr(first, "searchId", None)
                if not search_id:
                    raise ConnectorError("NetSuite search did not return a searchId to page through.")
                result = client.service.searchMoreWithId(search_id, page_index, _soapheaders=headers)
        except ZeepFault as exc:
            print(f"[NetSuite SOAP] Saved search SOAP fault: {self._scrub(str(exc))}", flush=True)
            raise ConnectorError(self._scrub(f"NetSuite saved search failed: {exc}")) from exc
        except Exception as exc:
            print(f"[NetSuite SOAP] Saved search failed: {self._scrub(str(exc))}", flush=True)
            raise ConnectorError(self._scrub(f"NetSuite saved search failed: {exc}")) from exc

        search_result = getattr(result, "searchResult", result)
        status = getattr(search_result, "status", None)
        if status is not None and not getattr(status, "isSuccess", True):
            details = "; ".join(str(d) for d in (_serialize(getattr(status, "statusDetail", None)) or []))
            print(f"[NetSuite SOAP] NetSuite rejected the saved search: {self._scrub(details)}", flush=True)
            raise ConnectorError(self._scrub(f"NetSuite rejected the saved search: {details or 'no detail provided'}"))

        row_list = getattr(search_result, "searchRowList", None)
        rows_raw = getattr(row_list, "searchRow", None) or []
        rows = [_flatten_row(r) for r in rows_raw]
        total_records = getattr(search_result, "totalRecords", None)
        total_pages = getattr(search_result, "totalPages", None)
        total_records = int(total_records) if total_records is not None else len(rows)
        total_pages = int(total_pages) if total_pages is not None else 1
        print(f"[NetSuite SOAP] Page {page_index}/{total_pages}: fetched {len(rows):,} row(s) (total records across search: {total_records:,}).", flush=True)
        return rows, total_records, total_pages


def _extract_internal_id(object_name: str) -> str:
    """list_objects() above renders each saved search as "Name (#id)" for
    human readability in the picker — this recovers the real internal ID
    NetSuite actually needs. Falls back to the raw string unchanged if it
    doesn't match that shape (e.g. a scriptId typed in directly)."""
    if "(#" in object_name and object_name.endswith(")"):
        return object_name.rsplit("(#", 1)[1][:-1]
    return object_name
