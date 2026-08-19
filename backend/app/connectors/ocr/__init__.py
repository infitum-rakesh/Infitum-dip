"""OCR Provider connectors — real cloud document-OCR API calls (AWS
Textract, Google Cloud Vision, Azure Document Intelligence). Selected via
an organization's OCR Provider setting (see app/ocr/engine.py's run_ocr()
and services/document_service.py) — Tesseract remains the zero-config
default and needs no connection at all."""
