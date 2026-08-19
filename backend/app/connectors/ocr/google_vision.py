"""
GoogleVisionConnector — real Google Cloud Vision OCR via the official
google-cloud-vision SDK. Not live-verified — no GCP project/service
account exists in this environment and there's no free/local Vision API
emulator; this is real code against the SDK's own documented
ImageAnnotatorClient contract, the same disclosed-but-real precedent as
SNOWFLAKE/SAP/FABRIC/SALESFORCE (see connectors/registry.py).

Config shape (see connection_service.py's GOOGLE_VISION field schema):
service_account_json — the full contents of a GCP service account JSON
key, pasted by the user and stored as a secret (never written to disk on
this server; loaded straight from the decrypted string into
google.oauth2.service_account.Credentials.from_service_account_info()).

Uses document_text_detection (Google's dense-text/document-optimized OCR
mode, distinct from the sparser text_detection used for photos of a few
words) — the right mode for scanned business documents, matching what the
Tesseract path and the other two cloud OCR connectors are all used for.
"""
from __future__ import annotations

import json
import time

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError, ConnectorValidationError


class GoogleVisionConnector(BaseConnector):
    connection_type = "GOOGLE_VISION"

    def _credentials(self):
        raw = self.secrets.get("service_account_json") or ""
        if not raw.strip():
            raise ConnectorValidationError("No Google service account JSON key configured for this connection.")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectorValidationError(f"Service account JSON key isn't valid JSON: {exc}") from exc
        try:
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_info(info)
        except Exception as exc:
            raise ConnectorValidationError(f"Could not load Google service account credentials: {exc}") from exc

    def _client(self):
        try:
            from google.cloud import vision
        except ImportError as exc:  # pragma: no cover - environment issue, not a code bug
            raise ConnectorError("google-cloud-vision is not installed.") from exc
        credentials = self._credentials()
        try:
            return vision.ImageAnnotatorClient(credentials=credentials), vision
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not create Google Vision client: {exc}")) from exc

    def test_connection(self) -> ConnectionTestResult:
        from io import BytesIO
        start = time.monotonic()
        try:
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
            client, vision = self._client()
            image = vision.Image(content=buf.getvalue())
            response = client.document_text_detection(image=image)
            if response.error and response.error.message:
                raise ConnectorError(response.error.message)
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))
        except Exception as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def ocr(self, image_bytes: bytes, *, mime_type: str = "image/png") -> dict:
        client, vision = self._client()
        image = vision.Image(content=image_bytes)
        try:
            response = client.document_text_detection(image=image)
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Google Vision OCR failed: {exc}")) from exc
        if response.error and response.error.message:
            raise ConnectorError(self._scrub(f"Google Vision OCR failed: {response.error.message}"))

        regions = []
        texts = []
        annotation = response.full_text_annotation
        for page in annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    words = []
                    confidences = []
                    for word in paragraph.words:
                        word_text = "".join(symbol.text for symbol in word.symbols)
                        if word_text:
                            words.append(word_text)
                        if word.confidence:
                            confidences.append(word.confidence)
                    text = " ".join(words).strip()
                    if not text:
                        continue
                    vertices = paragraph.bounding_box.vertices
                    xs = [v.x for v in vertices]
                    ys = [v.y for v in vertices]
                    bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
                    # Google's confidence is a 0.0-1.0 fraction — scaled to
                    # 0-100 to match Tesseract/Textract's convention.
                    conf = round((sum(confidences) / len(confidences)) * 100, 1) if confidences else None
                    regions.append({"text": text, "bbox": bbox, "confidence": conf if conf is not None else 0.0})
                    texts.append(text)

        confidences = [r["confidence"] for r in regions if r["confidence"]]
        overall = round(sum(confidences) / len(confidences), 1) if confidences else None
        full_text = annotation.text if annotation.text else "\n".join(texts)
        return {"full_text": full_text, "confidence": overall, "regions": regions}
