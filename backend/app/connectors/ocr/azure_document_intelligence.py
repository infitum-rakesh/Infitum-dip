"""
AzureDocumentIntelligenceConnector — real Azure AI Document Intelligence
OCR (the current name for what was "Form Recognizer"; the connection type
keeps the AZURE_FORM_RECOGNIZER name since that's still the vendor's
better-known product name) via the official azure-ai-documentintelligence
SDK. Not live-verified — no Azure subscription/Document Intelligence
resource exists in this environment and there's no free/local emulator;
this is real code against the SDK's own documented DocumentIntelligence-
Client contract, the same disclosed-but-real precedent as SNOWFLAKE/SAP/
FABRIC/SALESFORCE (see connectors/registry.py) — and the same auth shape
(endpoint + API key) connectors/cloud/azure_blob.py already uses for
Azure Blob Storage.

Config shape (see connection_service.py's AZURE_FORM_RECOGNIZER field
schema): endpoint, api_key.

Uses the "prebuilt-read" model — pure OCR (text + per-line polygons +
per-word confidence), not prebuilt-layout/prebuilt-document's structured
forms/tables extraction, matching what the Tesseract path and the other
two cloud OCR connectors are all used for.
"""
from __future__ import annotations

import time

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError, ConnectorValidationError


class AzureDocumentIntelligenceConnector(BaseConnector):
    connection_type = "AZURE_FORM_RECOGNIZER"

    def _client(self):
        endpoint = (self.config.get("endpoint") or "").strip()
        api_key = self.secrets.get("api_key") or ""
        if not endpoint:
            raise ConnectorValidationError("No Endpoint configured for this Azure Document Intelligence connection.")
        if not api_key:
            raise ConnectorValidationError("No API Key configured for this Azure Document Intelligence connection.")
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:  # pragma: no cover - environment issue, not a code bug
            raise ConnectorError("azure-ai-documentintelligence is not installed.") from exc
        try:
            return DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(api_key))
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not create Document Intelligence client: {exc}")) from exc

    def _analyze(self, image_bytes: bytes):
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.exceptions import HttpResponseError

        client = self._client()
        try:
            poller = client.begin_analyze_document("prebuilt-read", AnalyzeDocumentRequest(bytes_source=image_bytes))
            return poller.result()
        except HttpResponseError as exc:
            raise ConnectorError(self._scrub(f"Azure Document Intelligence OCR failed: {exc.message or exc}")) from exc

    def test_connection(self) -> ConnectionTestResult:
        from io import BytesIO
        start = time.monotonic()
        try:
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
            self._analyze(buf.getvalue())
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))
        except Exception as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def ocr(self, image_bytes: bytes, *, mime_type: str = "image/png") -> dict:
        result = self._analyze(image_bytes)

        regions = []
        texts = []
        for page in result.pages or []:
            # Word confidences aren't attached to lines directly in this
            # API — matched here via each word's span offset falling
            # inside the line's own span range, so a line's reported
            # confidence is a genuine average of the words that make it
            # up, not a fabricated or copied value.
            words = page.words or []
            for line in page.lines or []:
                text = (line.content or "").strip()
                if not text:
                    continue
                line_span = line.spans[0] if line.spans else None
                confidences = []
                if line_span is not None:
                    lo, hi = line_span.offset, line_span.offset + line_span.length
                    confidences = [
                        w.confidence for w in words
                        if w.confidence is not None and w.span is not None and lo <= w.span.offset < hi
                    ]
                polygon = line.polygon or []
                xs = polygon[0::2]
                ys = polygon[1::2]
                bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)] if xs and ys else [0, 0, 0, 0]
                conf = round((sum(confidences) / len(confidences)) * 100, 1) if confidences else None
                regions.append({"text": text, "bbox": [round(v) for v in bbox], "confidence": conf if conf is not None else 0.0})
                texts.append(text)

        confidences = [r["confidence"] for r in regions if r["confidence"]]
        overall = round(sum(confidences) / len(confidences), 1) if confidences else None
        full_text = result.content if result.content else "\n".join(texts)
        return {"full_text": full_text, "confidence": overall, "regions": regions}
