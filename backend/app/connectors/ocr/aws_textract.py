"""
AwsTextractConnector — real Amazon Textract OCR via boto3 (the official
AWS SDK), the same SDK family as connectors/cloud/aws_s3.py. Not live-
verified — Textract's synchronous DetectDocumentText API has no free/
local emulator (LocalStack's Textract support is a paid Pro feature, not
available in this environment); this is real code against boto3's own
documented Textract client contract, the same disclosed-but-real
precedent as SNOWFLAKE/SAP/FABRIC/SALESFORCE (see connectors/registry.py).

Config shape (see connection_service.py's AWS_TEXTRACT field schema):
region, access_key_id, secret_access_key, optional session_token (for
temporary STS credentials).

Only DetectDocumentText (plain OCR — text + per-line bounding boxes +
confidence) is used, not AnalyzeDocument's forms/tables features — this
module's job is page text extraction, matching what the Tesseract path
(ocr/engine.py) and the other two cloud OCR connectors all provide;
callers needing structured key-value/table extraction from a form should
use the AI Extraction pipeline (services/extraction_service.py) on top of
whichever OCR text this produces, same as they would for Tesseract.
"""
from __future__ import annotations

from io import BytesIO

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError, ConnectorValidationError


class AwsTextractConnector(BaseConnector):
    connection_type = "AWS_TEXTRACT"

    def _client(self):
        region = (self.config.get("region") or "us-east-1").strip()
        access_key = self.secrets.get("access_key_id") or None
        secret_key = self.secrets.get("secret_access_key") or None
        session_token = self.secrets.get("session_token") or None
        try:
            return boto3.client(
                "textract", region_name=region, aws_access_key_id=access_key,
                aws_secret_access_key=secret_key, aws_session_token=session_token,
            )
        except (BotoCoreError, ValueError) as exc:
            raise ConnectorError(self._scrub(f"Could not create Textract client: {exc}")) from exc

    def test_connection(self) -> ConnectionTestResult:
        # Textract has no cheap "ping" call — a 1x1 white PNG through the
        # real DetectDocumentText API is the smallest genuine request that
        # actually exercises the configured region/credentials, the same
        # approach a "run a trivial real query" DB connector test uses.
        import time
        start = time.monotonic()
        try:
            from PIL import Image
            buf = BytesIO()
            # Textract also documents a 50x50 pixel minimum for the image
            # dimensions it will accept — same real-world constraint that
            # broke the Azure Document Intelligence test with a 32x32
            # probe image; kept above that floor here too.
            Image.new("RGB", (200, 200), "white").save(buf, format="PNG")
            client = self._client()
            client.detect_document_text(Document={"Bytes": buf.getvalue()})
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(True, "Connection successful.", latency_ms)
        except (ClientError, BotoCoreError) as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))
        except (ConnectorError, ConnectorValidationError) as exc:
            # _client() wraps a client-construction failure (e.g. a
            # malformed region raising ValueError) as ConnectorError —
            # without this, that would propagate uncaught instead of the
            # graceful ConnectionTestResult(False, ...) every other
            # failure path here returns. Same bug class just fixed in
            # oracle_erp.py's test_connection() (Phase 11); Google Vision
            # and Azure Document Intelligence's sibling connectors in this
            # package already catch this broadly — this brings AWS
            # Textract in line with that established local convention.
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))
        except Exception as exc:
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    def ocr(self, image_bytes: bytes, *, mime_type: str = "image/png") -> dict:
        try:
            from PIL import Image
            with Image.open(BytesIO(image_bytes)) as img:
                width, height = img.size
        except Exception as exc:
            raise ConnectorError(f"Could not read page image dimensions: {exc}") from exc

        try:
            client = self._client()
            response = client.detect_document_text(Document={"Bytes": image_bytes})
        except (ClientError, BotoCoreError) as exc:
            raise ConnectorError(self._scrub(f"AWS Textract OCR failed: {exc}")) from exc

        regions = []
        texts = []
        for block in response.get("Blocks", []):
            if block.get("BlockType") != "LINE":
                continue  # WORD/PAGE blocks are redundant with LINE for this shape — same granularity Tesseract's regions use
            text = (block.get("Text") or "").strip()
            if not text:
                continue
            box = block.get("Geometry", {}).get("BoundingBox") or {}
            # Textract's BoundingBox is normalized 0-1 fractions of the page —
            # convert to pixel ints to match ocr/engine.py's Tesseract bbox
            # convention ([x, y, w, h] in real pixels of the page image sent).
            bbox = [
                round((box.get("Left") or 0) * width),
                round((box.get("Top") or 0) * height),
                round((box.get("Width") or 0) * width),
                round((box.get("Height") or 0) * height),
            ]
            regions.append({"text": text, "bbox": bbox, "confidence": round(block.get("Confidence") or 0.0, 1)})
            texts.append(text)

        confidences = [r["confidence"] for r in regions]
        overall = round(sum(confidences) / len(confidences), 1) if confidences else None
        return {"full_text": "\n".join(texts), "confidence": overall, "regions": regions}
