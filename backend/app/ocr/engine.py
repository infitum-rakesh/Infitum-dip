"""
Real OCR engine for the Documents module (Phase: "real OCR extraction").

Replaces the fabricated OCR stage in document_service.process_document()
(which returned the same "Document processed via OCR engine..." canned
sentence for every file). This module actually opens the uploaded file,
rasterizes PDF pages with PyMuPDF (no poppler dependency), and runs real
text recognition via Tesseract (pytesseract) — genuine extracted text,
per-word confidence scores, and bounding boxes computed from the file
that was actually uploaded.

Requires the `tesseract` binary on PATH (`brew install tesseract` on
macOS — not pip-installable; pytesseract just shells out to it). Raises
OcrEngineError with a real, specific message on any failure (missing
binary, corrupt file, empty page) rather than ever falling back to
fabricated output — a document that can't be OCR'd should end up
FAILED with a real error, not silently "succeed" with fake text.

Only PDF and image documents are OCR'd — Word/Excel/text files carry
their content natively and don't need image-to-text recognition; see
OcrNotApplicableError.
"""
from dataclasses import dataclass
from io import BytesIO

from PIL import Image
import pytesseract

from app.models.document import DocumentType

RENDER_DPI = 220  # good balance of OCR accuracy vs. memory/time for A4-ish pages


class OcrEngineError(Exception):
    """Real OCR failure — missing tesseract binary, corrupt/unreadable file, etc."""


class OcrNotApplicableError(Exception):
    """Raised for document types OCR doesn't apply to (Word/Excel/text/other)."""


@dataclass
class _PageImage:
    page_num: int
    image: "Image.Image"


def _check_tesseract_available() -> None:
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover - environment issue, not a code bug
        raise OcrEngineError(
            "The tesseract OCR binary is not available on this server "
            "(install it, e.g. `brew install tesseract` on macOS, and ensure it's on PATH)."
        ) from exc


def _load_page_images(file_bytes: bytes, doc_type: DocumentType) -> list[_PageImage]:
    if doc_type == DocumentType.IMAGE:
        try:
            img = Image.open(BytesIO(file_bytes))
            img.load()
        except Exception as exc:
            raise OcrEngineError(f"Could not open image file: {exc}") from exc
        return [_PageImage(page_num=1, image=img.convert("RGB"))]

    if doc_type == DocumentType.PDF:
        try:
            import pymupdf  # local import — only needed for the PDF path
        except ImportError as exc:  # pragma: no cover
            raise OcrEngineError("pymupdf is not installed — required to render PDF pages for OCR.") from exc
        try:
            pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
        except Exception as exc:
            raise OcrEngineError(f"Could not open PDF file: {exc}") from exc
        if pdf.page_count == 0:
            pdf.close()
            raise OcrEngineError("PDF has no pages.")
        pages = []
        zoom = RENDER_DPI / 72.0  # PDF units are 72 DPI by definition
        matrix = pymupdf.Matrix(zoom, zoom)
        for i in range(pdf.page_count):
            pix = pdf[i].get_pixmap(matrix=matrix)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(_PageImage(page_num=i + 1, image=img))
        pdf.close()
        return pages

    raise OcrNotApplicableError(
        f"OCR does not apply to {doc_type.value} documents — this format carries its content natively."
    )


def _extract_page(page: _PageImage) -> dict:
    """Runs Tesseract on one page image. Returns word-level data grouped
    into lines (block_num/par_num/line_num), since one region per single
    word would be too noisy for a bounding-box UI overlay."""
    data = pytesseract.image_to_data(page.image, output_type=pytesseract.Output.DICT)
    n = len(data["text"])

    lines: dict[tuple, dict] = {}
    for i in range(n):
        word = data["text"][i].strip()
        if not word:
            continue
        conf = float(data["conf"][i])
        if conf < 0:  # Tesseract's sentinel for "not a text detection"
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        if key not in lines:
            lines[key] = {"words": [], "left": left, "top": top, "right": left + w, "bottom": top + h, "confs": []}
        line = lines[key]
        line["words"].append(word)
        line["left"] = min(line["left"], left)
        line["top"] = min(line["top"], top)
        line["right"] = max(line["right"], left + w)
        line["bottom"] = max(line["bottom"], top + h)
        line["confs"].append(conf)

    regions = []
    for line in lines.values():
        text = " ".join(line["words"])
        avg_conf = round(sum(line["confs"]) / len(line["confs"]), 1)
        regions.append({
            "page": page.page_num,
            "text": text,
            "bbox": [line["left"], line["top"], line["right"] - line["left"], line["bottom"] - line["top"]],
            "confidence": avg_conf,
        })

    full_text = "\n".join(r["text"] for r in regions)
    return {
        "regions": regions,
        "full_text": full_text,
        "width": page.image.width,
        "height": page.image.height,
    }


def render_pages_as_png(file_bytes: bytes, doc_type: DocumentType) -> list[tuple[int, bytes, int, int]]:
    """Renders each page as PNG bytes — (page_num, png_bytes, width, height)
    — for the AI-vision OCR fallback (services/document_service.py). Reuses
    the exact same _load_page_images() PDF rasterization / image-loading
    path run_ocr() uses for Tesseract, so a vision model sees pixel-
    identical pages to what OCR actually attempted, not a separately
    re-rendered/re-encoded version. Raises the same OcrNotApplicableError/
    OcrEngineError run_ocr() would for a non-PDF/IMAGE doc type or a
    corrupt file — the fallback only ever applies where OCR conceptually
    applies too."""
    pages = _load_page_images(file_bytes, doc_type)
    out = []
    for p in pages:
        buf = BytesIO()
        p.image.save(buf, format="PNG")
        out.append((p.page_num, buf.getvalue(), p.image.width, p.image.height))
    return out


def run_ocr(file_bytes: bytes, doc_type: DocumentType) -> dict:
    """
    Real, synchronous OCR — call via asyncio.to_thread() from async code
    (Tesseract is a blocking subprocess call under the hood).

    Returns: {
      "full_text": str, "confidence": float | None,
      "regions": [{"page", "text", "bbox": [x,y,w,h], "confidence"}],
      "pages": [{"page", "width", "height"}],
      "page_count": int,
    }
    Raises OcrEngineError / OcrNotApplicableError on failure.
    """
    _check_tesseract_available()
    pages = _load_page_images(file_bytes, doc_type)

    all_regions = []
    all_texts = []
    all_confs = []
    page_dims = []
    for page in pages:
        result = _extract_page(page)
        all_regions.extend(result["regions"])
        if result["full_text"]:
            all_texts.append(result["full_text"])
        all_confs.extend(r["confidence"] for r in result["regions"])
        page_dims.append({"page": page.page_num, "width": result["width"], "height": result["height"]})

    overall_confidence = round(sum(all_confs) / len(all_confs), 1) if all_confs else None
    return {
        "full_text": "\n\n".join(all_texts),
        "confidence": overall_confidence,
        "regions": all_regions,
        "pages": page_dims,
        "page_count": len(pages),
    }
