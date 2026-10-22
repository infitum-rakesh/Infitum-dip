"""
MockVerificationProvider — real HTTP client against the local mock
verification gateway (scripts/mock_verification_gateway.py). This is the
ONLY verification-provider connector today: no UIDAI AUA/KUA license, no
GSTN/MCA-authorized data-provider contract, and no KYC-aggregator account
(Karza/Signzy/IDfy/...) exist in this environment, so every real check
would have nothing genuine to call — see this module's own docstring
disclosure pattern as SAP/Fabric/Salesforce/Snowflake/the three OCR
providers all already use in this codebase (real code, honestly
unverified against a live account vs. this one, which IS live-verified,
just against a local mock rather than a production vendor — the same
"TallyPrime vs. its mock gateway" precedent).

Wiring a real vendor later means: add its ConnectionType + FIELD_SCHEMAS
entry, write one new connector file implementing the same verify_pan()/
verify_gstin()/... contract against that vendor's real API, and register
it in connectors/registry.py — nothing in services/compliance/* changes.
"""
from __future__ import annotations

import time

import httpx

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError

DEFAULT_TIMEOUT = 15.0


class MockVerificationProvider(BaseConnector):
    connection_type = "MOCK_VERIFICATION_PROVIDER"

    def _base_url(self) -> str:
        return (self.config.get("base_url") or "http://localhost:9601").rstrip("/")

    def _timeout(self) -> float:
        try:
            return float(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                resp = client.get(f"{self._base_url()}/health")
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return ConnectionTestResult(True, "Mock verification gateway reachable.", latency_ms)
            return ConnectionTestResult(False, f"Gateway responded with HTTP {resp.status_code}.", latency_ms)
        except httpx.HTTPError as exc:
            return ConnectionTestResult(False, self._scrub(f"Could not reach mock verification gateway: {exc}"),
                                         round((time.monotonic() - start) * 1000, 1))

    def _post(self, path: str, payload: dict, *, unavailable_shape: dict) -> dict:
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                resp = client.post(f"{self._base_url()}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise ConnectorError(self._scrub(f"Verification call to {path} failed: {exc}")) from exc
        if resp.status_code == 503:
            return unavailable_shape
        if resp.status_code != 200:
            raise ConnectorError(self._scrub(f"Gateway returned HTTP {resp.status_code} for {path}."))
        return resp.json()

    def verify_pan(self, pan: str, *, submitted_name: str | None = None) -> dict:
        return self._post(
            "/verify/pan", {"pan": pan, "submitted_name": submitted_name},
            unavailable_shape={"status": "UNAVAILABLE", "holder_name": None, "entity_type": None, "reference_id": None, "verified_at": None},
        )

    def verify_gstin(self, gstin: str, *, submitted_name: str | None = None) -> dict:
        return self._post(
            "/verify/gstin", {"gstin": gstin, "submitted_name": submitted_name},
            unavailable_shape={"status": "UNAVAILABLE", "legal_name": None, "trade_name": None, "registration_status": None, "reference_id": None, "verified_at": None},
        )

    def verify_tan(self, tan: str, *, submitted_name: str | None = None) -> dict:
        return self._post(
            "/verify/tan", {"tan": tan, "submitted_name": submitted_name},
            unavailable_shape={"status": "UNAVAILABLE", "deductor_name": None, "entity_type": None, "reference_id": None, "verified_at": None},
        )

    def search_mca_company(self, *, cin: str | None = None, llpin: str | None = None, legal_name: str | None = None) -> dict:
        # llpin isn't seeded in this mock yet (no LLP records) — passed
        # through for interface completeness, the gateway simply won't
        # match anything for it today.
        return self._post(
            "/search/mca", {"cin": cin, "llpin": llpin, "legal_name": legal_name},
            unavailable_shape={"status": "UNAVAILABLE", "matches": []},
        )

    def verify_udyam(self, udyam_number: str, *, submitted_name: str | None = None) -> dict:
        return self._post(
            "/verify/udyam", {"udyam_number": udyam_number, "submitted_name": submitted_name},
            unavailable_shape={"status": "UNAVAILABLE", "enterprise_name": None, "classification": None, "reference_id": None, "verified_at": None},
        )
